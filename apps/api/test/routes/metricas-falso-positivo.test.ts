import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { emiteToken } from "../../src/auth/jwt.js";
import { diaLocal, inicioDoDiaUtc } from "../../src/metricas/dias.js";
import { SEGREDO_TESTE, buildTestApp } from "../helpers/build-app.js";
import { criaEvento } from "../helpers/evento.js";
import {
  type LojaSemeada,
  type UsuarioSemeado,
  limpaBanco,
  prismaTeste,
  semeiaLoja,
  semeiaUsuario,
} from "../helpers/test-db.js";

// A métrica do R-1 contra Postgres de verdade, porque o que ela tem de arriscado está no
// SQL: a decisão vigente resolvida por LATERAL e a conversão de fuso feita no banco.
// Reproduzir isso em JavaScript no teste provaria que o teste sabe somar, não que a
// consulta está certa.

const SP = "America/Sao_Paulo";

let app: FastifyInstance;

before(async () => {
  app = await buildTestApp();
});
after(async () => {
  await app.close();
});
beforeEach(async () => {
  await limpaBanco();
});

function tokenDe(usuario: UsuarioSemeado): string {
  return emiteToken(SEGREDO_TESTE, {
    usuarioId: usuario.usuarioId,
    tenantId: usuario.tenantId,
    tokenVersao: 0,
    expiraEmS: 3600,
  });
}

async function lojaComGerente(opcoes: { fuso?: string } = {}) {
  const loja = await semeiaLoja();
  if (opcoes.fuso !== undefined) {
    await prismaTeste.loja.update({
      where: { id: loja.lojaId },
      data: { fusoHorario: opcoes.fuso },
    });
  }
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  return { loja, usuario };
}

function metrica(token: string, query = "") {
  return app.inject({
    method: "GET",
    url: `/v1/metricas/falso-positivo${query}`,
    headers: { authorization: `Bearer ${token}` },
  });
}

/** Instante de hoje, para o evento cair dentro da janela padrão de 7 dias. */
function hojeAs(hora: number, minuto = 0): string {
  const agora = new Date();
  agora.setUTCHours(hora, minuto, 0, 0);
  return agora.toISOString();
}

async function decide(
  token: string,
  eventId: string,
  decisao: "confirmado" | "falso_positivo" | "inconclusivo",
) {
  const resposta = await app.inject({
    method: "POST",
    url: `/v1/events/${eventId}/triagem`,
    headers: { authorization: `Bearer ${token}` },
    payload: { decisao },
  });
  assert.equal(resposta.statusCode, 201, `triagem falhou: ${resposta.body}`);
}

test("conta o falso positivo da câmera e deixa o resto de fora", async () => {
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);

  const falso = await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: hojeAs(14) });
  const confirmado = await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: hojeAs(15) });
  const inconclusivo = await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: hojeAs(16) });
  await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: hojeAs(17) }); // pendente

  await decide(token, falso, "falso_positivo");
  await decide(token, confirmado, "confirmado");
  await decide(token, inconclusivo, "inconclusivo");

  const corpo = (await metrica(token)).json();
  const camera = corpo.cameras.find((c: { camera_id: string }) => c.camera_id === "cam3");

  assert.equal(camera.falso_positivo, 1);
  assert.equal(camera.confirmado, 1);
  assert.equal(camera.inconclusivo, 1);
  assert.equal(camera.pendentes, 1);
  assert.equal(camera.eventos, 4);
});

test("a re-triagem move o evento de coluna, e não cria um segundo", async () => {
  // A triagem é append-only: mudar de ideia grava linha nova, e a vigente é a última por
  // seq. Se a consulta contasse linhas de triagem em vez da vigente, um gerente corrigindo
  // um toque errado somaria falso positivo E confirmado para o mesmo evento -- inflando a
  // métrica exatamente quando alguém tenta consertá-la.
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);

  const eventId = await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: hojeAs(14) });
  await decide(token, eventId, "falso_positivo");
  await decide(token, eventId, "confirmado");

  const corpo = (await metrica(token)).json();
  const camera = corpo.cameras[0];

  assert.equal(camera.falso_positivo, 0, "a decisão antiga não conta mais");
  assert.equal(camera.confirmado, 1);
  assert.equal(camera.eventos, 1, "e continua sendo um evento só");
});

test("evento de instalação não entra na métrica", async () => {
  // O andaime de gatilho. Um instalador testando 40 vezes numa tarde afundaria a
  // estatística da câmera que ele estava justamente calibrando (R-1).
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);

  const manual = await criaEvento(app, loja, {
    cameraId: "cam3",
    source: "manual",
    ocorridoEm: hojeAs(14),
  });
  await decide(token, manual, "falso_positivo");

  const corpo = (await metrica(token)).json();

  assert.deepEqual(corpo.cameras, [], "nem como evento, nem como falso positivo");
});

test("a noite da loja não é partida em dois dias", async () => {
  // O motivo da coluna de fuso. Dois eventos da mesma noite em São Paulo -- 22h e 23h --
  // caem em dias UTC **diferentes**, porque a meia-noite de Greenwich passa no meio deles.
  // Contados pelo relógio de Greenwich, o pior dia da câmera sairia pela metade.
  //
  // Os instantes são relativos a hoje, e não datas fixas: uma data fixa sairia da janela
  // máxima de 90 dias com o tempo, e o teste passaria a não testar nada -- em silêncio,
  // que é o pior desfecho para o teste que guarda justamente um erro silencioso.
  const { loja, usuario } = await lojaComGerente({ fuso: SP });
  const token = tokenDe(usuario);

  // 20h e 22h de ontem na loja. Em UTC-3 isso é 23h de anteontem e 01h de ontem: a
  // meia-noite de Greenwich passa entre os dois, que é precisamente a fronteira que a
  // consulta precisa reconciliar. 22h e 23h locais não serviriam -- caem no mesmo dia UTC.
  const meiaNoiteDeHoje = inicioDoDiaUtc(diaLocal(new Date(), SP), SP).getTime();
  const vinteHoras = new Date(meiaNoiteDeHoje - 4 * 3_600_000);
  const vinteEDuas = new Date(meiaNoiteDeHoje - 2 * 3_600_000);
  const ontem = diaLocal(vinteHoras, SP);

  // A premissa do teste, afirmada em vez de suposta: os dois instantes são do mesmo dia
  // na loja e de dias diferentes em UTC. Sem isto, um fuso que mudasse de offset faria o
  // teste passar sem exercitar a fronteira.
  assert.equal(diaLocal(vinteEDuas, SP), ontem, "mesma noite na loja");
  assert.notEqual(
    vinteHoras.toISOString().slice(0, 10),
    vinteEDuas.toISOString().slice(0, 10),
    "e dias diferentes em UTC -- é isso que a consulta precisa reconciliar",
  );

  for (const ocorridoEm of [vinteHoras, vinteEDuas]) {
    await decide(
      token,
      await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: ocorridoEm.toISOString() }),
      "falso_positivo",
    );
  }

  const corpo = (await metrica(token, "?dias=2")).json();
  const camera = corpo.cameras[0];

  assert.equal(camera.falso_positivo, 2);
  assert.deepEqual(
    camera.pior_dia,
    { dia: ontem, falso_positivo: 2 },
    "os dois na mesma noite, não um em cada dia",
  );
});

test("o pior dia decide acima_do_limite, e a média não", async () => {
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);

  for (let i = 0; i < 4; i++) {
    await decide(
      token,
      await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: hojeAs(10 + i) }),
      "falso_positivo",
    );
  }

  const corpo = (await metrica(token, "?dias=30")).json();
  const camera = corpo.cameras[0];

  assert.equal(camera.acima_do_limite, true, "4 num dia estoura o teto de 3");
  assert.ok(camera.falso_positivo_por_dia < 1, "e a média em 30 dias é quase zero");
});

test("a fila de outra loja do tenant não aparece sem vínculo (NFR-6)", async () => {
  const loja = await semeiaLoja();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const token = tokenDe(usuario);

  const alheio = await criaEvento(app, outra, { cameraId: "cam9", ocorridoEm: hojeAs(14) });
  await prismaTeste.triagem.create({
    data: {
      eventId: alheio,
      tenantId: outra.tenantId,
      usuarioId: usuario.usuarioId,
      decisao: "falso_positivo",
    },
  });

  const corpo = (await metrica(token)).json();

  assert.deepEqual(corpo.cameras, [], "é o vínculo que decide o que ele enxerga, não o tenant");
});

test("store_id de loja que o usuário não alcança -> 403", async () => {
  const loja = await semeiaLoja();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });

  const resposta = await metrica(tokenDe(usuario), `?store_id=${outra.lojaId}`);

  assert.equal(resposta.statusCode, 403);
});

test("lojas em fusos diferentes recusam a leitura em vez de misturar os dias", async () => {
  // "O dia" passaria a significar duas coisas na mesma resposta, e o número não teria
  // como ser auditado depois. Um 400 acionável é melhor do que uma métrica que ninguém
  // consegue explicar -- e com uma loja por fuso o caso nunca aparece.
  const loja = await semeiaLoja();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });
  await prismaTeste.loja.update({
    where: { id: outra.lojaId },
    data: { fusoHorario: "Asia/Tokyo" },
  });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente", [outra.lojaId]: "gerente" },
  });
  const token = tokenDe(usuario);

  const misturado = await metrica(token);
  const escolhida = await metrica(token, `?store_id=${loja.lojaId}`);

  assert.equal(misturado.statusCode, 400);
  assert.match(misturado.json().erro, /fusos horários diferentes/);
  assert.equal(escolhida.statusCode, 200, "escolher uma loja resolve");
  assert.equal(escolhida.json().periodo.fuso, "America/Sao_Paulo");
});

test("dias fora de 1..90 é 400", async () => {
  // O teto existe porque a consulta varre eventos por período: sem ele, um cliente
  // pedindo 3650 dias faz a API varrer a tabela e a lentidão aparece na fila de triagem.
  const { usuario } = await lojaComGerente();
  const token = tokenDe(usuario);

  assert.equal((await metrica(token, "?dias=0")).statusCode, 400);
  assert.equal((await metrica(token, "?dias=91")).statusCode, 400);
  assert.equal((await metrica(token, "?dias=abc")).statusCode, 400);
  assert.equal((await metrica(token, "?dias=7")).statusCode, 200);
});

test("credencial de agente não lê a métrica", async () => {
  const { loja } = await lojaComGerente();

  assert.equal((await metrica(loja.token)).statusCode, 401);
  assert.equal(
    (await app.inject({ method: "GET", url: "/v1/metricas/falso-positivo" })).statusCode,
    401,
  );
});

test("loja sem evento nenhum devolve lista vazia com o período preenchido", async () => {
  // A tela do dia bom precisa saber que janela foi consultada, para dizer "nenhum alerta
  // nos últimos 7 dias" em vez de um vazio que se confunde com falha de carregamento.
  const { usuario } = await lojaComGerente();

  const corpo = (await metrica(tokenDe(usuario))).json();

  assert.deepEqual(corpo.cameras, []);
  assert.equal(corpo.periodo.dias, 7);
  assert.equal(corpo.limite_diario, 3);
  assert.match(corpo.periodo.desde, /^\d{4}-\d{2}-\d{2}$/);
});
