import { PrismaClient } from "@prisma/client";
import { hashToken } from "../src/auth/agent-token.js";
import { criaTokenBootstrap } from "../src/auth/bootstrap-token.js";

// Semeia tenant/loja/agente/câmeras de dev, espelhando apps/agent/config.exemplo.json
// (mesma loja "loja-dev", mesmas câmeras cam1/cam3, mesma regra da cam3). Idempotente
// via upsert por id fixo -- pode rodar de novo sem duplicar.
//
// Token fixo (não aleatório a cada run): é ambiente local, nunca segredo de produção,
// e permite repetir `--config-nuvem` contra a API sem copiar um token novo toda vez --
// mesmo padrão que infra/.env.example já usa para a senha de exemplo do Postgres.
const TOKEN_DEV = "dev-agent-token-local-only";

const RULES_CAM3 = {
  enabled: true,
  rule_id: "saida-sem-caixa",
  rule_version: 1,
  linha_saida: { origem: [0, 400], destino: [640, 400] },
  zonas_caixa: [
    {
      vertices: [
        [0, 410],
        [260, 410],
        [260, 478],
        [0, 478],
      ],
    },
  ],
  tempo_caixa_min_s: 3,
  vida_min_s: 1,
};

async function main(): Promise<void> {
  const prisma = new PrismaClient();

  const tenant = await prisma.tenant.upsert({
    where: { id: "dev" },
    create: { id: "dev", nome: "Tenant de desenvolvimento" },
    update: {},
  });

  const loja = await prisma.loja.upsert({
    where: { id: "loja-dev" },
    create: {
      id: "loja-dev",
      tenantId: tenant.id,
      nome: "Loja de desenvolvimento",
      detection: { enabled: true },
    },
    update: { detection: { enabled: true } },
  });

  await prisma.camera.upsert({
    where: { lojaId_cameraId: { lojaId: loja.id, cameraId: "cam3" } },
    create: {
      lojaId: loja.id,
      cameraId: "cam3",
      url: "rtsp://localhost:8554/cam3",
      rules: RULES_CAM3,
    },
    update: { url: "rtsp://localhost:8554/cam3", rules: RULES_CAM3 },
  });

  await prisma.camera.upsert({
    where: { lojaId_cameraId: { lojaId: loja.id, cameraId: "cam1" } },
    create: { lojaId: loja.id, cameraId: "cam1", url: "rtsp://localhost:8554/cam1" },
    update: { url: "rtsp://localhost:8554/cam1" },
  });

  // Token de bootstrap para exercitar POST /v1/agents/register (§5.1) na mão. Não é
  // idempotente por id fixo como o resto do seed -- é de uso único por natureza, então
  // cada `pnpm seed` gera um novo (o(s) anterior(es) seguem no banco, inertes).
  const bootstrap = await criaTokenBootstrap(prisma, loja.id);

  const tokenHash = hashToken(TOKEN_DEV);
  const agenteExistente = await prisma.agente.findUnique({ where: { tokenHash } });
  if (!agenteExistente) {
    await prisma.agente.create({
      data: {
        lojaId: loja.id,
        tokenHash,
        tokenPrefix: TOKEN_DEV.slice(0, 8),
        ativo: true,
      },
    });
  }

  const porta = process.env.PORT ?? "3000";

  console.log("Loja semeada:", loja.id);
  console.log("Token do agente de dev:", TOKEN_DEV);
  console.log("");
  console.log("Teste com o agente real:");
  console.log(
    `  cd apps/agent && uv run python -m lince_agent --config-nuvem --api-url http://localhost:${porta} --api-token ${TOKEN_DEV} --model models/yolox_s.onnx --dry-run --stats`,
  );
  console.log("");
  console.log(
    `Token de bootstrap (expira em ${bootstrap.expiraEm.toISOString()}):`,
    bootstrap.token,
  );
  console.log("Teste POST /v1/agents/register:");
  console.log(
    `  curl -s -X POST http://localhost:${porta}/v1/agents/register -H 'content-type: application/json' -d '{"schema_version":1,"bootstrap_token":"${bootstrap.token}"}'`,
  );
  console.log("");
  console.log("Teste POST /v1/agents/heartbeat (§5.3), com o token do agente de dev:");
  console.log(
    `  curl -s -o /dev/null -w '%{http_code}\\n' -X POST http://localhost:${porta}/v1/agents/heartbeat -H 'content-type: application/json' -H 'authorization: Bearer ${TOKEN_DEV}' -d '{"schema_version":1,"agent_version":"0.0.0","model_version":null,"config_version":null,"queue":{"depth":0,"oldest_age_s":0,"bytes":0},"cameras":[],"uptime_s":1,"restarts":0,"clock_skew_s":0}'`,
  );

  await prisma.$disconnect();
}

main().catch((erro: unknown) => {
  console.error(erro);
  process.exitCode = 1;
});
