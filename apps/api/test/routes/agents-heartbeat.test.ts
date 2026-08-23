import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { buildTestApp } from "../helpers/build-app.js";
import { limpaBanco, prismaTeste, semeiaLoja } from "../helpers/test-db.js";

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

function corpoHeartbeat(overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    agent_version: "0.0.0",
    model_version: "yolox_s-abc123",
    config_version: "v7",
    queue: { depth: 0, oldest_age_s: 0, bytes: 0 },
    cameras: [
      {
        camera_id: "cam1",
        status: "ok",
        decode_fps: 3.0,
        inference_fps: 2.9,
        dropped_frames: 0,
        last_frame_at: "2026-08-23T14:00:00.000Z",
      },
    ],
    uptime_s: 120.5,
    restarts: 0,
    clock_skew_s: 0.02,
    ...overrides,
  };
}

test("sem header Authorization -> 401", async () => {
  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    payload: corpoHeartbeat(),
  });
  assert.equal(resposta.statusCode, 401);
});

test("token inválido -> 401", async () => {
  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: "Bearer token-que-nao-existe" },
    payload: corpoHeartbeat(),
  });
  assert.equal(resposta.statusCode, 401);
});

test("agente inativo -> 403", async () => {
  const { token } = await semeiaLoja({ ativo: false });

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpoHeartbeat(),
  });

  assert.equal(resposta.statusCode, 403);
});

test("corpo válido -> 204 sem corpo, e o Agente grava heartbeatEm/heartbeat", async () => {
  const { token, lojaId } = await semeiaLoja();
  const antes = new Date();

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpoHeartbeat(),
  });

  assert.equal(resposta.statusCode, 204);
  assert.equal(resposta.rawPayload.length, 0, "204 nunca pode ter corpo");

  const agente = await prismaTeste.agente.findFirst({ where: { lojaId } });
  assert.ok(agente?.heartbeatEm, "heartbeatEm precisa ser gravado no heartbeat aceito");
  assert.ok(
    agente.heartbeatEm.getTime() >= antes.getTime(),
    "heartbeatEm precisa ser o instante do recebimento, não algo do payload",
  );
  assert.deepEqual(agente.heartbeat, corpoHeartbeat());
});

test("cameras vazio é válido -- câmera sem supervisor em pé ainda manda telemetria do agente", async () => {
  const { token } = await semeiaLoja();

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpoHeartbeat({ cameras: [] }),
  });

  assert.equal(resposta.statusCode, 204);
});

test("model_version e config_version null são válidos -- agente sem modelo no disco ou sem config aplicada ainda", async () => {
  const { token } = await semeiaLoja();

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpoHeartbeat({ model_version: null, config_version: null }),
  });

  assert.equal(resposta.statusCode, 204);
});

test("host omitido é válido -- é o único bloco opcional do documento", async () => {
  const { token } = await semeiaLoja();
  const corpo = corpoHeartbeat();

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpo,
  });

  assert.equal(resposta.statusCode, 204);
});

test("campo obrigatório ausente -> 400, e o Agente não é atualizado", async () => {
  const { token, lojaId } = await semeiaLoja();
  const corpo = corpoHeartbeat() as Record<string, unknown>;
  corpo.uptime_s = undefined;

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpo,
  });

  assert.equal(resposta.statusCode, 400);
  const agente = await prismaTeste.agente.findFirst({ where: { lojaId } });
  assert.equal(agente?.heartbeatEm, null);
});

test("schema_version diferente de 1 -> 400", async () => {
  const { token } = await semeiaLoja();

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpoHeartbeat({ schema_version: 2 }),
  });

  assert.equal(resposta.statusCode, 400);
});

test("status de câmera fora do enum -> 400", async () => {
  const { token } = await semeiaLoja();
  const corpo = corpoHeartbeat({
    cameras: [
      {
        camera_id: "cam1",
        status: "quebrada",
        decode_fps: 0,
        inference_fps: 0,
        dropped_frames: 0,
        last_frame_at: null,
      },
    ],
  });

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${token}` },
    payload: corpo,
  });

  assert.equal(resposta.statusCode, 400);
});

test("isolamento por tenant: heartbeat da loja A não altera o Agente da loja B (NFR-6)", async () => {
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja();

  await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${lojaA.token}` },
    payload: corpoHeartbeat(),
  });

  const agenteB = await prismaTeste.agente.findFirst({ where: { lojaId: lojaB.lojaId } });
  assert.equal(agenteB?.heartbeatEm, null);
});
