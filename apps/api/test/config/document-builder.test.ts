import assert from "node:assert/strict";
import { test } from "node:test";
import type { Camera, Loja } from "@prisma/client";
import { type LojaComCameras, montaDocumento } from "../../src/config/document-builder.js";

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

test("documento montado contém todos os campos obrigatórios do schema", () => {
  const loja: LojaComCameras = { ...fakeLoja(), cameras: [fakeCamera()] };

  const documento = montaDocumento(loja);

  assert.equal(documento.schema_version, 1);
  assert.equal(typeof documento.config_version, "string");
  assert.ok(documento.config_version.length > 0);
  assert.equal(documento.tenant_id, "dev");
  assert.equal(documento.store_id, "loja-dev");
  assert.equal(documento.cameras.length, 1);
});

test("bloco JSONB nulo numa câmera vira ausência no documento, não null explícito", () => {
  // O agente (config_loader.py) trata ausência como "usa meu default"; um `rules: null`
  // explícito seria um valor que o schema recusa (o campo, se presente, é objeto).
  const loja: LojaComCameras = { ...fakeLoja(), cameras: [fakeCamera({ rules: null })] };

  const documento = montaDocumento(loja);
  const [camera] = documento.cameras;
  assert.ok(camera);

  assert.ok(!("rules" in camera));
});

test("bloco JSONB presente numa câmera aparece no documento", () => {
  const rules = { enabled: true, rule_id: "saida-sem-caixa", rule_version: 1 };
  const loja: LojaComCameras = { ...fakeLoja(), cameras: [fakeCamera({ rules })] };

  const documento = montaDocumento(loja);
  const [camera] = documento.cameras;
  assert.ok(camera);

  assert.deepEqual(camera.rules, rules);
});

test("câmeras saem ordenadas por camera_id, independente da ordem de entrada", () => {
  const loja: LojaComCameras = {
    ...fakeLoja(),
    cameras: [
      fakeCamera({ id: "2", cameraId: "cam3" }),
      fakeCamera({ id: "1", cameraId: "cam1" }),
      fakeCamera({ id: "3", cameraId: "cam2" }),
    ],
  };

  const documento = montaDocumento(loja);

  assert.deepEqual(
    documento.cameras.map((c) => c.camera_id),
    ["cam1", "cam2", "cam3"],
  );
});

test("blocos de nível-loja (detection/tracking) só aparecem se não-nulos", () => {
  const semBlocos = montaDocumento({ ...fakeLoja(), cameras: [] });
  assert.ok(!("detection" in semBlocos));
  assert.ok(!("tracking" in semBlocos));

  const comBlocos = montaDocumento({
    ...fakeLoja({ detection: { enabled: true }, tracking: { min_hits: 3 } }),
    cameras: [],
  });
  assert.deepEqual(comBlocos.detection, { enabled: true });
  assert.deepEqual(comBlocos.tracking, { min_hits: 3 });
});
