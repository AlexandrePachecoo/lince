import assert from "node:assert/strict";
import { test } from "node:test";
import { canonicalize } from "../../src/config/canonical-json.js";

test("canonicalize produz os mesmos bytes para o mesmo conteúdo, independente da ordem de inserção das chaves", () => {
  // O ETag (etag.ts) depende disso: se a ordem de inserção mudasse o resultado, o
  // agente baixaria o documento inteiro a cada poll de 30s mesmo sem mudança nenhuma.
  const a = { b: 2, a: 1, c: { y: 2, x: 1 } };
  const b = { a: 1, c: { x: 1, y: 2 }, b: 2 };

  assert.equal(canonicalize(a), canonicalize(b));
});

test("canonicalize distingue conteúdo diferente", () => {
  assert.notEqual(canonicalize({ a: 1 }), canonicalize({ a: 2 }));
});

test("canonicalize preserva a ordem dos elementos de um array", () => {
  // Arrays não são reordenados por esta função -- ordenar semanticamente (ex.:
  // câmeras por camera_id) é responsabilidade de quem monta o documento.
  assert.notEqual(canonicalize([1, 2, 3]), canonicalize([3, 2, 1]));
});

test("canonicalize trata null e undefined em um campo como o mesmo valor", () => {
  assert.equal(canonicalize({ a: null }), canonicalize({ a: undefined }));
});

test("canonicalize é determinístico em chamadas repetidas", () => {
  const documento = { cameras: [{ camera_id: "cam1" }, { camera_id: "cam3" }], tenant_id: "dev" };
  assert.equal(canonicalize(documento), canonicalize(documento));
});
