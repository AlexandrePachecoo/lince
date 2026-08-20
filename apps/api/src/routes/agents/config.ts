import type { FastifyInstance } from "fastify";
import { autenticaAgente } from "../../auth/agent-token.js";
import { montaDocumento } from "../../config/document-builder.js";
import { calculaEtag, etagBate } from "../../config/etag.js";
import { validaDocumentoConfig } from "../../config/schema-validator.js";

// GET /v1/agents/config (§5.2). Contrato exato consumido por
// apps/agent/src/lince_agent/outbox/{http.py,policy.py}:
//   - 401/403 (auth) -> RECUSADA no agente, nunca 5xx nem 408/429.
//   - 304 sem corpo quando If-None-Match bate -> SEM_MUDANCA (caminho mais frequente).
//   - 200 com corpo validado contra config.v1.json -> NOVA.
//   - Falha de montagem/validação do lado da API -> 500 -> TENTAR_DEPOIS (instabilidade
//     genuína, nunca confundida com "conserto é humano").
export default async function agentsConfigRoutes(app: FastifyInstance) {
  app.get("/v1/agents/config", async (request, reply) => {
    const agente = await autenticaAgente(app.prisma, request.headers.authorization);

    // Filtra por lojaId E tenantId -- defesa em profundidade de NFR-6, mesmo já tendo
    // resolvido lojaId a partir do token.
    const loja = await app.prisma.loja.findFirst({
      where: { id: agente.lojaId, tenantId: agente.tenantId },
      include: { cameras: true },
    });
    if (!loja) {
      // Invariante quebrada: Agente.lojaId é FK obrigatória, então um agente
      // autenticado sempre aponta para uma loja existente. Chegar aqui é bug
      // interno, não erro esperado de cliente.
      request.log.error(
        { agenteId: agente.agenteId },
        "agente autenticado sem loja correspondente",
      );
      return reply.code(500).send();
    }

    const documento = montaDocumento(loja);
    const etag = calculaEtag(documento);
    reply.header("ETag", etag);

    const ifNoneMatch = normalizaHeader(request.headers["if-none-match"]);
    if (etagBate(ifNoneMatch, etag)) {
      // Corpo vazio é obrigatório aqui: é o caminho SEM_MUDANCA, o mais frequente do
      // transporte de config -- qualquer corpo confundiria o poller (§5.4).
      return reply.code(304).send();
    }

    const validacao = validaDocumentoConfig(documento);
    if (!validacao.valido) {
      request.log.error(
        { erros: validacao.erros, lojaId: loja.id },
        "documento de config inválido gerado pela própria API",
      );
      return reply.code(500).send();
    }

    reply.header("Content-Type", "application/json");
    return reply.code(200).send(documento);
  });
}

function normalizaHeader(valor: string | string[] | undefined): string | undefined {
  return Array.isArray(valor) ? valor[0] : valor;
}
