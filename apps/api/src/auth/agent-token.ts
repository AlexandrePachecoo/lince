import { createHash } from "node:crypto";
import type { PrismaClient } from "@prisma/client";
import { AgenteInativo, TokenAusente, TokenInvalido } from "./errors.js";

export interface AgenteAutenticado {
  agenteId: string;
  lojaId: string;
  tenantId: string;
}

// SHA-256 hex, função pura -- o banco guarda só isto, nunca o token em claro (nem em
// seed de dev: não é hábito para estabelecer, mesmo fora de produção).
export function hashToken(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

const PREFIXO_BEARER = "Bearer ";

export async function autenticaAgente(
  prisma: PrismaClient,
  authorizationHeader: string | undefined,
): Promise<AgenteAutenticado> {
  if (!authorizationHeader || !authorizationHeader.startsWith(PREFIXO_BEARER)) {
    throw new TokenAusente();
  }
  const token = authorizationHeader.slice(PREFIXO_BEARER.length).trim();
  if (token.length === 0) {
    throw new TokenAusente();
  }

  const agente = await prisma.agente.findUnique({
    where: { tokenHash: hashToken(token) },
    include: { loja: true },
  });
  if (!agente) {
    throw new TokenInvalido();
  }
  if (!agente.ativo) {
    throw new AgenteInativo();
  }

  return {
    agenteId: agente.id,
    lojaId: agente.lojaId,
    tenantId: agente.loja.tenantId,
  };
}
