import assert from "node:assert/strict";
import { test } from "node:test";
import type { Camera, Loja } from "@prisma/client";
import { type LojaComCameras, montaDocumento } from "../../src/config/document-builder.js";
import { calculaEtag, etagBate } from "../../src/config/etag.js";

const AGORA = new Date("2026-08-19T12:00:00.000Z");

// Object.assign, não spread: `{...base, ...overrides}` com `overrides: Partial<T>`
// faz o TS inferir toda propriedade sobrescritível como opcional no resultado (não
// sabe estaticamente se a chamada realmente passou aquela chave). Object.assign
// tipa como `T & Partial<T>`, que permanece assignável a T sem essa perda.
function fakeLoja(overrides: Partial<Loja> = {}): Loja {
  const base: Loja = {
    id: "loja-dev",
    tenantId: "dev",
    nome: "Loja de teste",
    detection: null,
    tracking: null,
    // Não entra no documento da §5.2: o fuso é de quem **lê** a métrica na nuvem, não um
    // limiar que a borda usa. O agente carimba instante UTC e pronto.
    fusoHorario: "America/Sao_Paulo",
    criadoEm: AGORA,
    atualizadoEm: AGORA,
  };
  return Object.assign(base, overrides);
}

function fakeCamera(overrides: Partial<Camera> = {}): Camera {
  const base: Camera = {
    id: "11111111-1111-1111-1111-111111111111",
    cameraId: "cam1",
    lojaId: "loja-dev",
    url: "rtsp://localhost:8554/cam1",
    detect: true,
    decode: null,
    supervision: null,
    clip: null,
    rules: null,
    criadoEm: AGORA,
    atualizadoEm: AGORA,
  };
  return Object.assign(base, overrides);
}

test("mesmo conteúdo produz o mesmo config_version/ETag", () => {
  const loja1: LojaComCameras = { ...fakeLoja(), cameras: [fakeCamera()] };
  const loja2: LojaComCameras = { ...fakeLoja(), cameras: [fakeCamera()] };

  const doc1 = montaDocumento(loja1);
  const doc2 = montaDocumento(loja2);

  assert.equal(doc1.config_version, doc2.config_version);
  assert.equal(calculaEtag(doc1), calculaEtag(doc2));
});

test("mudar um campo de uma câmera muda o config_version/ETag", () => {
  const base: LojaComCameras = {
    ...fakeLoja(),
    cameras: [fakeCamera({ rules: { tempo_caixa_min_s: 3 } })],
  };
  const alterado: LojaComCameras = {
    ...fakeLoja(),
    cameras: [fakeCamera({ rules: { tempo_caixa_min_s: 5 } })],
  };

  assert.notEqual(montaDocumento(base).config_version, montaDocumento(alterado).config_version);
});

test("adicionar ou remover uma câmera muda o config_version/ETag", () => {
  const umaCamera: LojaComCameras = { ...fakeLoja(), cameras: [fakeCamera({ cameraId: "cam1" })] };
  const duasCameras: LojaComCameras = {
    ...fakeLoja(),
    cameras: [fakeCamera({ cameraId: "cam1" }), fakeCamera({ id: "2", cameraId: "cam2" })],
  };

  assert.notEqual(
    montaDocumento(umaCamera).config_version,
    montaDocumento(duasCameras).config_version,
  );
});

test("a ordem de inserção das câmeras não afeta o config_version/ETag", () => {
  // Prova de que document-builder ordena por camera_id antes do hash -- sem isso, o
  // Postgres poderia devolver as linhas em ordens diferentes entre duas leituras do
  // mesmo conteúdo e o agente baixaria o documento inteiro à toa a cada poll.
  const ordemA: LojaComCameras = {
    ...fakeLoja(),
    cameras: [fakeCamera({ id: "1", cameraId: "cam1" }), fakeCamera({ id: "2", cameraId: "cam2" })],
  };
  const ordemB: LojaComCameras = {
    ...fakeLoja(),
    cameras: [fakeCamera({ id: "2", cameraId: "cam2" }), fakeCamera({ id: "1", cameraId: "cam1" })],
  };

  assert.equal(montaDocumento(ordemA).config_version, montaDocumento(ordemB).config_version);
});

test("etagBate reconhece um If-None-Match igual ao ETag calculado", () => {
  const documento = montaDocumento({ ...fakeLoja(), cameras: [fakeCamera()] });
  const etag = calculaEtag(documento);

  assert.ok(etagBate(etag, etag));
});

test("etagBate recusa um If-None-Match diferente", () => {
  const documento = montaDocumento({ ...fakeLoja(), cameras: [fakeCamera()] });
  const etag = calculaEtag(documento);

  assert.equal(etagBate('"etag-antigo-qualquer"', etag), false);
});

test("etagBate recusa quando não há If-None-Match", () => {
  assert.equal(etagBate(undefined, '"algum-etag"'), false);
});
