// Leitura e validação das variáveis de ambiente que a API precisa. Falhar aqui, no
// boot, é preferível a falhar mais tarde num handler com uma mensagem genérica de
// "undefined não é uma função" quando DATABASE_URL não estiver setada.

import type { ConfigArmazenamento } from "./storage/clip-storage.js";

export interface Env {
  databaseUrl: string;
  port: number;
  armazenamento: ConfigArmazenamento;
  uploadExpiraEmS: number;
}

// 15 min. Curto de propósito (NFR-7): o agente já sabe renovar uma URL vencida
// repetindo o POST, que é idempotente (§5.4), então esticar isto não compra
// robustez nenhuma -- só aumenta a janela de uma URL que vazou.
const UPLOAD_EXPIRA_PADRAO_S = 900;

function obrigatoria(fonte: NodeJS.ProcessEnv, nome: string): string {
  const valor = fonte[nome];
  if (!valor) {
    throw new Error(`${nome} não definida (ver apps/api/.env.example)`);
  }
  return valor;
}

function numero(texto: string, nome: string): number {
  const valor = Number.parseInt(texto, 10);
  if (Number.isNaN(valor) || valor <= 0) {
    throw new Error(`${nome} inválida: "${texto}"`);
  }
  return valor;
}

export function carregaEnv(fonte: NodeJS.ProcessEnv = process.env): Env {
  const databaseUrl = obrigatoria(fonte, "DATABASE_URL");
  const port = numero(fonte.PORT ?? "3000", "PORT");

  // Todas obrigatórias, nenhuma com valor de desenvolvimento embutido: um padrão
  // silencioso aqui deixaria a API subir em produção assinando contra um bucket que
  // não é o dela, e o sintoma seria clipe sumindo, não erro de boot.
  const armazenamento: ConfigArmazenamento = {
    endpoint: obrigatoria(fonte, "S3_ENDPOINT"),
    region: obrigatoria(fonte, "S3_REGION"),
    accessKeyId: obrigatoria(fonte, "S3_ACCESS_KEY_ID"),
    secretAccessKey: obrigatoria(fonte, "S3_SECRET_ACCESS_KEY"),
    bucket: obrigatoria(fonte, "S3_BUCKET"),
  };

  const uploadExpiraEmS = numero(
    fonte.CLIP_UPLOAD_EXPIRES_S ?? String(UPLOAD_EXPIRA_PADRAO_S),
    "CLIP_UPLOAD_EXPIRES_S",
  );

  return { databaseUrl, port, armazenamento, uploadExpiraEmS };
}
