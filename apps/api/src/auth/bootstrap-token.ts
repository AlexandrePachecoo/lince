import { randomBytes } from "node:crypto";
import type { PrismaClient } from "@prisma/client";
import { hashToken } from "./agent-token.js";

// TTL do token de bootstrap (§5.1). A arquitetura não fixa um valor -- o token existe
// para provisionar a loja logo depois de cadastrada, não para durar, então 24h é uma
// janela generosa sem virar um segredo de vida longa por engano. Sem flag de config:
// não há hoje um segundo caso de uso pedindo isso configurável.
export const TTL_HORAS = 24;

export function geraToken(): string {
  return randomBytes(32).toString("hex");
}

export interface TokenBootstrapCriado {
  token: string;
  expiraEm: Date;
}

// Usada hoje só por scripts/seed.ts -- não existe POST /v1/lojas nem dashboard ainda
// para gerar isto por conta própria (§5.1 descreve "gerado no dashboard ao cadastrar
// a loja"; nesta fatia, o seed faz esse papel).
export async function criaTokenBootstrap(
  prisma: PrismaClient,
  lojaId: string,
): Promise<TokenBootstrapCriado> {
  const token = geraToken();
  const expiraEm = new Date(Date.now() + TTL_HORAS * 60 * 60 * 1000);

  await prisma.tokenBootstrap.create({
    data: {
      lojaId,
      tokenHash: hashToken(token),
      tokenPrefix: token.slice(0, 8),
      expiraEm,
    },
  });

  return { token, expiraEm };
}
