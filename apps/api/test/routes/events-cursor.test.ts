import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";
import { RequisicaoInvalida } from "../../src/auth/errors.js";
import { codificaCursor, decodificaCursor } from "../../src/routes/events/cursor.js";

test("ida e volta preserva o milissegundo", () => {
  // Sem o milissegundo, dois eventos dentro do mesmo segundo ficariam do mesmo lado da
  // comparação: um deles apareceria duas vezes na fila ou não apareceria nenhuma. A 3
  // fps, duas travessias no mesmo segundo não são hipótese remota.
  const posicao = { ocorridoEm: new Date("2026-08-23T14:00:00.123Z"), eventId: randomUUID() };

  const volta = decodificaCursor(codificaCursor(posicao));

  assert.equal(volta.ocorridoEm.getTime(), posicao.ocorridoEm.getTime());
  assert.equal(volta.eventId, posicao.eventId);
});

test("cursor corrompido é recusado, nunca interpretado pela metade", () => {
  // Um cursor meio lido viraria uma posição plausível e errada: a fila pularia um trecho
  // sem erro nenhum, e ninguém descobre que decidiu menos do que devia.
  for (const ruim of [
    "",
    "nao-e-base64url!!",
    Buffer.from("sem-separador").toString("base64url"),
  ]) {
    assert.throws(() => decodificaCursor(ruim), RequisicaoInvalida, `cursor: ${ruim}`);
  }
});

test("data inválida dentro de um cursor bem formado também é recusada", () => {
  const forjado = Buffer.from("ontem|algum-id").toString("base64url");

  assert.throws(() => decodificaCursor(forjado), RequisicaoInvalida);
});
