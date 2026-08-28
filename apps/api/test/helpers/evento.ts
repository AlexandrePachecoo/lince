import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import type { FastifyInstance } from "fastify";
import type { LojaSemeada } from "./test-db.js";

// Cria evento pela rota de verdade (POST /v1/events), e não semeando a linha na mão.
// Vale para as rotas do dashboard pelo mesmo motivo que já valia para o PATCH do clipe:
// o que a fila de triagem mostra é o que o agente mandou, e uma linha montada aqui
// esconderia justamente as diferenças entre os dois -- o bloco `clip` congelado no
// corte, os três instantes, a regra achatada em duas colunas.

export interface OpcoesEvento {
  cameraId?: string;
  ocorridoEm?: string;
  source?: "rule" | "manual";
  rule?: { id: string; version: number } | null;
  clipStatus?: "ok" | "clip_failed";
}

export async function criaEvento(
  app: FastifyInstance,
  loja: LojaSemeada,
  opcoes: OpcoesEvento = {},
): Promise<string> {
  const eventId = randomUUID();
  const ocorridoEm = opcoes.ocorridoEm ?? "2026-08-23T14:00:00.000Z";
  const source = opcoes.source ?? "rule";
  const clipStatus = opcoes.clipStatus ?? "ok";

  const resposta = await app.inject({
    method: "POST",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
    payload: {
      schema_version: 1,
      event_id: eventId,
      tenant_id: loja.tenantId,
      store_id: loja.lojaId,
      camera_id: opcoes.cameraId ?? "cam1",
      occurred_at: ocorridoEm,
      reported_at: ocorridoEm,
      source,
      rule:
        source === "manual"
          ? null
          : (opcoes.rule ?? { id: "saida-sem-passar-no-caixa", version: 3 }),
      versions: { agent: "0.1.0", model: "yolox_s-abc123", config: "v7" },
      clip:
        clipStatus === "ok"
          ? { status: "ok", duration_s: 15, size_bytes: 1_200_000 }
          : { status: "clip_failed", error: "buffer despejado pelo teto de disco" },
    },
  });
  assert.ok(resposta.statusCode < 300, `POST /v1/events falhou com ${resposta.statusCode}`);
  return eventId;
}
