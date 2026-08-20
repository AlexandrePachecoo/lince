import { PrismaClient } from "@prisma/client";
import type { FastifyInstance } from "fastify";
import fp from "fastify-plugin";

declare module "fastify" {
  interface FastifyInstance {
    prisma: PrismaClient;
  }
}

export interface PrismaPluginOptions {
  databaseUrl: string;
}

export default fp<PrismaPluginOptions>(async (app: FastifyInstance, opts: PrismaPluginOptions) => {
  const prisma = new PrismaClient({
    datasources: { db: { url: opts.databaseUrl } },
  });
  await prisma.$connect();

  app.decorate("prisma", prisma);
  app.addHook("onClose", async (instance) => {
    await instance.prisma.$disconnect();
  });
});
