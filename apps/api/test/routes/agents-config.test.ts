import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { validaDocumentoConfig } from "../../src/config/schema-validator.js";
import { buildTestApp } from "../helpers/build-app.js";
import { limpaBanco, prismaTeste, semeiaLoja } from "../helpers/test-db.js";

const RULES_CAM3 = {
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
};

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

test("sem header Authorization -> 401", async () => {
  const resposta = await app.inject({ method: "GET", url: "/v1/agents/config" });
  assert.equal(resposta.statusCode, 401);
});

test("token inválido -> 401", async () => {
  const resposta = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: "Bearer token-que-nao-existe" },
  });
  assert.equal(resposta.statusCode, 401);
});

test("agente inativo -> 403", async () => {
  const { token } = await semeiaLoja({ ativo: false, cameras: [{ cameraId: "cam1" }] });

  const resposta = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}` },
  });

  assert.equal(resposta.statusCode, 403);
});

test("token válido, sem If-None-Match -> 200 com corpo que valida contra config.v1.json", async () => {
  const { token, tenantId, lojaId } = await semeiaLoja({
    cameras: [{ cameraId: "cam1" }, { cameraId: "cam3", rules: RULES_CAM3 }],
  });

  const resposta = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}` },
  });

  assert.equal(resposta.statusCode, 200);
  assert.match(String(resposta.headers["content-type"]), /application\/json/);
  assert.ok(resposta.headers.etag, "resposta 200 precisa ter header ETag");

  const documento = resposta.json();
  const validacao = validaDocumentoConfig(documento);
  assert.equal(validacao.valido, true, JSON.stringify(!validacao.valido && validacao.erros));
  assert.equal(documento.tenant_id, tenantId);
  assert.equal(documento.store_id, lojaId);
  assert.equal(documento.cameras.length, 2);
});

test("If-None-Match igual ao ETag atual -> 304 sem corpo", async () => {
  const { token } = await semeiaLoja({ cameras: [{ cameraId: "cam1" }] });

  const primeira = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}` },
  });
  const etag = String(primeira.headers.etag);

  const segunda = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}`, "if-none-match": etag },
  });

  assert.equal(segunda.statusCode, 304);
  assert.equal(segunda.rawPayload.length, 0, "304 nunca pode ter corpo -- é o caminho SEM_MUDANCA");
  assert.equal(segunda.headers.etag, etag);
});

test("If-None-Match desatualizado -> 200 com o documento atual, nunca 304", async () => {
  const { token } = await semeiaLoja({ cameras: [{ cameraId: "cam1" }] });

  const resposta = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}`, "if-none-match": '"etag-velho-qualquer"' },
  });

  assert.equal(resposta.statusCode, 200);
});

test("alterar uma câmera no banco muda o ETag, e o If-None-Match antigo volta a dar 200", async () => {
  const { token, lojaId } = await semeiaLoja({ cameras: [{ cameraId: "cam1" }] });

  const primeira = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}` },
  });
  const etagAntigo = String(primeira.headers.etag);

  await prismaTeste.camera.update({
    where: { lojaId_cameraId: { lojaId, cameraId: "cam1" } },
    data: { url: "rtsp://localhost:8554/outra-url" },
  });

  const segunda = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}` },
  });
  assert.notEqual(String(segunda.headers.etag), etagAntigo);

  const terceira = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}`, "if-none-match": etagAntigo },
  });
  assert.equal(terceira.statusCode, 200, "ETag desatualizado nunca pode dar 304");
});

test("nenhum campo fora do schema vaza na resposta (token_hash, ids internos, timestamps do Prisma)", async () => {
  const { token } = await semeiaLoja({
    detection: { enabled: true },
    tracking: { min_hits: 3 },
    cameras: [{ cameraId: "cam3", rules: RULES_CAM3 }],
  });

  const resposta = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${token}` },
  });
  const documento = resposta.json();

  // additionalProperties:false já é reforçado em todo nível pelo schema (ver
  // schema-validator.test.ts); aqui a asserção é específica sobre os campos que
  // seriam a regressão mais grave -- vazar segredo ou id interno do Prisma.
  assert.ok(!("token_hash" in documento));
  assert.ok(!("criadoEm" in documento));
  assert.ok(!("id" in documento.cameras[0]));
  assert.ok(!("lojaId" in documento.cameras[0]));
  assert.equal(validaDocumentoConfig(documento).valido, true);
});

test("isolamento por tenant: agente da loja B nunca vê câmera da loja A (NFR-6)", async () => {
  const lojaA = await semeiaLoja({ cameras: [{ cameraId: "cam-a" }] });
  await semeiaLoja({ cameras: [{ cameraId: "cam-b" }] });

  const resposta = await app.inject({
    method: "GET",
    url: "/v1/agents/config",
    headers: { authorization: `Bearer ${lojaA.token}` },
  });
  const documento = resposta.json();

  assert.equal(documento.store_id, lojaA.lojaId);
  assert.equal(documento.cameras.length, 1);
  assert.equal(documento.cameras[0].camera_id, "cam-a");
});
