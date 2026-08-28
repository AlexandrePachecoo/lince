// Leitura e validação das variáveis de ambiente que a API precisa. Falhar aqui, no
// boot, é preferível a falhar mais tarde num handler com uma mensagem genérica de
// "undefined não é uma função" quando DATABASE_URL não estiver setada.

import type { ConfigArmazenamento } from "./storage/clip-storage.js";

export interface Env {
  databaseUrl: string;
  port: number;
  armazenamento: ConfigArmazenamento;
  uploadExpiraEmS: number;
  leituraExpiraEmS: number;
  sessaoSegredo: string;
  sessaoExpiraEmS: number;
}

// 15 min. Curto de propósito (NFR-7): o agente já sabe renovar uma URL vencida
// repetindo o POST, que é idempotente (§5.4), então esticar isto não compra
// robustez nenhuma -- só aumenta a janela de uma URL que vazou.
const UPLOAD_EXPIRA_PADRAO_S = 900;

// 5 min para a URL de leitura do clipe -- um terço da de upload, e não por simetria
// esquecida. O clipe é dado pessoal (R-9) e a URL assinada é a credencial inteira:
// quem a tiver, vê o vídeo (NFR-7). Quem faz o PUT é um box com a fila cheia e link
// ruim, e por isso precisa de folga; quem faz o GET é um dashboard que já está com o
// player aberto na tela. Pedir uma URL nova é uma requisição, e ela fica registrada na
// auditoria -- que é justamente o que se quer que aconteça de novo.
const LEITURA_EXPIRA_PADRAO_S = 300;

// 12 h: um turno. Sem refresh token nesta fatia, então o número é o intervalo entre
// dois logins do gerente, e não uma janela de risco solta -- desativar o usuário ou
// bumpar Usuario.tokenVersao invalida na requisição seguinte (auth/usuario-token.ts).
const SESSAO_EXPIRA_PADRAO_S = 43200;

// HS256 com segredo curto é força bruta offline: quem capturar um token testa
// candidatos sem falar com a API. 32 caracteres é o piso, não a recomendação -- em
// produção isto é saída de `openssl rand -base64 48`.
const SEGREDO_TAMANHO_MINIMO = 32;

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
  const leituraExpiraEmS = numero(
    fonte.CLIP_READ_EXPIRES_S ?? String(LEITURA_EXPIRA_PADRAO_S),
    "CLIP_READ_EXPIRES_S",
  );

  // Obrigatório e sem padrão, pelo mesmo motivo das credenciais do bucket: um segredo
  // de desenvolvimento embutido no código sobe para produção sem ninguém perceber, e
  // aí qualquer um que leia o repositório assina um token de admin.
  const sessaoSegredo = obrigatoria(fonte, "AUTH_JWT_SECRET");
  if (sessaoSegredo.length < SEGREDO_TAMANHO_MINIMO) {
    throw new Error(
      `AUTH_JWT_SECRET curta demais (${sessaoSegredo.length} caracteres, mínimo ` +
        `${SEGREDO_TAMANHO_MINIMO})`,
    );
  }
  const sessaoExpiraEmS = numero(
    fonte.AUTH_TOKEN_EXPIRES_S ?? String(SESSAO_EXPIRA_PADRAO_S),
    "AUTH_TOKEN_EXPIRES_S",
  );

  return {
    databaseUrl,
    port,
    armazenamento,
    uploadExpiraEmS,
    leituraExpiraEmS,
    sessaoSegredo,
    sessaoExpiraEmS,
  };
}
