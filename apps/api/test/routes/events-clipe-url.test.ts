import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { emiteToken } from "../../src/auth/jwt.js";
import { SEGREDO_TESTE, buildTestApp } from "../helpers/build-app.js";
import { apagaObjeto, urlCrua } from "../helpers/minio.js";
import {
  type LojaSemeada,
  type UsuarioSemeado,
  limpaBanco,
  prismaTeste,
  semeiaLoja,
  semeiaUsuario,
} from "../helpers/test-db.js";

// A leitura do clipe pelo dashboard, com MinIO de verdade. Vale o caminho completo
// (POST -> PUT -> PATCH -> GET clip-url -> GET no bucket) porque o que se quer provar
// aqui é que a URL emitida **lê os bytes que o agente subiu** -- uma URL bem formada
// contra a chave errada é indistinguível de uma certa até alguém tentar assistir.

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

function tokenDe(usuario: UsuarioSemeado): string {
  return emiteToken(SEGREDO_TESTE, {
    usuarioId: usuario.usuarioId,
    tenantId: usuario.tenantId,
    tokenVersao: 0,
    expiraEmS: 3600,
  });
}

async function postaEvento(loja: LojaSemeada, clipStatus: "ok" | "clip_failed" = "ok") {
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
      camera_id: "cam3",
      occurred_at: "2026-08-23T14:00:00.000Z",
      reported_at: "2026-08-23T14:00:02.500Z",
      source: "rule",
      rule: { id: "saida-sem-passar-no-caixa", version: 3 },
      versions: { agent: "0.1.0", model: "yolox_s-abc123", config: "v7" },
      clip:
        clipStatus === "ok"
          ? { status: "ok", duration_s: 15, size_bytes: 1_200_000 }
          : { status: "clip_failed", error: "buffer despejado pelo teto de disco" },
    },
  });
  assert.ok(resposta.statusCode < 300, `POST falhou com ${resposta.statusCode}`);
  return {
    eventId,
    corpo: resposta.json() as { clip_upload_url: string; clip_object_key: string },
  };
}

/** Deixa o evento com o clipe realmente no bucket e confirmado. */
async function comClipeNoBucket(loja: LojaSemeada, bytes: Buffer): Promise<string> {
  const { eventId, corpo } = await postaEvento(loja);
  objetosCriados.push(corpo.clip_object_key);

  const put = await fetch(corpo.clip_upload_url, {
    method: "PUT",
    body: bytes,
    headers: { "Content-Length": String(bytes.byteLength) },
  });
  assert.equal(put.status, 200, "o PUT no bucket precisa ter funcionado");

  const patch = await app.inject({
    method: "PATCH",
    url: `/v1/events/${eventId}`,
    headers: { authorization: `Bearer ${loja.token}` },
    payload: { schema_version: 1, clip: { status: "ok" }, object_key: corpo.clip_object_key },
  });
  assert.equal(patch.statusCode, 204);
  return eventId;
}

function pedeUrl(token: string, eventId: string) {
  return app.inject({
    method: "GET",
    url: `/v1/events/${eventId}/clip-url`,
    headers: { authorization: `Bearer ${token}` },
  });
}

async function lojaComGerente() {
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  return { loja, usuario };
}

test("clipe disponível -> URL que realmente lê os bytes que o agente subiu", async () => {
  const { loja, usuario } = await lojaComGerente();
  const bytes = Buffer.from("bytes que fazem as vezes de um clipe de 15 s");
  const eventId = await comClipeNoBucket(loja, bytes);

  const resposta = await pedeUrl(tokenDe(usuario), eventId);

  assert.equal(resposta.statusCode, 200);
  const corpo = resposta.json();
  assert.equal(corpo.event_id, eventId);
  assert.equal(corpo.expires_in_s, 300);

  // Buscar de verdade, e conferir o tamanho: uma URL assinada contra a chave errada
  // responde 404, e uma contra bucket errado também -- os dois casos parecem credencial
  // inválida se ninguém tentar assistir.
  const video = await fetch(corpo.url);
  assert.equal(video.status, 200);
  assert.equal((await video.arrayBuffer()).byteLength, bytes.byteLength);
});

test("cada emissão vira uma linha de auditoria com quem, qual evento e de onde", async () => {
  // Depois que a URL sai daqui o download acontece direto no bucket, fora do alcance da
  // API: não há como registrar "fulano baixou". A emissão é o único instante em que a
  // nuvem sabe quem pediu, e é o que resta para responder a pergunta do R-8.
  const { loja, usuario } = await lojaComGerente();
  const eventId = await comClipeNoBucket(loja, Buffer.from("clipe"));

  await pedeUrl(tokenDe(usuario), eventId);
  await pedeUrl(tokenDe(usuario), eventId);

  const linhas = await prismaTeste.auditoria.findMany({ where: { eventId } });
  assert.equal(linhas.length, 2, "pedir a URL de novo é um acesso novo, e conta como tal");
  assert.equal(linhas[0]?.acao, "clipe_url_emitida");
  assert.equal(linhas[0]?.usuarioId, usuario.usuarioId);
  assert.equal(linhas[0]?.usuarioEmail, usuario.email, "a trilha guarda o e-mail de então");
  assert.equal(linhas[0]?.tenantId, usuario.tenantId);
  assert.ok(linhas[0]?.ip, "sem 'de onde' a trilha não fecha a pergunta do §6");
});

test("sem assinatura o bucket recusa (NFR-7)", async () => {
  // O clipe é dado pessoal (R-9). Se o objeto fosse legível por quem soubesse a chave, o
  // event_id -- que viaja em log e em payload -- viraria credencial.
  const { loja } = await lojaComGerente();
  const eventId = await comClipeNoBucket(loja, Buffer.from("clipe"));
  const evento = await prismaTeste.evento.findUniqueOrThrow({ where: { eventId } });

  const semAssinatura = await fetch(urlCrua(evento.clipeObjectKey ?? ""));

  assert.ok(semAssinatura.status >= 400, `bucket serviu o objeto cru com ${semAssinatura.status}`);
});

test("clipe ainda subindo -> 409 dizendo que é para esperar", async () => {
  // `pendente` é o estado que o contrato do agente não nomeia. Sem distingui-lo de
  // `indisponivel`, o dashboard não sabe se mostra "processando" ou libera a decisão --
  // e na dúvida deixa o gerente esperando um vídeo que talvez nunca venha.
  const { loja, usuario } = await lojaComGerente();
  const { eventId } = await postaEvento(loja);

  const resposta = await pedeUrl(tokenDe(usuario), eventId);

  assert.equal(resposta.statusCode, 409);
  assert.match(resposta.json().erro, /subindo/);
});

test("clipe que não vai existir -> 409 dizendo que não adianta esperar", async () => {
  // É este 409 que libera o triador a decidir sem vídeo. Sem ele, um evento com corte
  // falhado ficaria para sempre "carregando" na tela e nunca seria triado -- virando
  // dívida operacional invisível, o oposto do que o §4.5 quer.
  const { loja, usuario } = await lojaComGerente();
  const { eventId } = await postaEvento(loja, "clip_failed");

  const resposta = await pedeUrl(tokenDe(usuario), eventId);

  assert.equal(resposta.statusCode, 409);
  assert.match(resposta.json().erro, /não haverá vídeo/);
});

test("evento de outro tenant -> 404 e nenhuma linha de auditoria (NFR-6)", async () => {
  // Auditar a tentativa recusada parece zelo e é vazamento pelo lado de dentro: a linha
  // ficaria no tenant de quem pediu, apontando para um event_id de outra rede.
  const alheia = await semeiaLoja();
  const minha = await lojaComGerente();
  const eventId = await comClipeNoBucket(alheia, Buffer.from("clipe alheio"));

  const resposta = await pedeUrl(tokenDe(minha.usuario), eventId);

  assert.equal(resposta.statusCode, 404);
  assert.equal(await prismaTeste.auditoria.count(), 0);
});

test("loja do mesmo tenant sem vínculo -> 403", async () => {
  const loja = await semeiaLoja();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const eventId = await comClipeNoBucket(outra, Buffer.from("clipe da outra loja"));

  const resposta = await pedeUrl(tokenDe(usuario), eventId);

  assert.equal(resposta.statusCode, 403);
  assert.equal(await prismaTeste.auditoria.count(), 0);
});

test("a validade configurada é a mesma na resposta e na assinatura", async () => {
  // Dois números que precisam ser um só. Se a resposta dissesse 300 e a assinatura
  // valesse 30, o dashboard confiaria numa URL já morta e o gerente veria o player
  // falhar sem explicação.
  const curto = await buildTestApp({ leituraExpiraEmS: 60 });
  try {
    const { loja, usuario } = await lojaComGerente();
    const eventId = await comClipeNoBucket(loja, Buffer.from("clipe"));

    const resposta = await curto.inject({
      method: "GET",
      url: `/v1/events/${eventId}/clip-url`,
      headers: { authorization: `Bearer ${tokenDe(usuario)}` },
    });

    assert.equal(resposta.statusCode, 200);
    assert.equal(resposta.json().expires_in_s, 60);
    const url = new URL(resposta.json().url);
    assert.equal(url.searchParams.get("X-Amz-Expires"), "60");
  } finally {
    await curto.close();
  }
});
