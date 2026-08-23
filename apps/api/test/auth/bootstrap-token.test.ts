import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";
import { hashToken } from "../../src/auth/agent-token.js";
import { TTL_HORAS, criaTokenBootstrap, geraToken } from "../../src/auth/bootstrap-token.js";
import { limpaBanco, prismaTeste, semeiaLoja } from "../helpers/test-db.js";

beforeEach(async () => {
  await limpaBanco();
});

test("geraToken produz valores distintos a cada chamada", () => {
  const a = geraToken();
  const b = geraToken();
  assert.notEqual(a, b);
  assert.ok(a.length >= 32, "token curto demais para servir de segredo");
});

test("criaTokenBootstrap persiste o hash e o prefixo, nunca o token em claro", async () => {
  const { lojaId } = await semeiaLoja();

  const { token, expiraEm } = await criaTokenBootstrap(prismaTeste, lojaId);

  const registro = await prismaTeste.tokenBootstrap.findUnique({
    where: { tokenHash: hashToken(token) },
  });
  assert.ok(registro, "criaTokenBootstrap deveria ter gravado uma linha buscável pelo hash");
  assert.equal(registro?.tokenPrefix, token.slice(0, 8));
  assert.equal(registro?.lojaId, lojaId);
  assert.equal(registro?.usadoEm, null);
  assert.equal(registro?.expiraEm.getTime(), expiraEm.getTime());
  assert.notEqual(
    registro?.tokenHash,
    token,
    "coluna precisa guardar o hash, não o token em claro",
  );
});

test("criaTokenBootstrap expira TTL_HORAS à frente do momento da criação", async () => {
  const { lojaId } = await semeiaLoja();
  const antes = Date.now();

  const { expiraEm } = await criaTokenBootstrap(prismaTeste, lojaId);

  const depois = Date.now();
  const ttlMs = TTL_HORAS * 60 * 60 * 1000;
  assert.ok(expiraEm.getTime() >= antes + ttlMs);
  assert.ok(expiraEm.getTime() <= depois + ttlMs);
});
