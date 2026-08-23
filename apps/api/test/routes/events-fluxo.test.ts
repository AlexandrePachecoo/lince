import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { buildTestApp } from "../helpers/build-app.js";
import { apagaObjeto, leObjeto } from "../helpers/minio.js";
import { type LojaSemeada, limpaBanco, prismaTeste, semeiaLoja } from "../helpers/test-db.js";

// O caminho inteiro do §5.2, com Postgres e MinIO de verdade:
//
//   POST /v1/events  ->  PUT na URL pré-assinada  ->  PATCH /v1/events/{id}
//
// Os testes de rota ao lado cobrem cada peça em separado; este existe porque as
// armadilhas do clipe moram justamente nas juntas -- a chave que o POST emitiu e o
// PATCH confirma, e o PUT que o agente faz com Content-Length explícito. Nenhuma
// delas aparece num teste que só confira o formato da URL assinada.

let app: FastifyInstance;
const objetosCriados: string[] = [];

before(async () => {
  app = await buildTestApp();
});
after(async () => {
  await app.close();
  await Promise.all(objetosCriados.map(apagaObjeto));
});
beforeEach(async () => {
  await limpaBanco();
});

function corpoEvento(loja: LojaSemeada, eventId: string) {
  return {
    schema_version: 1,
    event_id: eventId,
    tenant_id: loja.tenantId,
    store_id: loja.lojaId,
    camera_id: "cam3",
    occurred_at: "2026-08-23T14:00:00.000Z",
    reported_at: "2026-08-23T14:00:02.500Z",
    source: "rule",
    rule: { id: "saida-sem-passar-no-caixa", version: 3 },
    versions: { agent: "0.1.0", model: "yolox_s-abc123", config: "v7" },
    clip: { status: "ok", duration_s: 15, size_bytes: 1_200_000 },
  };
}

// Como o agente sobe (outbox/http.py): Content-Length explícito, porque sem ele o
// cliente usa Transfer-Encoding: chunked e o R2 recusa numa URL pré-assinada.
async function sobeClipe(url: string, corpo: Buffer): Promise<number> {
  const resposta = await fetch(url, {
    method: "PUT",
    body: corpo,
    headers: { "Content-Type": "video/mp4", "Content-Length": String(corpo.byteLength) },
  });
  return resposta.status;
}

test("evento -> clipe no bucket -> confirmação: o caminho inteiro", async () => {
  const loja = await semeiaLoja();
  const eventId = randomUUID();
  const bytes = Buffer.from("bytes que fazem as vezes de um clipe de 15 s");

  const aceito = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}`, "idempotency-key": eventId },
    payload: corpoEvento(loja, eventId),
  });
  assert.equal(aceito.statusCode, 201);
  const { clip_upload_url: url, clip_object_key: chave } = aceito.json();
  objetosCriados.push(chave);

  assert.equal(await sobeClipe(url, bytes), 200);

  const confirmado = await app.inject({
    method: "PATCH",
    url: `/v1/events/${eventId}`,
    headers: { authorization: `Bearer ${loja.token}` },
    payload: { schema_version: 1, clip: { status: "ok" }, object_key: chave },
  });
  assert.equal(confirmado.statusCode, 204);

  // O objeto está no bucket, inteiro, na chave que o evento aponta. Conferir o
  // tamanho e não só o 200: um PUT que subiu zero byte também responde 200, e é
  // exatamente o que acontece quando o corpo não é reaberto entre tentativas.
  const objeto = await leObjeto(chave);
  assert.equal(objeto.status, 200);
  assert.equal(objeto.bytes, bytes.byteLength);

  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeEstado, "disponivel");
  assert.equal(evento?.clipeObjectKey, chave);
});

test("URL renovada por reenvio sobe para a mesma chave", async () => {
  // O §5.4 em ação: a URL venceu e o agente reposta o evento só para ganhar outra.
  // Se a chave mudasse, os bytes iriam para um objeto novo e o evento continuaria
  // apontando para o antigo -- e o agente, que depois de um 404 no PATCH repõe o
  // evento mas NÃO reenvia os bytes, deixaria o clipe apontando para o nada.
  const loja = await semeiaLoja();
  const eventId = randomUUID();
  const bytes = Buffer.from("segunda tentativa, mesmos bytes");

  const primeira = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
    payload: corpoEvento(loja, eventId),
  });
  const segunda = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
    payload: corpoEvento(loja, eventId),
  });

  assert.equal(segunda.statusCode, 200);
  assert.equal(segunda.json().clip_object_key, primeira.json().clip_object_key);
  objetosCriados.push(segunda.json().clip_object_key);

  assert.equal(await sobeClipe(segunda.json().clip_upload_url, bytes), 200);
  const objeto = await leObjeto(segunda.json().clip_object_key);
  assert.equal(objeto.bytes, bytes.byteLength);
});

test("evento sem clipe atravessa o caminho inteiro sem nunca tocar no bucket", async () => {
  // O corte falhou na borda. O alerta sobe do mesmo jeito e a triagem acontece sem
  // vídeo (§3.5) -- o que não pode é o evento ficar esperando um upload que ninguém
  // vai fazer.
  const loja = await semeiaLoja();
  const eventId = randomUUID();

  const aceito = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
    payload: {
      ...corpoEvento(loja, eventId),
      clip: { status: "clip_failed", error: "sem keyframe no buffer" },
    },
  });

  assert.equal(aceito.statusCode, 201);
  assert.equal(aceito.json().clip_upload_url, null);
  const evento = await prismaTeste.evento.findUnique({ where: { eventId } });
  assert.equal(evento?.clipeEstado, "indisponivel");
  assert.ok(evento?.clipeResolvidoEm, "evento sem clipe não pode ficar pendente para sempre");
});
