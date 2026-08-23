import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { hashToken } from "../../src/auth/agent-token.js";
import { validaRespostaRegister } from "../../src/routes/agents/register-schema.js";
import { buildTestApp } from "../helpers/build-app.js";
import { limpaBanco, prismaTeste, semeiaLoja, semeiaTokenBootstrap } from "../helpers/test-db.js";

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

function corpoRegister(bootstrapToken: string) {
  return { schema_version: 1, bootstrap_token: bootstrapToken };
}

test("token de bootstrap válido -> 201 com credencial nova que valida contra o schema", async () => {
  const { tenantId, lojaId } = await semeiaLoja();
  const { token: bootstrapToken } = await semeiaTokenBootstrap({ lojaId });

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: corpoRegister(bootstrapToken),
  });

  assert.equal(resposta.statusCode, 201);
  const documento = resposta.json();
  const validacao = validaRespostaRegister(documento);
  assert.equal(validacao.valido, true, JSON.stringify(!validacao.valido && validacao.erros));
  assert.equal(documento.tenant_id, tenantId);
  assert.equal(documento.store_id, lojaId);
  assert.ok(documento.token, "resposta precisa trazer a credencial em claro");

  const agente = await prismaTeste.agente.findUnique({
    where: { tokenHash: hashToken(documento.token) },
  });
  assert.ok(agente, "POST /v1/agents/register precisa criar um Agente com o token devolvido");
  assert.equal(agente?.lojaId, lojaId);
  assert.equal(agente?.id, documento.agent_id);
});

test("reusar o mesmo token de bootstrap -> 409, uso único é respeitado", async () => {
  const { lojaId } = await semeiaLoja();
  const { token: bootstrapToken } = await semeiaTokenBootstrap({ lojaId });
  // semeiaLoja já cria um Agente de fixture para a loja -- a contagem tem que ser
  // relativa a esse ponto de partida, não absoluta.
  const antes = await prismaTeste.agente.count({ where: { lojaId } });

  const primeira = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: corpoRegister(bootstrapToken),
  });
  assert.equal(primeira.statusCode, 201);

  const segunda = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: corpoRegister(bootstrapToken),
  });
  assert.equal(segunda.statusCode, 409);

  const depois = await prismaTeste.agente.count({ where: { lojaId } });
  assert.equal(depois - antes, 1, "reuso não pode criar um segundo Agente");
});

test("token de bootstrap expirado -> 401", async () => {
  const { lojaId } = await semeiaLoja();
  const { token: bootstrapToken } = await semeiaTokenBootstrap({
    lojaId,
    expiraEm: new Date(Date.now() - 1000),
  });

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: corpoRegister(bootstrapToken),
  });

  assert.equal(resposta.statusCode, 401);
});

test("token de bootstrap desconhecido -> 401", async () => {
  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: corpoRegister("token-que-nao-existe"),
  });

  assert.equal(resposta.statusCode, 401);
});

test("corpo sem bootstrap_token -> 400", async () => {
  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: { schema_version: 1 },
  });

  assert.equal(resposta.statusCode, 400);
});

test("schema_version diferente de 1 -> 400", async () => {
  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: { schema_version: 2, bootstrap_token: "qualquer-coisa" },
  });

  assert.equal(resposta.statusCode, 400);
});

test("duas chamadas concorrentes com o mesmo token de bootstrap: exatamente uma vence", async () => {
  const { lojaId } = await semeiaLoja();
  const { token: bootstrapToken } = await semeiaTokenBootstrap({ lojaId });
  const antes = await prismaTeste.agente.count({ where: { lojaId } });

  const [primeira, segunda] = await Promise.all([
    app.inject({
      method: "POST",
      url: "/v1/agents/register",
      payload: corpoRegister(bootstrapToken),
    }),
    app.inject({
      method: "POST",
      url: "/v1/agents/register",
      payload: corpoRegister(bootstrapToken),
    }),
  ]);

  const statusCodes = [primeira.statusCode, segunda.statusCode].sort();
  assert.deepEqual(
    statusCodes,
    [201, 409],
    `esperava uma 201 e uma 409, recebi ${JSON.stringify(statusCodes)}`,
  );

  const depois = await prismaTeste.agente.count({ where: { lojaId } });
  assert.equal(depois - antes, 1, "a corrida não pode criar dois Agente para o mesmo token");
});

test("isolamento por tenant: token de bootstrap da loja A nunca produz credencial de outra loja/tenant (NFR-6)", async () => {
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja();
  const { token: bootstrapToken } = await semeiaTokenBootstrap({ lojaId: lojaA.lojaId });

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: corpoRegister(bootstrapToken),
  });

  assert.equal(resposta.statusCode, 201);
  const documento = resposta.json();
  assert.equal(documento.store_id, lojaA.lojaId);
  assert.equal(documento.tenant_id, lojaA.tenantId);
  assert.notEqual(documento.tenant_id, lojaB.tenantId);
});
