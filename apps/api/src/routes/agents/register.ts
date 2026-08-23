import type { FastifyInstance } from "fastify";
import { hashToken } from "../../auth/agent-token.js";
import { geraToken } from "../../auth/bootstrap-token.js";
import {
  BootstrapTokenExpirado,
  BootstrapTokenInvalido,
  BootstrapTokenJaUsado,
  RequisicaoInvalida,
} from "../../auth/errors.js";
import { validaRequisicaoRegister, validaRespostaRegister } from "./register-schema.js";

// POST /v1/agents/register (§5.1). Troca um token de bootstrap de uso único por uma
// credencial de agente de longa duração. Contrato exato: agent-register.v1.json /
// agent-register-response.v1.json em packages/shared.
export default async function agentsRegisterRoutes(app: FastifyInstance) {
  app.post("/v1/agents/register", async (request, reply) => {
    const validacaoCorpo = validaRequisicaoRegister(request.body);
    if (!validacaoCorpo.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com agent-register.v1.json: ${JSON.stringify(validacaoCorpo.erros)}`,
      );
    }
    const { bootstrap_token: bootstrapToken } = request.body as { bootstrap_token: string };

    const tokenNovo = geraToken();

    const agente = await app.prisma.$transaction(async (tx) => {
      const registro = await tx.tokenBootstrap.findUnique({
        where: { tokenHash: hashToken(bootstrapToken) },
      });
      if (!registro) {
        throw new BootstrapTokenInvalido();
      }
      if (registro.expiraEm.getTime() < Date.now()) {
        throw new BootstrapTokenExpirado();
      }

      // Consumo atômico: UPDATE ... WHERE usado_em IS NULL. Sob READ COMMITTED, a
      // segunda de duas chamadas concorrentes reavalia o predicado depois de esperar
      // o lock da primeira e encontra 0 linhas -- é isto, não uma checagem em memória,
      // que impede dois POST /v1/agents/register simultâneos com o mesmo token de
      // bootstrap criarem dois Agente (ver test/routes/agents-register.test.ts).
      const consumo = await tx.tokenBootstrap.updateMany({
        where: { id: registro.id, usadoEm: null },
        data: { usadoEm: new Date() },
      });
      if (consumo.count === 0) {
        throw new BootstrapTokenJaUsado();
      }

      return tx.agente.create({
        data: {
          lojaId: registro.lojaId,
          tokenHash: hashToken(tokenNovo),
          tokenPrefix: tokenNovo.slice(0, 8),
          ativo: true,
        },
        include: { loja: true },
      });
    });

    const documento = {
      schema_version: 1,
      agent_id: agente.id,
      tenant_id: agente.loja.tenantId,
      store_id: agente.lojaId,
      token: tokenNovo,
    };

    const validacaoResposta = validaRespostaRegister(documento);
    if (!validacaoResposta.valido) {
      // Defesa em profundidade, mesmo padrão de config.ts: bug interno, nunca um 201
      // malformado.
      request.log.error(
        { erros: validacaoResposta.erros, agenteId: agente.id },
        "resposta de register não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(201).send(documento);
  });
}
