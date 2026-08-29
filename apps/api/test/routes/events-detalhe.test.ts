import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { emiteToken } from "../../src/auth/jwt.js";
import { SEGREDO_TESTE, buildTestApp } from "../helpers/build-app.js";
import { criaEvento } from "../helpers/evento.js";
import {
  type LojaSemeada,
  type UsuarioSemeado,
  limpaBanco,
  semeiaLoja,
  semeiaUsuario,
} from "../helpers/test-db.js";

// GET /v1/events/{event_id} (§4.5): o evento avulso, para quem tem o id e não tem a fila
// -- um F5 na tela do evento, o link mandado ao técnico, a notificação quando existir.
//
// O que estes testes protegem não é a rota devolver "algum" evento: é ela devolver **o
// mesmo documento que a fila devolve**. Se as duas montagens divergirem, o sintoma não é
// erro nenhum -- é um campo que existe numa tela e não na outra, e alguém decidindo com
// menos informação do que achava que tinha.

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

// Mesmo atalho de events-listar.test.ts, e pelo mesmo motivo: o que se exercita aqui é a
// rota do evento, e um login por caso pagaria scrypt à toa. A função é a de produção.
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

function detalhe(token: string, eventId: string) {
  return app.inject({
    method: "GET",
    url: `/v1/events/${eventId}`,
    headers: { authorization: `Bearer ${token}` },
  });
}

function fila(token: string, query = "") {
  return app.inject({
    method: "GET",
    url: `/v1/events${query}`,
    headers: { authorization: `Bearer ${token}` },
  });
}

async function tria(token: string, eventId: string, decisao: string, observacao?: string) {
  const resposta = await app.inject({
    method: "POST",
    url: `/v1/events/${eventId}/triagem`,
    headers: { authorization: `Bearer ${token}` },
    payload: { decisao, ...(observacao !== undefined ? { observacao } : {}) },
  });
  assert.equal(resposta.statusCode, 201, `triagem falhou: ${resposta.body}`);
  return resposta;
}

test("o evento avulso é byte a byte o mesmo que a fila devolve", async () => {
  // A razão de existir de apresentacao.ts, agora com teste: a fila e o detalhe são a
  // mesma montagem. Comparar contra a fila em vez de contra um literal é o que faz este
  // teste falhar quando alguém acrescenta um campo só de um lado -- um literal aqui
  // precisaria ser atualizado à mão e passaria a concordar com a divergência.
  const { loja, usuario } = await lojaComGerente();
  const eventId = await criaEvento(app, loja, { cameraId: "cam3" });
  const token = tokenDe(usuario);

  const daFila = (await fila(token)).json().eventos[0];
  const resposta = await detalhe(token, eventId);

  assert.equal(resposta.statusCode, 200);
  assert.deepEqual(resposta.json(), { schema_version: 1, evento: daFila });
});

test("evento triado abre com a decisão vigente, e a correção aparece como correção", async () => {
  // O caso do dedo errado (NFR-9): quem reabre o evento pelo link precisa ver a decisão
  // que vale agora -- a última, por seq -- e precisa ver que houve correção. Sem
  // `revisada`, uma re-triagem se apresentaria como se sempre tivesse sido aquilo.
  const { loja, usuario } = await lojaComGerente();
  const eventId = await criaEvento(app, loja);
  const token = tokenDe(usuario);

  await tria(token, eventId, "confirmado");
  const primeira = (await detalhe(token, eventId)).json().evento;
  assert.equal(primeira.triagem.decisao, "confirmado");
  assert.equal(primeira.triagem.revisada, false, "a primeira decisão não é revisão");

  await tria(token, eventId, "falso_positivo", "cliente pagou no autoatendimento");
  const segunda = (await detalhe(token, eventId)).json().evento;

  assert.equal(segunda.triagem.decisao, "falso_positivo", "a vigente é a última");
  assert.equal(segunda.triagem.observacao, "cliente pagou no autoatendimento");
  assert.equal(segunda.triagem.revisada, true);
  assert.equal(segunda.triagem.decidido_por.id, usuario.usuarioId);
});

test("evento sem triagem chega com triagem nula, e não omitida", async () => {
  // Evento sem triagem é dívida operacional **visível** (§4.5). `null` explícito é o que
  // deixa a tela dizer "ninguém decidiu isto ainda"; um campo ausente vira `undefined` no
  // cliente e se confunde com erro de carregamento.
  const { loja, usuario } = await lojaComGerente();
  const eventId = await criaEvento(app, loja);

  const evento = (await detalhe(tokenDe(usuario), eventId)).json().evento;

  assert.equal(evento.triagem, null);
  assert.ok("triagem" in evento, "a chave existe, com valor nulo");
});

test("clipe que não vai existir chega com o motivo junto", async () => {
  // É o que libera o triador a decidir sem vídeo em vez de esperar para sempre. Sem
  // `clip_error` na tela, `indisponivel` é indistinguível de "ainda está subindo", e a
  // diferença entre os dois é a única coisa que diz se vale esperar.
  const { loja, usuario } = await lojaComGerente();
  const eventId = await criaEvento(app, loja, { clipStatus: "clip_failed" });

  const evento = (await detalhe(tokenDe(usuario), eventId)).json().evento;

  assert.equal(evento.clip_state, "indisponivel");
  assert.equal(evento.clip_error, "buffer despejado pelo teto de disco");
  // E o bloco `clip` continua contando o desfecho do **corte**, que é outra pergunta.
  assert.equal(evento.clip.status, "clip_failed");
});

test("loja do mesmo tenant sem vínculo -> 403, e não 404", async () => {
  // Dentro do tenant a recusa é acionável: quem está autenticado já sabe que as lojas da
  // rede existem, e "peça acesso ao admin" resolve. Responder 404 aqui mandaria o gerente
  // procurar um evento que existe.
  const loja = await semeiaLoja();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, outra);

  const resposta = await detalhe(tokenDe(usuario), eventId);

  assert.equal(resposta.statusCode, 403);
});

test("evento de outro tenant -> 404, e não 403 (NFR-6)", async () => {
  // 403 confirmaria que aquele event_id existe em alguma outra rede. Dentro do tenant a
  // existência já é sabida; fora dele, confirmá-la é vazamento -- e é a mesma escolha que
  // clipe-url.ts e triagem.ts fazem.
  const alheia = await semeiaLoja();
  const minha = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: minha.tenantId,
    lojas: { [minha.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, alheia);

  const resposta = await detalhe(tokenDe(usuario), eventId);

  assert.equal(resposta.statusCode, 404);
});

test("event_id inexistente ou malformado -> 404, nunca 500", async () => {
  // O link chega por WhatsApp e por notificação, e vai ser truncado e colado errado. O
  // id é texto no banco justamente por não ser um UUID validado -- então nada aqui pode
  // estourar antes de virar o mesmo 404 de sempre.
  const { usuario } = await lojaComGerente();
  const token = tokenDe(usuario);

  assert.equal((await detalhe(token, randomUUID())).statusCode, 404);
  assert.equal((await detalhe(token, "nao-é-um-uuid")).statusCode, 404);
});

test("credencial de agente não abre evento (§4.5)", async () => {
  // O token do agente vive num box no estoque de um mercado. Se ele abrisse o evento, o
  // clipe de qualquer alerta estaria a um arrombamento de distância -- por isso os dois
  // caminhos de credencial leem o mesmo cabeçalho e não se cruzam em lugar nenhum.
  const { loja } = await lojaComGerente();
  const eventId = await criaEvento(app, loja);

  const comTokenDeAgente = await detalhe(loja.token, eventId);
  const semCabecalho = await app.inject({ method: "GET", url: `/v1/events/${eventId}` });

  assert.equal(comTokenDeAgente.statusCode, 401);
  assert.equal(semCabecalho.statusCode, 401);
});
