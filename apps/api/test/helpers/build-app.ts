import type { FastifyInstance } from "fastify";
import { buildApp } from "../../src/server.js";
import type { ConfigArmazenamento } from "../../src/storage/clip-storage.js";

// Monta o app real (server.ts + plugin do Prisma real + MinIO real) contra
// TEST_DATABASE_URL e TEST_S3_BUCKET -- nenhum mock: os testes de rota batem no mesmo
// caminho de código que produção, só apontado para o schema e o bucket de teste (ver
// test/helpers/test-db.ts e infra/docker-compose.yml).

export function configArmazenamentoTeste(): ConfigArmazenamento {
  const bucket = process.env.TEST_S3_BUCKET;
  if (!bucket) {
    throw new Error(
      "TEST_S3_BUCKET não definida -- copie apps/api/.env.example para .env e rode " +
        "`pnpm infra:up`, que cria os buckets no MinIO.",
    );
  }
  return {
    endpoint: process.env.S3_ENDPOINT ?? "http://localhost:9000",
    region: process.env.S3_REGION ?? "auto",
    accessKeyId: process.env.S3_ACCESS_KEY_ID ?? "lince",
    secretAccessKey: process.env.S3_SECRET_ACCESS_KEY ?? "troque-esta-senha",
    bucket,
  };
}

// Segredo fixo de teste: a suíte precisa poder forjar e conferir token, e um segredo
// aleatório por processo tornaria impossível escrever o teste que prova que um token
// assinado com OUTRO segredo é recusado. Nunca vale fora daqui -- em produção
// AUTH_JWT_SECRET é obrigatória e sem padrão (src/env.ts).
export const SEGREDO_TESTE = "segredo-de-teste-so-para-a-suite-com-32+";

export interface OpcoesTestApp {
  /** Validade das URLs de upload emitidas. Existe para o teste que confere que o
   * número configurado é o mesmo que sai na resposta E na assinatura da URL, em vez
   * de um 900 embutido no código da rota. */
  uploadExpiraEmS?: number;
  /** Idem para a URL de leitura do clipe emitida ao dashboard. */
  leituraExpiraEmS?: number;
  /** Existe para o teste de sessão vencida: emitir um token com validade de 1 s e
   * conferir que ele é recusado depois, sem sleep (CLAUDE.md) -- quem manipula o
   * relógio é emiteToken, via agoraMs. */
  sessaoExpiraEmS?: number;
}

export async function buildTestApp(opcoes: OpcoesTestApp = {}): Promise<FastifyInstance> {
  const databaseUrl = process.env.TEST_DATABASE_URL;
  if (!databaseUrl) {
    throw new Error("TEST_DATABASE_URL não definida (ver apps/api/.env.example)");
  }
  return buildApp({
    databaseUrl,
    armazenamento: configArmazenamentoTeste(),
    uploadExpiraEmS: opcoes.uploadExpiraEmS ?? 900,
    leituraExpiraEmS: opcoes.leituraExpiraEmS ?? 300,
    sessaoSegredo: SEGREDO_TESTE,
    sessaoExpiraEmS: opcoes.sessaoExpiraEmS ?? 43200,
    logger: false,
  });
}
