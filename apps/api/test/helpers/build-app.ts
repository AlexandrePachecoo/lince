import type { FastifyInstance } from "fastify";
import { buildApp } from "../../src/server.js";

// Monta o app real (server.ts + plugin do Prisma real) contra TEST_DATABASE_URL --
// nenhum mock: os testes de rota batem no mesmo caminho de código que produção,
// só apontado para o schema de teste (ver test/helpers/test-db.ts).
export async function buildTestApp(): Promise<FastifyInstance> {
  const databaseUrl = process.env.TEST_DATABASE_URL;
  if (!databaseUrl) {
    throw new Error("TEST_DATABASE_URL não definida (ver apps/api/.env.example)");
  }
  return buildApp({ databaseUrl, logger: false });
}
