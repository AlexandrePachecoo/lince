import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
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

interface Identidade {
  tenantId: string;
  lojaId: string;
}

function corpoEvento(identidade: Identidade, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    event_id: randomUUID(),
    tenant_id: identidade.tenantId,
    store_id: identidade.lojaId,
    camera_id: "cam1",
    occurred_at: "2026-08-23T14:00:00.000Z",
    reported_at: "2026-08-23T14:00:02.500Z",
    source: "rule",
    rule: { id: "saida-sem-passar-no-caixa", version: 3 },
    versions: { agent: "0.1.0", model: "yolox_s-abc123", config: "v7" },
    clip: {
      status: "ok",
      duration_s: 15.0,
      size_bytes: 1_200_000,
      pre_roll_s: 5.0,
      post_roll_s: 10.0,
      pre_roll_requested_s: 5.0,
      post_roll_requested_s: 10.0,
      fragments: 30,
      truncated_pre_roll: false,
      truncated_post_roll: false,
      session_lost: false,
      error: null,
    },
    ...overrides,
  };
}

async function postaEvento(token: string, corpo: Record<string, unknown>) {
  return app.inject({
    method: "POST",
    url: "/v1/events",
    headers: {
      authorization: `Bearer ${token}`,
      "idempotency-key": corpo.event_id as string,
    },
    payload: corpo,
  });
}

test("sem header Authorization -> 401", async () => {
  const resposta = await app.inject({
    method: "POST",
    url: "/v1/events",
    payload: corpoEvento({ tenantId: "t", lojaId: "l" }),
  });
  assert.equal(resposta.statusCode, 401);
});

test("agente inativo -> 403", async () => {
  const loja = await semeiaLoja({ ativo: false });
  const resposta = await postaEvento(loja.token, corpoEvento(loja));
  assert.equal(resposta.statusCode, 403);
});

test("evento novo -> 201 com URL de upload e a chave do objeto", async () => {
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);

  const resposta = await postaEvento(loja.token, corpo);

  assert.equal(resposta.statusCode, 201);
  const body = resposta.json();
  assert.equal(body.schema_version, 1);
  assert.equal(body.event_id, corpo.event_id);
  assert.ok(body.clip_upload_url, "sem URL o clipe fica preso na fila da loja para sempre");
  assert.equal(body.clip_object_key, `clipes/${loja.tenantId}/2026/08/23/${corpo.event_id}.mp4`);
  assert.equal(body.clip_upload_expires_in_s, 900);
});

test("o evento é gravado com o que a triagem e a métrica de falso positivo precisam", async () => {
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);

  await postaEvento(loja.token, corpo);

  const evento = await prismaTeste.evento.findUnique({ where: { eventId: corpo.event_id } });
  assert.ok(evento);
  assert.equal(evento.tenantId, loja.tenantId);
  assert.equal(evento.lojaId, loja.lojaId);
  assert.equal(evento.cameraId, "cam1");
  assert.equal(evento.source, "rule");
  // Regra achatada em colunas: é por (regra, versão) que se agrupa taxa de falso
  // positivo (R-1), e métrica que se vai agrupar não pode morar dentro de um JSON.
  assert.equal(evento.regraId, "saida-sem-passar-no-caixa");
  assert.equal(evento.regraVersao, 3);
  assert.equal(evento.ocorridoEm.toISOString(), "2026-08-23T14:00:00.000Z");
  assert.equal(evento.reportadoEm.toISOString(), "2026-08-23T14:00:02.500Z");
  assert.equal(evento.clipeEstado, "pendente");
});

test("o agente que mandou fica registrado no evento", async () => {
  // Sem isso não há como investigar uma loja com dois boxes em versões diferentes
  // mandando eventos que discordam.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);
  await postaEvento(loja.token, corpo);

  const evento = await prismaTeste.evento.findUnique({ where: { eventId: corpo.event_id } });
  const agente = await prismaTeste.agente.findFirst({ where: { lojaId: loja.lojaId } });
  assert.equal(evento?.agenteId, agente?.id);
});

test("source manual não pode virar regra: regraId/regraVersao ficam nulos", async () => {
  // `manual` é o andaime de gatilho da instalação. Se ele contasse como regra, um
  // instalador testando 40 vezes numa tarde afundaria a métrica de falso positivo da
  // câmera (NFR-2, R-1) e a loja apareceria como calibrada errado.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja, { source: "manual", rule: null });

  await postaEvento(loja.token, corpo);

  const evento = await prismaTeste.evento.findUnique({ where: { eventId: corpo.event_id } });
  assert.equal(evento?.source, "manual");
  assert.equal(evento?.regraId, null);
  assert.equal(evento?.regraVersao, null);
});

test("reenvio -> 200, mesma chave e URL de upload nova", async () => {
  // O caminho de renovação do §5.4: a URL venceu, o agente reposta o evento só para
  // ganhar outra. Um 200 sem clip_upload_url deixaria o clipe preso na fila para
  // sempre (event-accepted.v1.json diz isso na própria descrição).
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);

  const primeira = await postaEvento(loja.token, corpo);
  const segunda = await postaEvento(loja.token, corpo);

  assert.equal(primeira.statusCode, 201);
  assert.equal(segunda.statusCode, 200);
  assert.ok(segunda.json().clip_upload_url);
  assert.equal(segunda.json().clip_object_key, primeira.json().clip_object_key);
});

test("reenvio não duplica o evento", async () => {
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);

  await postaEvento(loja.token, corpo);
  await postaEvento(loja.token, corpo);

  assert.equal(await prismaTeste.evento.count({ where: { eventId: corpo.event_id } }), 1);
});

test("dois POST simultâneos do mesmo evento não criam duas linhas", async () => {
  // A borda reenvia sem saber se a resposta anterior se perdeu no caminho; sob link
  // ruim as duas chamadas podem estar em voo ao mesmo tempo. Se a idempotência
  // dependesse de checar-e-então-inserir, aqui nasceriam dois eventos -- e o gerente
  // veria o mesmo furto duas vezes na fila de triagem.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);

  const respostas = await Promise.all([
    postaEvento(loja.token, corpo),
    postaEvento(loja.token, corpo),
  ]);

  for (const resposta of respostas) {
    assert.ok(resposta.statusCode < 300, `status inesperado: ${resposta.statusCode}`);
  }
  assert.equal(await prismaTeste.evento.count({ where: { eventId: corpo.event_id } }), 1);
});

test("reenvio depois do PATCH não rebaixa o clipe já confirmado", async () => {
  // O bug que o POST idempotente convida: o payload congelado na borda continua
  // dizendo `clip.status: ok` (o desfecho do CORTE) muito depois de o PATCH ter
  // registrado o upload. Reescrever o evento no reenvio devolveria o clipe para
  // "pendente", e a triagem esperaria para sempre um upload que já aconteceu.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);
  await postaEvento(loja.token, corpo);

  await app.inject({
    method: "PATCH",
    url: `/v1/events/${corpo.event_id}`,
    headers: { authorization: `Bearer ${loja.token}` },
    payload: { schema_version: 1, clip: { status: "ok" } },
  });
  const reenvio = await postaEvento(loja.token, corpo);

  const evento = await prismaTeste.evento.findUnique({ where: { eventId: corpo.event_id } });
  assert.equal(evento?.clipeEstado, "disponivel");
  // E não oferece upload: não há mais nada para subir.
  assert.equal(reenvio.json().clip_upload_url, null);
});

test("evento que subiu com clip_failed -> 201 sem URL, e o motivo fica gravado", async () => {
  // O corte falhou na borda: não existe arquivo. O evento continua válido e triável
  // (§3.5), só sem vídeo -- e emitir URL para ele seria oferecer upload de nada.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja, {
    clip: { status: "clip_failed", error: "câmera reconectou no meio do corte" },
  });

  const resposta = await postaEvento(loja.token, corpo);

  assert.equal(resposta.statusCode, 201);
  assert.equal(resposta.json().clip_upload_url, null);
  assert.equal(resposta.json().clip_object_key, null);
  const evento = await prismaTeste.evento.findUnique({ where: { eventId: corpo.event_id } });
  assert.equal(evento?.clipeEstado, "indisponivel");
  assert.equal(evento?.clipeErro, "câmera reconectou no meio do corte");
  assert.ok(evento?.clipeResolvidoEm, "clipe sem desfecho pendente não pode ficar em aberto");
});

test("câmera que não está mais no cadastro é aceita", async () => {
  // Um evento pode subir seis horas depois do gatilho, porque o link caiu, e chegar
  // com a câmera já removida da configuração no dashboard. Recusar aqui mandaria o
  // agente jogar o evento na fila morta (outbox/policy.py) -- um alerta real perdido
  // por causa de uma edição de cadastro feita no meio do caminho.
  const loja = await semeiaLoja({ cameras: [{ cameraId: "cam1" }] });
  const corpo = corpoEvento(loja, { camera_id: "cam-que-foi-removida" });

  const resposta = await postaEvento(loja.token, corpo);

  assert.equal(resposta.statusCode, 201);
});

test("evento de outra loja -> 403, e nada é gravado (NFR-6)", async () => {
  // 403, e não 400: classify_response manda 400 para a fila morta e 403 para retry
  // lento. Um box provisionado com o tenant errado é erro humano de instalação, e os
  // eventos que ele manda são reais -- descartar o dia da loja é o lado caro do erro.
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja();
  const corpo = corpoEvento({ tenantId: lojaB.tenantId, lojaId: lojaB.lojaId });

  const resposta = await postaEvento(lojaA.token, corpo);

  assert.equal(resposta.statusCode, 403);
  assert.equal(await prismaTeste.evento.count(), 0);
});

test("tenant_id certo com store_id de outra loja -> 403", async () => {
  // Duas lojas do mesmo tenant. A credencial é escopada a UMA loja (§5.1), então
  // acertar o tenant não basta.
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja({ tenantId: lojaA.tenantId });
  const corpo = corpoEvento({ tenantId: lojaA.tenantId, lojaId: lojaB.lojaId });

  const resposta = await postaEvento(lojaA.token, corpo);

  assert.equal(resposta.statusCode, 403);
});

test("Idempotency-Key divergente do event_id -> 400, e nada é gravado", async () => {
  // A chave viaja nos dois lugares (outbox/http.py). Divergirem é proxy remontando
  // requisição ou cliente com bug; aceitar escolheria em silêncio qual das duas é a
  // verdade, e a escolha errada grava o evento com o id de outro.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: {
      authorization: `Bearer ${loja.token}`,
      "idempotency-key": randomUUID(),
    },
    payload: corpo,
  });

  assert.equal(resposta.statusCode, 400);
  assert.equal(await prismaTeste.evento.count(), 0);
});

test("sem Idempotency-Key o corpo basta", async () => {
  // O cabeçalho é conveniência para proxy; a chave de idempotência do §5.4 é o
  // event_id do corpo. Exigir o cabeçalho inventaria um requisito que o contrato não
  // tem, e quebraria qualquer cliente que siga só o event.v1.json.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja);

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
    payload: corpo,
  });

  assert.equal(resposta.statusCode, 201);
});

test("campo obrigatório ausente -> 400, e nada é gravado", async () => {
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja) as Record<string, unknown>;
  corpo.occurred_at = undefined;

  const resposta = await postaEvento(loja.token, corpo);

  assert.equal(resposta.statusCode, 400);
  assert.equal(await prismaTeste.evento.count(), 0);
});

test("event_id fora do formato de UUID -> 400", async () => {
  // O event_id é também o nome do arquivo do clipe no disco do box e entra na chave
  // do objeto no R2 (§4.4). Um id com barra ou ponto-ponto viraria caminho.
  const loja = await semeiaLoja();
  const corpo = corpoEvento(loja, { event_id: "../../etc/passwd" });

  const resposta = await postaEvento(loja.token, { ...corpo });

  assert.equal(resposta.statusCode, 400);
});

test("source fora do enum -> 400", async () => {
  const loja = await semeiaLoja();
  const resposta = await postaEvento(loja.token, corpoEvento(loja, { source: "palpite" }));
  assert.equal(resposta.statusCode, 400);
});

test("schema_version diferente de 1 -> 400", async () => {
  const loja = await semeiaLoja();
  const resposta = await postaEvento(loja.token, corpoEvento(loja, { schema_version: 2 }));
  assert.equal(resposta.statusCode, 400);
});

test("a validade configurada chega à resposta E à assinatura da URL", async () => {
  // Os dois números têm que ser o mesmo. Se `clip_upload_expires_in_s` divergisse do
  // X-Amz-Expires que a URL carrega, o agente calcularia mal o vencimento: com o campo
  // maior que a assinatura, ele gastaria tentativas num PUT que já vai falhar; com o
  // campo menor, reporia o evento antes da hora e pediria URL nova sem precisar.
  const outroApp = await buildTestApp({ uploadExpiraEmS: 120 });
  try {
    const loja = await semeiaLoja();
    const corpo = corpoEvento(loja);

    const resposta = await outroApp.inject({
      method: "POST",
      url: "/v1/events",
      headers: { authorization: `Bearer ${loja.token}` },
      payload: corpo,
    });

    const body = resposta.json();
    assert.equal(body.clip_upload_expires_in_s, 120);
    assert.equal(new URL(body.clip_upload_url).searchParams.get("X-Amz-Expires"), "120");
  } finally {
    await outroApp.close();
  }
});
