import assert from "node:assert/strict";
import { test } from "node:test";
import type { Camera, Loja } from "@prisma/client";
import { type LojaComCameras, montaDocumento } from "../../src/config/document-builder.js";
import { validaDocumentoConfig } from "../../src/config/schema-validator.js";

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
    criadoEm: AGORA,
    atualizadoEm: AGORA,
  };
  return Object.assign(base, overrides);
}

function fakeCamera(overrides: Partial<Camera> = {}): Camera {
  const base: Camera = {
    id: "11111111-1111-1111-1111-111111111111",
    cameraId: "cam3",
    lojaId: "loja-dev",
    url: "rtsp://localhost:8554/cam3",
    detect: true,
    decode: null,
    supervision: null,
    clip: null,
    rules: {
      enabled: true,
      rule_id: "saida-sem-caixa",
      rule_version: 1,
      linha_saida: { origem: [0, 400], destino: [640, 400] },
      zonas_caixa: [
        {
          vertices: [
            [0, 410],
            [260, 410],
            [260, 478],
            [0, 478],
          ],
        },
      ],
      tempo_caixa_min_s: 3,
      vida_min_s: 1,
    },
    criadoEm: AGORA,
    atualizadoEm: AGORA,
  };
  return Object.assign(base, overrides);
}

test("documento montado por document-builder valida contra config.v1.json", () => {
  const documento = montaDocumento({ ...fakeLoja(), cameras: [fakeCamera()] });

  const resultado = validaDocumentoConfig(documento);

  assert.equal(resultado.valido, true, JSON.stringify(!resultado.valido && resultado.erros));
});

test("documento sem cameras (obrigatório, minItems 1) é recusado", () => {
  const resultado = validaDocumentoConfig({
    schema_version: 1,
    config_version: "x",
    tenant_id: "dev",
    store_id: "loja-dev",
    cameras: [],
  });

  assert.equal(resultado.valido, false);
});

test("campo desconhecido na raiz é recusado (additionalProperties: false)", () => {
  const documento = montaDocumento({ ...fakeLoja(), cameras: [fakeCamera()] });
  const adulterado = { ...documento, caminho_do_modelo: "/models/yolox_s.onnx" };

  const resultado = validaDocumentoConfig(adulterado);

  assert.equal(resultado.valido, false);
});

test("campo desconhecido dentro de rules é recusado", () => {
  const documento = montaDocumento({
    ...fakeLoja(),
    cameras: [fakeCamera({ rules: { enabled: true, campo_inventado: 1 } })],
  });

  const resultado = validaDocumentoConfig(documento);

  assert.equal(resultado.valido, false);
});
