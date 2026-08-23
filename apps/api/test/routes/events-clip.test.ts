import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { buildTestApp } from "../helpers/build-app.js";
import { type LojaSemeada, limpaBanco, prismaTeste, semeiaLoja } from "../helpers/test-db.js";

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

// Cria o evento pela rota de verdade: o PATCH só faz sentido sobre um evento que
// passou pelo POST, e semear a linha na mão esconderia justamente a parte que os dois
// combinam entre si -- a chave do objeto emitida por um e conferida pelo outro.
async function eventoAceito(
  loja: LojaSemeada,
  overrides: Record<string, unknown> = {},
): Promise<string> {
  const eventId = randomUUID();
  const resposta = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
    payload: {
      schema_version: 1,
      event_id: eventId,
      tenant_id: loja.tenantId,
      store_id: loja.lojaId,
      camera_id: "cam1",
      occurred_at: "2026-08-23T14:00:00.000Z",
      reported_at: "2026-08-23T14:00:02.500Z",
      source: "rule",
      rule: { id: "saida-sem-passar-no-caixa", version: 3 },
      versions: { agent: "0.1.0", model: "yolox_s-abc123", config: "v7" },
      clip: { status: "ok", duration_s: 15, size_bytes: 1_200_000 },
      ...overrides,
    },
  });
  assert.ok(resposta.statusCode < 300, `POST falhou com ${resposta.statusCode}`);
  return eventId;
}

function patch(token: string, eventId: string, corpo: unknown) {
  return app.inject({
    method: "PATCH",
    url: `/v1/events/${eventId}`,
    headers: { authorization: `Bearer ${token}` },
    payload: corpo as Record<string, unknown>,
  });
}

test("sem header Authorization -> 401", async () => {
  const resposta = await app.inject({
    method: "PATCH",
    url: `/v1/events/${randomUUID()}`,
    payload: { schema_version: 1, clip: { status: "ok" } },
  });
  assert.equal(resposta.statusCode, 401);
});

test("agente inativo -> 403", async () => {
  const loja = await semeiaLoja({ ativo: false });
  const resposta = await patch(loja.token, randomUUID(), {
    schema_version: 1,
    clip: { status: "ok" },
  });
  assert.equal(resposta.statusCode, 403);
});

test("clip ok -> 204, e o clipe fica disponível", async () => {
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja);

  const resposta = await patch(loja.token, eventId, {
    schema_version: 1,
    clip: { status: "ok" },
    object_key: `clipes/${loja.tenantId}/2026/08/23/${eventId}.mp4`,
  });

  assert.equal(resposta.statusCode, 204);
  assert.equal(resposta.rawPayload.length, 0, "204 nunca pode ter corpo");
  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeEstado, "disponivel");
  assert.equal(evento?.clipeErro, null);
  assert.ok(evento?.clipeResolvidoEm);
});

test("clip_failed -> 204, clipe indisponível e o motivo gravado", async () => {
  // O evento continua válido e triável -- o que se perde é o vídeo (§3.5). E o motivo
  // importa: "teto de disco" e "câmera reconectou" pedem ações diferentes do técnico.
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja);

  const resposta = await patch(loja.token, eventId, {
    schema_version: 1,
    clip: { status: "clip_failed", error: "clipe despejado pelo teto de disco" },
  });

  assert.equal(resposta.statusCode, 204);
  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeEstado, "indisponivel");
  assert.equal(evento?.clipeErro, "clipe despejado pelo teto de disco");
});

test("o mesmo PATCH duas vezes -> 204 nas duas", async () => {
  // O agente confirma de novo quando não viu a resposta anterior. Um segundo PATCH
  // que respondesse erro pararia a fila de clipes num item que já está resolvido.
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja);
  const corpo = { schema_version: 1, clip: { status: "ok" } };

  assert.equal((await patch(loja.token, eventId, corpo)).statusCode, 204);
  assert.equal((await patch(loja.token, eventId, corpo)).statusCode, 204);
});

test("evento desconhecido -> 404", async () => {
  // 404 é combinado com o agente: classify_response traduz em UNKNOWN_EVENT, e ele
  // repõe o evento inteiro na fila em vez de desistir do clipe (outbox/sender.py).
  const loja = await semeiaLoja();
  const resposta = await patch(loja.token, randomUUID(), {
    schema_version: 1,
    clip: { status: "ok" },
  });
  assert.equal(resposta.statusCode, 404);
});

test("evento de outra loja -> 404, e o evento não é tocado (NFR-6)", async () => {
  // 404 e não 403: além de não confirmar que a linha existe, é o único status que leva
  // o agente a fazer algo sensato. A recusa por escopo acontece no POST, num lugar só.
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja();
  const eventId = await eventoAceito(lojaB);

  const resposta = await patch(lojaA.token, eventId, {
    schema_version: 1,
    clip: { status: "clip_failed", error: "invasão" },
  });

  assert.equal(resposta.statusCode, 404);
  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeEstado, "pendente");
});

test("object_key divergente é ignorado: vale a chave que a API emitiu", async () => {
  // O campo existe no contrato para conciliação (event-clip.v1.json). Gravar o valor
  // do cliente deixaria uma credencial de loja apontar o evento para qualquer objeto
  // do bucket -- inclusive o clipe de outra loja.
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja);

  const resposta = await patch(loja.token, eventId, {
    schema_version: 1,
    clip: { status: "ok" },
    object_key: "clipes/outro-tenant/2026/08/23/objeto-alheio.mp4",
  });

  assert.equal(resposta.statusCode, 204);
  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeObjectKey, `clipes/${loja.tenantId}/2026/08/23/${eventId}.mp4`);
});

test("confirmar upload de evento que subiu sem clipe -> 400", async () => {
  // Estado incoerente: o POST declarou clip_failed, então nenhuma URL foi emitida e
  // não há objeto no R2. Aceitar marcaria o clipe como disponível numa chave que não
  // existe, e o sintoma apareceria semanas depois como player quebrado na triagem.
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja, {
    clip: { status: "clip_failed", error: "corte falhou" },
  });

  const resposta = await patch(loja.token, eventId, { schema_version: 1, clip: { status: "ok" } });

  assert.equal(resposta.statusCode, 400);
  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeEstado, "indisponivel");
});

test("corpo sem o bloco clip -> 400", async () => {
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja);

  const resposta = await patch(loja.token, eventId, { schema_version: 1 });

  assert.equal(resposta.statusCode, 400);
  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeEstado, "pendente");
});

test("status de clipe fora do enum -> 400", async () => {
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja);

  const resposta = await patch(loja.token, eventId, {
    schema_version: 1,
    clip: { status: "quase" },
  });

  assert.equal(resposta.statusCode, 400);
});

test("campo desconhecido no corpo -> 400", async () => {
  // event-clip.v1.json é additionalProperties: false. Um campo que a API ignora em
  // silêncio é um contrato que já divergiu e ninguém viu.
  const loja = await semeiaLoja();
  const eventId = await eventoAceito(loja);

  const resposta = await patch(loja.token, eventId, {
    schema_version: 1,
    clip: { status: "ok" },
    clip_url: "https://exemplo/clipe.mp4",
  });

  assert.equal(resposta.statusCode, 400);
});
