// Leitura e validação das variáveis de ambiente que a API precisa. Falhar aqui, no
// boot, é preferível a falhar mais tarde num handler com uma mensagem genérica de
// "undefined não é uma função" quando DATABASE_URL não estiver setada.

export interface Env {
  databaseUrl: string;
  port: number;
}

export function carregaEnv(fonte: NodeJS.ProcessEnv = process.env): Env {
  const databaseUrl = fonte.DATABASE_URL;
  if (!databaseUrl) {
    throw new Error("DATABASE_URL não definida (ver apps/api/.env.example)");
  }

  const portTexto = fonte.PORT ?? "3000";
  const port = Number.parseInt(portTexto, 10);
  if (Number.isNaN(port) || port <= 0) {
    throw new Error(`PORT inválida: "${portTexto}"`);
  }

  return { databaseUrl, port };
}
