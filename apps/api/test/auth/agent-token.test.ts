import assert from "node:assert/strict";
import { before, beforeEach, test } from "node:test";
import { autenticaAgente, hashToken } from "../../src/auth/agent-token.js";
import { AgenteInativo, TokenAusente, TokenInvalido } from "../../src/auth/errors.js";
import { limpaBanco, prismaTeste, semeiaLoja } from "../helpers/test-db.js";

before(async () => {
  await limpaBanco();
});
beforeEach(async () => {
  await limpaBanco();
});

test("hashToken é determinístico e sensível a qualquer mudança de byte", () => {
  assert.equal(hashToken("abc"), hashToken("abc"));
  assert.notEqual(hashToken("abc"), hashToken("abd"));
});

test("resolve um token de agente ativo para a loja/tenant corretos", async () => {
  const { tenantId, lojaId, token } = await semeiaLoja();

  const resultado = await autenticaAgente(prismaTeste, `Bearer ${token}`);

  assert.equal(resultado.lojaId, lojaId);
  assert.equal(resultado.tenantId, tenantId);
});

test("token de uma loja nunca resolve para outro tenant (NFR-6)", async () => {
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja();

  const resultado = await autenticaAgente(prismaTeste, `Bearer ${lojaA.token}`);

  assert.notEqual(resultado.tenantId, lojaB.tenantId);
  assert.equal(resultado.tenantId, lojaA.tenantId);
});

test("lança TokenAusente quando o header Authorization está ausente", async () => {
  await assert.rejects(() => autenticaAgente(prismaTeste, undefined), TokenAusente);
});

test("lança TokenAusente quando o header não começa com 'Bearer '", async () => {
  const { token } = await semeiaLoja();
  await assert.rejects(() => autenticaAgente(prismaTeste, `Token ${token}`), TokenAusente);
});

test("lança TokenInvalido quando o token não bate com nenhum hash no banco", async () => {
  await assert.rejects(
    () => autenticaAgente(prismaTeste, "Bearer token-que-nao-existe"),
    TokenInvalido,
  );
});

test("lança AgenteInativo quando o agente existe mas está desativado", async () => {
  const { token } = await semeiaLoja({ ativo: false });

  await assert.rejects(() => autenticaAgente(prismaTeste, `Bearer ${token}`), AgenteInativo);
});
