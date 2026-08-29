import Fastify, { type FastifyInstance } from "fastify";
import { AppError } from "./app-error.js";
import authPlugin from "./plugins/auth.js";
import prismaPlugin from "./plugins/prisma.js";
import storagePlugin from "./plugins/storage.js";
import agentsConfigRoutes from "./routes/agents/config.js";
import agentsHeartbeatRoutes from "./routes/agents/heartbeat.js";
import agentsRegisterRoutes from "./routes/agents/register.js";
import authLoginRoutes from "./routes/auth/login.js";
import eventsClipRoutes from "./routes/events/clip.js";
import eventsClipeUrlRoutes from "./routes/events/clipe-url.js";
import eventsCreateRoutes from "./routes/events/create.js";
import eventsDetalheRoutes from "./routes/events/detalhe.js";
import eventsListarRoutes from "./routes/events/listar.js";
import eventsTriagemRoutes from "./routes/events/triagem.js";
import usuariosAlterarRoutes from "./routes/usuarios/alterar.js";
import usuariosCriarRoutes from "./routes/usuarios/criar.js";
import usuariosListarRoutes from "./routes/usuarios/listar.js";
import type { ConfigArmazenamento } from "./storage/clip-storage.js";

export interface BuildAppOptions {
  databaseUrl: string;
  armazenamento: ConfigArmazenamento;
  uploadExpiraEmS: number;
  leituraExpiraEmS: number;
  sessaoSegredo: string;
  sessaoExpiraEmS: number;
  logger?: boolean;
}

export async function buildApp(opts: BuildAppOptions): Promise<FastifyInstance> {
  const app = Fastify({ logger: opts.logger ?? true });

  await app.register(prismaPlugin, { databaseUrl: opts.databaseUrl });
  await app.register(storagePlugin, {
    armazenamento: opts.armazenamento,
    uploadExpiraEmS: opts.uploadExpiraEmS,
    leituraExpiraEmS: opts.leituraExpiraEmS,
  });
  await app.register(authPlugin, {
    sessaoSegredo: opts.sessaoSegredo,
    sessaoExpiraEmS: opts.sessaoExpiraEmS,
  });

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
  await app.register(eventsCreateRoutes);
  await app.register(eventsClipRoutes);

  // Rotas do dashboard (§4.5). Credencial humana, não de agente: os dois caminhos leem o
  // mesmo cabeçalho Bearer e não se cruzam em lugar nenhum -- ver auth/usuario-token.ts.
  await app.register(authLoginRoutes);
  await app.register(eventsListarRoutes);
  await app.register(eventsDetalheRoutes);
  await app.register(eventsTriagemRoutes);
  await app.register(eventsClipeUrlRoutes);
  await app.register(usuariosCriarRoutes);
  await app.register(usuariosListarRoutes);
  await app.register(usuariosAlterarRoutes);

  return app;
}
