import { carregaEnv } from "./env.js";
import { buildApp } from "./server.js";

const env = carregaEnv();
const app = await buildApp({ databaseUrl: env.databaseUrl });

try {
  await app.listen({ port: env.port, host: "0.0.0.0" });
} catch (erro) {
  app.log.error(erro);
  process.exit(1);
}
