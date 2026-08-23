import type { FastifyInstance } from "fastify";
import { autenticaAgente } from "../../auth/agent-token.js";
import { RequisicaoInvalida } from "../../auth/errors.js";
import { validaRequisicaoHeartbeat } from "./heartbeat-schema.js";

// POST /v1/agents/heartbeat (§5.3). Telemetria periódica, sem estado a devolver -- 204
// sem corpo, mesma leitura do agente que o 304 do config tem (outbox/http.py):
// sucesso não precisa de payload de volta.
//
// Guarda só o último heartbeat (heartbeatEm/heartbeat em Agente), não série histórica
// -- ver comentário em prisma/schema.prisma. heartbeatEm sozinho, sem precisar
// parsear o JSON, é o que sustenta o alerta de agente offline do R-6.
export default async function agentsHeartbeatRoutes(app: FastifyInstance) {
  app.post("/v1/agents/heartbeat", async (request, reply) => {
    const agente = await autenticaAgente(app.prisma, request.headers.authorization);

    const validacao = validaRequisicaoHeartbeat(request.body);
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com heartbeat.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }

    await app.prisma.agente.update({
      where: { id: agente.agenteId },
      data: { heartbeatEm: new Date(), heartbeat: request.body as object },
    });

    return reply.code(204).send();
  });
}
