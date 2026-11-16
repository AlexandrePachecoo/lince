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

/** Quem pediu o token. Ausente só no scripts/seed.ts, que não tem usuário. */
export interface AutorDoToken {
  usuarioId: string;
  email: string;
}

// Usada pelo POST /v1/lojas/{store_id}/bootstrap-token (o "gerado no dashboard" da
// §5.1) e pelo scripts/seed.ts.
//
// Gerar um token **vence os anteriores da mesma loja que ainda não foram usados**. O
// token é mostrado uma vez e copiado para um `.env`; quem pede outro é porque perdeu o
// primeiro ou não sabe onde ele foi parar, e nos dois casos o primeiro continuar
// valendo é uma credencial solta que ninguém está vigiando. Os já usados não são
// tocados: a linha deles é o registro de qual token virou qual agente.
//
// Vencer é adiantar `expiraEm`, e não apagar a linha: a trilha de quem gerou continua,
// e o box que chegar com o token antigo recebe "expirado" -- que manda gerar outro --
// em vez de "não reconhecido", que mandaria conferir se copiou errado.
export async function criaTokenBootstrap(
  prisma: PrismaClient,
  lojaId: string,
  autor?: AutorDoToken,
): Promise<TokenBootstrapCriado> {
  const token = geraToken();
  const agora = new Date();
  const expiraEm = new Date(agora.getTime() + TTL_HORAS * 60 * 60 * 1000);

  await prisma.$transaction([
    prisma.tokenBootstrap.updateMany({
      where: { lojaId, usadoEm: null, expiraEm: { gt: agora } },
      data: { expiraEm: agora },
    }),
    prisma.tokenBootstrap.create({
      data: {
        lojaId,
        tokenHash: hashToken(token),
        tokenPrefix: token.slice(0, 8),
        expiraEm,
        criadoPorId: autor?.usuarioId ?? null,
        criadoPorEmail: autor?.email ?? null,
      },
    }),
  ]);

  return { token, expiraEm };
}
