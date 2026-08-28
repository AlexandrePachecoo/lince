import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { emiteToken } from "../../src/auth/jwt.js";
import { SEGREDO_TESTE, buildTestApp } from "../helpers/build-app.js";
import { criaEvento } from "../helpers/evento.js";
import {
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

function tokenDe(usuario: UsuarioSemeado, tokenVersao = 0): string {
  return emiteToken(SEGREDO_TESTE, {
    usuarioId: usuario.usuarioId,
    tenantId: usuario.tenantId,
    tokenVersao,
    expiraEmS: 3600,
  });
}

function tria(token: string, eventId: string, corpo: unknown) {
  return app.inject({
    method: "POST",
    url: `/v1/events/${eventId}/triagem`,
    headers: { authorization: `Bearer ${token}` },
    payload: corpo as object,
  });
}

test("decisão do gerente -> 201 com o evento já atualizado", async () => {
  // A resposta é o evento, e não um {ok: true}: o PWA acabou de tirar a linha da fila e
  // precisa refletir o novo estado sem uma segunda chamada -- o gerente está em pé, com
  // uma mão, e cada ida à rede é um instante de tela inconsistente (NFR-9).
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, loja);

  const resposta = await tria(tokenDe(usuario), eventId, {
    decisao: "confirmado",
    observacao: "levou duas latas pelo corredor da frente",
  });

  assert.equal(resposta.statusCode, 201);
  const evento = resposta.json().evento;
  assert.equal(evento.event_id, eventId);
  assert.equal(evento.triagem.decisao, "confirmado");
  assert.equal(evento.triagem.observacao, "levou duas latas pelo corredor da frente");
  assert.equal(evento.triagem.decidido_por.nome, usuario.nome);
});

test("qualquer papel com acesso à loja tria", async () => {
  // Triagem é o trabalho do produto, não privilégio. Restringi-la a admin ou gerente
  // deixaria evento parado justamente nos horários em que só há operador na loja -- e
  // evento parado é a dívida que o §4.5 quer visível, não a que quer criar.
  const loja = await semeiaLoja();
  const operador = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "operador" },
  });
  const eventId = await criaEvento(app, loja);

  const resposta = await tria(tokenDe(operador), eventId, { decisao: "inconclusivo" });

  assert.equal(resposta.statusCode, 201);
});

test("decidir de novo grava linha nova e não apaga a anterior", async () => {
  // Append-only por causa do dedo errado: dois toques, celular numa mão, corredor de
  // mercado (NFR-9). Sobrescrever transformaria um toque errado em falso positivo
  // permanente na estatística da câmera, que é o número do R-1.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const token = tokenDe(usuario);
  const eventId = await criaEvento(app, loja);

  await tria(token, eventId, { decisao: "falso_positivo" });
  const correcao = await tria(token, eventId, { decisao: "confirmado" });

  assert.equal(correcao.statusCode, 201);
  assert.equal(correcao.json().evento.triagem.decisao, "confirmado");
  assert.equal(correcao.json().evento.triagem.revisada, true);

  const linhas = await prismaTeste.triagem.findMany({
    where: { eventId },
    orderBy: { seq: "asc" },
  });
  assert.equal(linhas.length, 2, "a decisão anterior tem que continuar registrada");
  assert.equal(linhas[0]?.decisao, "falso_positivo");
});

test("evento de outro tenant -> 404, e nada é gravado (NFR-6)", async () => {
  // 404 e não 403: além de o tenant nem chegar a ser lido, responder 403 confirmaria que
  // aquele event_id existe em alguma rede -- e o event_id viaja em log e em payload.
  const alheia = await semeiaLoja();
  const minha = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: minha.tenantId,
    lojas: { [minha.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, alheia);

  const resposta = await tria(tokenDe(usuario), eventId, { decisao: "confirmado" });

  assert.equal(resposta.statusCode, 404);
  assert.equal(await prismaTeste.triagem.count(), 0);
});

test("loja do mesmo tenant sem vínculo -> 403", async () => {
  const loja = await semeiaLoja();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, outra);

  const resposta = await tria(tokenDe(usuario), eventId, { decisao: "confirmado" });

  assert.equal(resposta.statusCode, 403);
});

test("evento inexistente -> 404", async () => {
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });

  const resposta = await tria(tokenDe(usuario), randomUUID(), { decisao: "confirmado" });

  assert.equal(resposta.statusCode, 404);
});

test("decisão fora das três do §4.5 -> 400", async () => {
  // O enum não é decoração: `provavelmente` ou `sim` entrando no banco quebraria toda
  // leitura agregada de falso positivo depois, e o conserto seria migração de dados.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, loja);
  const token = tokenDe(usuario);

  assert.equal((await tria(token, eventId, { decisao: "talvez" })).statusCode, 400);
  assert.equal((await tria(token, eventId, {})).statusCode, 400);
  assert.equal(await prismaTeste.triagem.count(), 0);
});

test("sessão de versão antiga é recusada (senha trocada) -> 401", async () => {
  // É o que faz "troquei a senha porque vazou" valer agora, e não quando o token vencer
  // 12 h depois. Sem tabela de sessão: o token carrega a versão que existia quando foi
  // assinado, e o cadastro diz qual é a atual.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, loja);
  const tokenAntigo = tokenDe(usuario, 0);
  await prismaTeste.usuario.update({
    where: { id: usuario.usuarioId },
    data: { tokenVersao: 1 },
  });

  const resposta = await tria(tokenAntigo, eventId, { decisao: "confirmado" });

  assert.equal(resposta.statusCode, 401);
});

test("usuário desativado depois do login perde o acesso na requisição seguinte", async () => {
  // O JWT poupa a tabela de sessão, não o SELECT -- papel e lojas vêm do banco a cada
  // requisição de qualquer jeito. É isso que faz desativar alguém valer na hora, que é o
  // que se espera de quem foi demitido hoje.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const eventId = await criaEvento(app, loja);
  const token = tokenDe(usuario);
  await prismaTeste.usuario.update({ where: { id: usuario.usuarioId }, data: { ativo: false } });

  const resposta = await tria(token, eventId, { decisao: "confirmado" });

  assert.equal(resposta.statusCode, 403);
});
