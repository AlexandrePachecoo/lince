import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { emiteToken } from "../../src/auth/jwt.js";
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

// Emite o token direto em vez de passar pelo login: o que estes testes exercitam é a
// fila, e um POST /v1/auth/login por caso pagaria scrypt à toa. É a mesma função que a
// rota de login usa -- não um atalho que só a suíte conhece.
function tokenDe(usuario: UsuarioSemeado): string {
  return emiteToken(SEGREDO_TESTE, {
    usuarioId: usuario.usuarioId,
    tenantId: usuario.tenantId,
    tokenVersao: 0,
    expiraEmS: 3600,
  });
}

async function lojaComGerente(): Promise<{ loja: LojaSemeada; usuario: UsuarioSemeado }> {
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  return { loja, usuario };
}

function fila(token: string, query = "") {
  return app.inject({
    method: "GET",
    url: `/v1/events${query}`,
    headers: { authorization: `Bearer ${token}` },
  });
}

async function tria(token: string, eventId: string, decisao: string) {
  const resposta = await app.inject({
    method: "POST",
    url: `/v1/events/${eventId}/triagem`,
    headers: { authorization: `Bearer ${token}` },
    payload: { decisao },
  });
  assert.equal(resposta.statusCode, 201, `triagem falhou com ${resposta.statusCode}`);
  return resposta;
}

test("sem header Authorization -> 401", async () => {
  const resposta = await app.inject({ method: "GET", url: "/v1/events" });
  assert.equal(resposta.statusCode, 401);
});

test("o padrão da fila é o que falta decidir, do mais recente para o mais antigo", async () => {
  // A fila é lista de trabalho, não histórico: o gerente abre o app no corredor e o que
  // ele precisa ver é o que ninguém decidiu. E o mais novo primeiro porque um alerta de
  // agora ainda é acionável -- dá para olhar a câmera, dá para falar com alguém.
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);
  const antigo = await criaEvento(app, loja, { ocorridoEm: "2026-08-23T10:00:00.000Z" });
  const recente = await criaEvento(app, loja, { ocorridoEm: "2026-08-23T18:00:00.000Z" });
  const decidido = await criaEvento(app, loja, { ocorridoEm: "2026-08-23T12:00:00.000Z" });
  await tria(token, decidido, "falso_positivo");

  const corpo = (await fila(token)).json();

  assert.deepEqual(
    corpo.eventos.map((e: { event_id: string }) => e.event_id),
    [recente, antigo],
    "evento já triado não pode continuar na fila de trabalho",
  );
});

test("triagem=todas traz também o que já foi decidido, com a decisão vigente", async () => {
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);
  const eventId = await criaEvento(app, loja);
  await tria(token, eventId, "confirmado");

  const corpo = (await fila(token, "?triagem=todas")).json();

  assert.equal(corpo.eventos.length, 1);
  assert.equal(corpo.eventos[0].triagem.decisao, "confirmado");
  assert.equal(corpo.eventos[0].triagem.decidido_por.id, usuario.usuarioId);
  assert.equal(corpo.eventos[0].triagem.revisada, false);
});

test("mudar de ideia: a vigente é a última, e a fila marca que houve correção", async () => {
  // A triagem é append-only e o dedo errado no corredor é o motivo. Se a fila mostrasse
  // a primeira decisão, corrigir não teria efeito nenhum; se mostrasse a última sem
  // dizer que houve correção, uma estatística de falso positivo pareceria mais limpa do
  // que foi (R-1).
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);
  const eventId = await criaEvento(app, loja);
  await tria(token, eventId, "falso_positivo");
  await tria(token, eventId, "confirmado");

  const corpo = (await fila(token, "?triagem=todas")).json();

  assert.equal(corpo.eventos[0].triagem.decisao, "confirmado");
  assert.equal(corpo.eventos[0].triagem.revisada, true);
  assert.equal(
    await prismaTeste.triagem.count({ where: { eventId } }),
    2,
    "corrigir não pode apagar a decisão anterior",
  );
});

test("evento de outro tenant não aparece na fila (NFR-6)", async () => {
  // O isolamento não é conferido depois da leitura: o tenant entra no WHERE, então a
  // linha da outra rede nem chega a ser lida. Um furto na loja de um cliente não pode
  // aparecer na tela de outro em hipótese nenhuma.
  const meu = await lojaComGerente();
  const alheia = await semeiaLoja();
  await criaEvento(app, alheia);
  const meuEvento = await criaEvento(app, meu.loja);

  const corpo = (await fila(tokenDe(meu.usuario))).json();

  assert.deepEqual(
    corpo.eventos.map((e: { event_id: string }) => e.event_id),
    [meuEvento],
  );
});

test("loja do mesmo tenant sem vínculo -> 403, e a fila padrão não a inclui", async () => {
  // Dentro da rede é o vínculo que decide, não o tenant: o gerente da loja A não vê a
  // fila da loja B. 403 e não 404 porque quem está autenticado já sabe que a loja
  // existe -- ela aparece no cadastro -- e "peça acesso ao admin" é acionável.
  const loja = await semeiaLoja();
  const outraLoja = await semeiaLoja({ tenantId: loja.tenantId });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  await criaEvento(app, outraLoja);
  const meu = await criaEvento(app, loja);
  const token = tokenDe(usuario);

  const pedindoAOutra = await fila(token, `?store_id=${outraLoja.lojaId}`);
  const padrao = (await fila(token)).json();

  assert.equal(pedindoAOutra.statusCode, 403);
  assert.deepEqual(
    padrao.eventos.map((e: { event_id: string }) => e.event_id),
    [meu],
  );
});

test("usuário sem vínculo nenhum recebe fila vazia, não erro", async () => {
  const loja = await semeiaLoja();
  const semLoja = await semeiaUsuario({ tenantId: loja.tenantId });
  await criaEvento(app, loja);

  const resposta = await fila(tokenDe(semLoja));

  assert.equal(resposta.statusCode, 200);
  assert.deepEqual(resposta.json().eventos, []);
});

test("paginação por cursor não pula evento quando chega um novo no meio", async () => {
  // É a razão de existir do cursor. Com OFFSET, cada evento que entra por cima empurra
  // um antigo para uma página já lida: ele some da tela sem ninguém decidir nada, e a
  // dívida operacional que o §4.5 quer visível vira invisível.
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);
  const c = await criaEvento(app, loja, { ocorridoEm: "2026-08-23T10:00:00.000Z" });
  const b = await criaEvento(app, loja, { ocorridoEm: "2026-08-23T11:00:00.000Z" });
  const a = await criaEvento(app, loja, { ocorridoEm: "2026-08-23T12:00:00.000Z" });

  const primeira = (await fila(token, "?limite=2")).json();
  assert.deepEqual(
    primeira.eventos.map((e: { event_id: string }) => e.event_id),
    [a, b],
  );
  assert.ok(primeira.proximo_cursor, "com mais eventos adiante o cursor não pode ser nulo");

  // Entra um evento mais recente que todos, entre uma página e outra.
  await criaEvento(app, loja, { ocorridoEm: "2026-08-23T23:00:00.000Z" });

  const segunda = (await fila(token, `?limite=2&cursor=${primeira.proximo_cursor}`)).json();

  assert.deepEqual(
    segunda.eventos.map((e: { event_id: string }) => e.event_id),
    [c],
    "o evento mais antigo não pode sumir porque chegou um novo",
  );
  assert.equal(segunda.proximo_cursor, null);
});

test("cursor corrompido -> 400, não um silencioso 'volta ao topo'", async () => {
  // Voltar ao início sem avisar faria o dashboard repetir a primeira página para sempre,
  // e o sintoma seria "a fila não anda" -- sem erro em lugar nenhum para investigar.
  const { usuario } = await lojaComGerente();

  const resposta = await fila(tokenDe(usuario), "?cursor=nao-e-um-cursor");

  assert.equal(resposta.statusCode, 400);
});

test("filtros de câmera e de período recortam a fila", async () => {
  const { loja, usuario } = await lojaComGerente();
  const token = tokenDe(usuario);
  await criaEvento(app, loja, { cameraId: "cam1", ocorridoEm: "2026-08-20T10:00:00.000Z" });
  const alvo = await criaEvento(app, loja, {
    cameraId: "cam3",
    ocorridoEm: "2026-08-23T10:00:00.000Z",
  });
  await criaEvento(app, loja, { cameraId: "cam3", ocorridoEm: "2026-08-25T10:00:00.000Z" });

  const porCamera = (await fila(token, "?camera_id=cam3")).json();
  const porPeriodo = (
    await fila(token, "?camera_id=cam3&desde=2026-08-22T00:00:00.000Z&ate=2026-08-24T00:00:00.000Z")
  ).json();

  assert.equal(porCamera.eventos.length, 2);
  assert.deepEqual(
    porPeriodo.eventos.map((e: { event_id: string }) => e.event_id),
    [alvo],
  );
});

test("parâmetros inválidos -> 400", async () => {
  const { usuario } = await lojaComGerente();
  const token = tokenDe(usuario);

  assert.equal((await fila(token, "?limite=0")).statusCode, 400);
  assert.equal((await fila(token, "?limite=999")).statusCode, 400);
  assert.equal((await fila(token, "?triagem=talvez")).statusCode, 400);
  assert.equal((await fila(token, "?desde=ontem")).statusCode, 400);
  // Repetido é engano do cliente; adivinhar qual vale esconderia o erro numa lista
  // filtrada pela loja errada.
  assert.equal((await fila(token, "?camera_id=cam1&camera_id=cam3")).statusCode, 400);
});

test("o desfecho do corte e o do upload são campos diferentes", async () => {
  // A armadilha registrada no CLAUDE.md, agora do lado de quem lê: `clip` é o que a
  // borda conseguiu cortar, `clip_state` é se os bytes chegaram ao bucket. Um evento com
  // clipe cortado bem pode estar com upload pendente, e é a distinção que diz ao triador
  // se vale esperar.
  const { loja, usuario } = await lojaComGerente();
  const eventId = await criaEvento(app, loja);

  const corpo = (await fila(tokenDe(usuario))).json();
  const evento = corpo.eventos.find((e: { event_id: string }) => e.event_id === eventId);

  assert.equal(evento.clip.status, "ok", "o corte deu certo na borda");
  assert.equal(evento.clip_state, "pendente", "e os bytes ainda não subiram");
});

test("evento manual entra na fila e não finge ter regra", async () => {
  // O andaime de gatilho da instalação precisa aparecer para o instalador conferir, e
  // precisa ser distinguível: 40 testes numa tarde não podem afundar a estatística de
  // falso positivo da câmera (R-1).
  const { loja, usuario } = await lojaComGerente();
  await criaEvento(app, loja, { source: "manual" });

  const corpo = (await fila(tokenDe(usuario))).json();

  assert.equal(corpo.eventos[0].source, "manual");
  assert.equal(corpo.eventos[0].rule, null);
});
