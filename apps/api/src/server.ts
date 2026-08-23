import Fastify, { type FastifyInstance } from "fastify";
import { AppError } from "./app-error.js";
import prismaPlugin from "./plugins/prisma.js";
import agentsConfigRoutes from "./routes/agents/config.js";
import agentsHeartbeatRoutes from "./routes/agents/heartbeat.js";
import agentsRegisterRoutes from "./routes/agents/register.js";

export interface BuildAppOptions {
  databaseUrl: string;
  logger?: boolean;
}

export async function buildApp(opts: BuildAppOptions): Promise<FastifyInstance> {
  const app = Fastify({ logger: opts.logger ?? true });

  await app.register(prismaPlugin, { databaseUrl: opts.databaseUrl });

  // Erro tipado (auth, validação) vira o status que ele já declara. Qualquer outra
  // exceção é bug interno -> 500, nunca vaza como 401/403 nem some sem log.
  app.setErrorHandler((error, request, reply) => {
    if (error instanceof AppError) {
      return reply.code(error.status).send({ erro: error.message });
    }
    request.log.error(error);
    return reply.code(500).send({ erro: "erro interno" });
  });

  app.get("/health", async () => ({ status: "ok" }));

  await app.register(agentsConfigRoutes);
  await app.register(agentsRegisterRoutes);
  await app.register(agentsHeartbeatRoutes);

  return app;
}
