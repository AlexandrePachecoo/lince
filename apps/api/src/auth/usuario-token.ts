import type { PapelUsuario, PrismaClient } from "@prisma/client";
import {
  SemAcessoALoja,
  SemPermissao,
  SessaoExpirada,
  TokenAusente,
  TokenInvalido,
  UsuarioInativo,
} from "./errors.js";
import {
  AssinaturaInvalida,
  type ClaimsSessao,
  TokenMalformado,
  TokenVencido,
  verificaToken,
} from "./jwt.js";

// O par humano de agent-token.ts. Os dois leem o mesmo cabeçalho `Authorization:
// Bearer`, e ainda assim são caminhos separados que não se cruzam em lugar nenhum:
// um token de agente apresentado aqui não é JWT e morre na assinatura; um JWT
// apresentado lá não casa nenhum tokenHash. Nenhum código decide "que tipo de
// credencial é esta" -- a rota já sabe quem ela atende, e é isso que garante que a
// credencial que vive num box no estoque de um mercado não abra clipe nem trie evento.

export interface UsuarioAutenticado {
  usuarioId: string;
  tenantId: string;
  email: string;
  nome: string;
  /** lojaId -> papel. Vem do banco a cada requisição, nunca do token. */
  lojas: ReadonlyMap<string, PapelUsuario>;
}

const PREFIXO_BEARER = "Bearer ";

export async function autenticaUsuario(
  prisma: PrismaClient,
  authorizationHeader: string | undefined,
  segredo: string,
): Promise<UsuarioAutenticado> {
  if (!authorizationHeader || !authorizationHeader.startsWith(PREFIXO_BEARER)) {
    throw new TokenAusente();
  }
  const token = authorizationHeader.slice(PREFIXO_BEARER.length).trim();
  if (token.length === 0) {
    throw new TokenAusente();
  }

  let claims: ClaimsSessao;
  try {
    claims = verificaToken(token, segredo);
  } catch (erro) {
    if (erro instanceof TokenVencido) {
      throw new SessaoExpirada();
    }
    if (erro instanceof AssinaturaInvalida || erro instanceof TokenMalformado) {
      throw new TokenInvalido();
    }
    throw erro;
  }

  // O JWT poupa a tabela de sessão, não o SELECT: papel e lojas mudam no cadastro e
  // não podem viajar congelados dentro do token -- tirar um gerente de uma loja tem
  // que valer na requisição seguinte, não quando a sessão dele vencer. Como a leitura
  // acontece de qualquer jeito, é aqui que `ativo` e `tokenVersao` também se conferem,
  // e é por isso que desativar alguém corta o acesso na hora.
  const usuario = await prisma.usuario.findUnique({
    where: { id: claims.sub },
    include: { lojas: true },
  });
  if (!usuario) {
    throw new TokenInvalido();
  }
  if (!usuario.ativo) {
    throw new UsuarioInativo();
  }
  if (usuario.tokenVersao !== claims.ver) {
    // Trocou a senha ou pediu para derrubar as sessões. Não é token adulterado.
    throw new SessaoExpirada();
  }
  if (usuario.tenantId !== claims.tid) {
    // O tenant do token não é mais o do cadastro. Não deveria acontecer -- usuário não
    // muda de tenant hoje -- e é exatamente por isso que passar batido seria pior: o
    // token continuaria autorizando leitura no tenant antigo.
    throw new TokenInvalido();
  }

  return {
    usuarioId: usuario.id,
    tenantId: usuario.tenantId,
    email: usuario.email,
    nome: usuario.nome,
    lojas: new Map(usuario.lojas.map((v) => [v.lojaId, v.papel])),
  };
}

/**
 * Papel do usuário na loja, ou 403. Toda rota que recebe `loja_id` (na query, no
 * caminho ou vindo da linha do evento) passa por aqui antes de devolver qualquer
 * dado — é o vínculo, e não o tenant, que decide o que um gerente enxerga.
 */
export function exigePapelNaLoja(usuario: UsuarioAutenticado, lojaId: string): PapelUsuario {
  const papel = usuario.lojas.get(lojaId);
  if (papel === undefined) {
    throw new SemAcessoALoja();
  }
  return papel;
}

/** Idem, exigindo `admin` — administrar gente é papel de loja (ver schema.prisma). */
export function exigeAdminNaLoja(usuario: UsuarioAutenticado, lojaId: string, acao: string): void {
  if (usuario.lojas.get(lojaId) !== "admin") {
    throw new SemPermissao(acao);
  }
}

/** Lojas em que o usuário é admin. Base da visibilidade das rotas de usuário. */
export function lojasAdministradas(usuario: UsuarioAutenticado): string[] {
  return [...usuario.lojas.entries()].filter(([, papel]) => papel === "admin").map(([id]) => id);
}
