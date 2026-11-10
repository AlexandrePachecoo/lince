import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { conteudoDoAlerta, conteudoDoTeto } from "../../src/notificacao/conteudo.js";
import { validaNotificacao } from "../../src/schemas/dashboard.js";
import { buildTestApp } from "../helpers/build-app.js";
import { criaEvento } from "../helpers/evento.js";
import { ServicoPushFalso, configPushTeste } from "../helpers/servico-push.js";
import {
  type LojaSemeada,
  limpaBanco,
  prismaTeste,
  semeiaLoja,
  semeiaUsuario,
} from "../helpers/test-db.js";

// Do POST /v1/events ao celular (§4.5, §4.6, R-12).
//
// O número que estes testes protegem não é "a notificação chegou": é **quantas vezes o
// celular do gerente toca**. Uma câmera mal calibrada mandando trinta notificações numa
// noite é como o gerente desliga a notificação — e com ela o produto (R-1). Por isso
// cada caso aqui conta POSTs no serviço de push, e não só linhas no banco.

const TETO = 2;
let app: FastifyInstance;
let servico: ServicoPushFalso;
let agora: Date;

before(async () => {
  servico = await ServicoPushFalso.sobe();
  app = await buildTestApp({
    push: configPushTeste(servico.origem, { tetoDiarioPorLoja: TETO, janelaAgrupamentoS: 120 }),
    relogioAlerta: () => agora,
  });
});
after(async () => {
  await app.close();
  await servico.fecha();
});
beforeEach(async () => {
  await limpaBanco();
  servico.limpa();
  agora = new Date("2026-08-23T14:00:05.000Z");
});

/** Gerente da loja com o celular inscrito, e a loja. */
async function lojaComCelular(nome = "celular"): Promise<LojaSemeada> {
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  await inscreve(usuario.usuarioId, usuario.tenantId, nome);
  return loja;
}

async function inscreve(usuarioId: string, tenantId: string, nome: string): Promise<void> {
  const inscricao = servico.inscricao(nome);
  await prismaTeste.inscricaoPush.create({
    data: {
      tenantId,
      usuarioId,
      endpoint: inscricao.endpoint,
      p256dh: inscricao.keys.p256dh,
      auth: inscricao.keys.auth,
    },
  });
}

/** Um evento chegando e o envio dele terminado. */
async function chega(
  loja: LojaSemeada,
  opcoes: Parameters<typeof criaEvento>[2] = {},
): Promise<string> {
  const eventId = await criaEvento(app, loja, { ocorridoEm: agora.toISOString(), ...opcoes });
  await app.notificador?.ocioso();
  return eventId;
}

async function decisaoDe(eventId: string) {
  return (await prismaTeste.alerta.findUnique({ where: { eventId } }))?.decisao ?? null;
}

test("evento novo toca o celular de quem tem vínculo com a loja, cifrado e urgente", async () => {
  const loja = await lojaComCelular();

  const eventId = await chega(loja);

  const [push] = servico.recebidosEm("celular");
  assert.ok(push, "o serviço de push não recebeu nada");
  assert.equal(servico.recebidos.length, 1);
  // O que o celular faz com a mensagem depende destes três: aes128gcm é a cifragem que
  // todo navegador lê, `urgency: high` acorda o aparelho em economia de bateria, e o TTL
  // curto impede o celular que volta à rede uma hora depois de tocar por um furto velho.
  assert.equal(push.cabecalhos["content-encoding"], "aes128gcm");
  assert.equal(push.cabecalhos.urgency, "high");
  assert.equal(push.cabecalhos.ttl, "600");
  assert.match(String(push.cabecalhos.authorization), /^vapid t=.+, k=.+$/);
  assert.ok(push.bytes > 0);

  assert.equal(await decisaoDe(eventId), "notificado");
  const envios = await prismaTeste.envioAlerta.findMany({ where: { eventId } });
  assert.equal(envios.length, 1);
  assert.equal(envios[0]?.status, "enviado");
  assert.equal(envios[0]?.httpStatus, 201);
});

test("quem não alcança a loja não recebe: outra loja, outro tenant, usuário desativado", async () => {
  const loja = await lojaComCelular("da-loja");
  const outraLoja = await semeiaLoja({ tenantId: loja.tenantId });
  const deOutraLoja = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [outraLoja.lojaId]: "gerente" },
  });
  await inscreve(deOutraLoja.usuarioId, deOutraLoja.tenantId, "outra-loja");
  const desativado = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
    ativo: false,
  });
  await inscreve(desativado.usuarioId, desativado.tenantId, "desativado");
  const outroTenant = await lojaComCelular("outro-tenant");

  await chega(loja);

  assert.equal(servico.recebidosEm("da-loja").length, 1);
  assert.equal(servico.recebidosEm("outra-loja").length, 0);
  assert.equal(servico.recebidosEm("desativado").length, 0);
  assert.equal(servico.recebidosEm("outro-tenant").length, 0);
  assert.ok(outroTenant);
});

test("reenvio do mesmo evento não toca de novo", async () => {
  // A borda reposta para renovar a URL vencida ou depois de um 404 no PATCH (§5.4). O
  // gerente já foi avisado — ou já decidiu — da primeira vez.
  const loja = await lojaComCelular();
  const eventId = await chega(loja);

  const evento = await prismaTeste.evento.findUniqueOrThrow({ where: { eventId } });
  await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
    payload: {
      schema_version: 1,
      event_id: eventId,
      tenant_id: loja.tenantId,
      store_id: loja.lojaId,
      camera_id: evento.cameraId,
      occurred_at: evento.ocorridoEm.toISOString(),
      reported_at: evento.ocorridoEm.toISOString(),
      source: "rule",
      rule: { id: "saida-sem-passar-no-caixa", version: 3 },
      versions: { agent: "0.1.0" },
      clip: { status: "ok", duration_s: 15, size_bytes: 1_200_000 },
    },
  });
  await app.notificador?.ocioso();

  assert.equal(servico.recebidos.length, 1);
});

test("teste de instalação não toca o celular do gerente", async () => {
  const loja = await lojaComCelular();

  const eventId = await chega(loja, { source: "manual" });

  assert.equal(servico.recebidos.length, 0);
  assert.equal(await decisaoDe(eventId), null, "manual não entra nem na contagem do teto");
});

test("segundo evento da mesma câmera dentro da janela vai para a fila sem tocar", async () => {
  // §4.6: a rajada de uma câmera mal calibrada vira um alerta, não trinta.
  const loja = await lojaComCelular();
  await chega(loja, { cameraId: "cam3" });
  agora = new Date(agora.getTime() + 60_000);

  const segundo = await chega(loja, { cameraId: "cam3" });

  assert.equal(servico.recebidos.length, 1);
  assert.equal(await decisaoDe(segundo), "agrupado");
  // E o evento agrupado está na fila como qualquer outro: o agrupamento é do aviso.
  assert.ok(await prismaTeste.evento.findUnique({ where: { eventId: segundo } }));
});

test("outra câmera dentro da janela toca: agrupar por loja esconderia um corredor", async () => {
  // Agrupar por loja deixaria o furto do corredor 2 atrás do alarme do corredor 1.
  const loja = await lojaComCelular();
  await chega(loja, { cameraId: "cam3" });

  const outraCamera = await chega(loja, { cameraId: "cam1" });

  assert.equal(await decisaoDe(outraCamera), "notificado");
  assert.equal(servico.recebidos.length, 2);
});

test("o outro gatilho na mesma câmera toca dentro da janela", async () => {
  // A saída pela porta e o gesto na prateleira não são o mesmo acontecimento, e o falso
  // positivo de um não pode calar o outro.
  const loja = await lojaComCelular();
  await chega(loja, { cameraId: "cam3" });

  const gesto = await chega(loja, { cameraId: "cam3", source: "gesture" });

  assert.equal(await decisaoDe(gesto), "notificado");
  assert.equal(servico.recebidos.length, 2);
});

test("vencida a janela, a mesma câmera toca de novo", async () => {
  const loja = await lojaComCelular();
  await chega(loja, { cameraId: "cam3" });
  agora = new Date(agora.getTime() + 121_000);

  const depois = await chega(loja, { cameraId: "cam3" });

  assert.equal(await decisaoDe(depois), "notificado");
  assert.equal(servico.recebidos.length, 2);
});

test("bateu o teto: um aviso de que o resto vai só para a fila, e depois silêncio", async () => {
  // R-12. Sem o aviso, o silêncio depois do teto pareceria loja tranquila.
  const loja = await lojaComCelular();
  for (const camera of ["cam1", "cam2"]) {
    await chega(loja, { cameraId: camera });
  }
  assert.equal(servico.recebidos.length, TETO);

  const primeiroBarrado = await chega(loja, { cameraId: "cam3" });
  const segundoBarrado = await chega(loja, { cameraId: "cam4" });

  assert.equal(await decisaoDe(primeiroBarrado), "teto");
  assert.equal(await decisaoDe(segundoBarrado), "teto");
  assert.equal(servico.recebidos.length, TETO + 1, "um aviso de teto, e só um");
});

test("o teto zera na meia-noite da loja, e não na de Greenwich", async () => {
  // 02:30 UTC é 23:30 em São Paulo. Em UTC os dois eventos abaixo seriam do mesmo dia e
  // o segundo bateria o teto; no dia da loja, o segundo é do dia seguinte.
  const loja = await lojaComCelular();
  agora = new Date("2026-08-24T01:00:00.000Z"); // 22:00 do dia 23 em São Paulo
  await chega(loja, { cameraId: "cam1" });
  agora = new Date("2026-08-24T02:30:00.000Z"); // 23:30 do dia 23
  await chega(loja, { cameraId: "cam2" });
  agora = new Date("2026-08-24T03:30:00.000Z"); // 00:30 do dia 24

  const novoDia = await chega(loja, { cameraId: "cam3" });

  assert.equal(await decisaoDe(novoDia), "notificado");
  assert.equal(servico.recebidos.length, 3);
});

test("evento que chega muito depois de acontecer não toca", async () => {
  // A fila da borda drenando depois de uma noite sem link. O evento está na fila; tocar
  // às 15h por um furto das 11h manda o gerente correr atrás de ninguém.
  const loja = await lojaComCelular();
  const ocorrido = new Date(agora.getTime() - 31 * 60_000);

  const eventId = await chega(loja, { ocorridoEm: ocorrido.toISOString() });

  assert.equal(await decisaoDe(eventId), "atrasado");
  assert.equal(servico.recebidos.length, 0);
});

test("410 do serviço de push apaga a inscrição; 500 só registra a falha", async () => {
  // 410 é o aparelho que desinstalou ou limpou o site: mandar de novo é inútil para
  // sempre. 500 é o serviço de push com problema agora, e o aparelho continua válido.
  const loja = await lojaComCelular("sumiu");
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "operador" },
  });
  await inscreve(usuario.usuarioId, usuario.tenantId, "instavel");
  servico.respostas.set("/push/sumiu", 410);
  servico.respostas.set("/push/instavel", 500);

  const eventId = await chega(loja);

  const envios = await prismaTeste.envioAlerta.findMany({ where: { eventId } });
  const porStatus = Object.fromEntries(envios.map((e) => [e.status, e.httpStatus]));
  assert.deepEqual(porStatus, { expirada: 410, falhou: 500 });
  const restantes = await prismaTeste.inscricaoPush.findMany();
  assert.deepEqual(
    restantes.map((i) => i.endpoint),
    [`${servico.origem}/push/instavel`],
  );
});

test("eventos simultâneos da mesma câmera tocam uma vez só", async () => {
  // A borda drenando a fila depois de o link voltar manda vários de uma vez. Sem a trava
  // por loja, todos leriam "nenhum notificado na janela" e todos tocariam. Várias
  // rodadas, porque um par de requisições às vezes serializa sozinho e o teste passaria
  // por sorte (CLAUDE.md, armadilhas).
  for (let rodada = 0; rodada < 5; rodada++) {
    await limpaBanco();
    servico.limpa();
    const loja = await lojaComCelular();

    await Promise.all(
      Array.from({ length: 6 }, () =>
        criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: agora.toISOString() }),
      ),
    );
    await app.notificador?.ocioso();

    assert.equal(servico.recebidos.length, 1, `rodada ${rodada}`);
    assert.equal(await prismaTeste.alerta.count({ where: { decisao: "notificado" } }), 1);
    assert.equal(await prismaTeste.alerta.count({ where: { decisao: "agrupado" } }), 5);
  }
});

test("o conteúdo diz câmera, gatilho e hora da loja, e bate com o contrato", () => {
  // A tela de bloqueio é tudo o que o gerente vê antes de decidir abrir. E nada de
  // clipe ou URL: o conteúdo atravessa o serviço de push do fabricante (R-9).
  const loja = { nome: "Mercado Centro", fusoHorario: "America/Sao_Paulo" };
  const alerta = conteudoDoAlerta(
    {
      eventId: "0b6f1a52-1d0e-4c9a-9a0e-6f1f2c3d4e5f",
      tenantId: "t",
      lojaId: "loja-1",
      cameraId: "cam3",
      source: "gesture",
      ocorridoEm: new Date("2026-08-23T14:07:00.000Z"),
    },
    loja,
  );
  assert.equal(alerta.titulo, "Possível furto — cam3");
  assert.equal(alerta.corpo, "gesto de esconder · Mercado Centro · 11:07");
  assert.equal(alerta.url, "/eventos/0b6f1a52-1d0e-4c9a-9a0e-6f1f2c3d4e5f");
  assert.deepEqual(validaNotificacao(alerta), { valido: true });

  const teto = conteudoDoTeto(loja, "loja-1", 20);
  assert.equal(teto.url, "/fila");
  assert.deepEqual(validaNotificacao(teto), { valido: true });
});
