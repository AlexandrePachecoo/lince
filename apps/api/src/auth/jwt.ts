import { createHmac, timingSafeEqual } from "node:crypto";

// JWT HS256 escrito à mão, em vez de uma biblioteca. É a decisão contrária à de
// clip-storage.ts (que preferiu aws4fetch a reimplementar SigV4), e o motivo é que
// aqui a superfície é o oposto:
//
// - SigV4 é grande, cheio de casos (canonicalização, payload hash, region scope) e
//   errar é silencioso. Vale biblioteca.
// - Um JWT HS256 com um único emissor, um único segredo e um único algoritmo é um
//   HMAC sobre duas strings em base64url. O que faz CVE em biblioteca de JWT é
//   justamente a generalidade que este uso não tem: `alg: none`, confusão HS/RS,
//   `kid` apontando para arquivo. Aqui o algoritmo é constante do código e o cabeçalho
//   do token **não escolhe nada** -- ele é conferido depois da assinatura, nunca antes.
//
// O que este arquivo não faz, e é de propósito: não valida `aud`, `iss` nem `nbf`. Não
// há federação, o emissor e o verificador são o mesmo processo. Campo que não se usa é
// campo que se confere errado.

const CABECALHO = { alg: "HS256", typ: "JWT" } as const;

// Um Bearer que chega com megabytes não é sessão, é abuso: recusar pelo tamanho antes
// de qualquer parse mantém o custo de um token lixo em O(1).
const TAMANHO_MAXIMO = 4096;

export interface ClaimsSessao {
  /** id do usuário. */
  sub: string;
  /** tenant do usuário no momento da emissão. */
  tid: string;
  /** Usuario.tokenVersao no momento da emissão -- é o que permite revogar (§5.1). */
  ver: number;
  /** emitido em, segundos epoch. */
  iat: number;
  /** expira em, segundos epoch. */
  exp: number;
}

export class TokenMalformado extends Error {}
export class AssinaturaInvalida extends Error {}
export class TokenVencido extends Error {}

function base64url(valor: object): string {
  return Buffer.from(JSON.stringify(valor), "utf-8").toString("base64url");
}

function assinatura(corpo: string, segredo: string): Buffer {
  return createHmac("sha256", segredo).update(corpo).digest();
}

export interface EmiteTokenOpcoes {
  usuarioId: string;
  tenantId: string;
  tokenVersao: number;
  expiraEmS: number;
  agoraMs?: number;
}

export function emiteToken(segredo: string, opcoes: EmiteTokenOpcoes): string {
  const agora = Math.floor((opcoes.agoraMs ?? Date.now()) / 1000);
  const claims: ClaimsSessao = {
    sub: opcoes.usuarioId,
    tid: opcoes.tenantId,
    ver: opcoes.tokenVersao,
    iat: agora,
    exp: agora + opcoes.expiraEmS,
  };
  const corpo = `${base64url(CABECALHO)}.${base64url(claims)}`;
  return `${corpo}.${assinatura(corpo, segredo).toString("base64url")}`;
}

/**
 * Verifica e devolve os claims. Lança `AssinaturaInvalida`, `TokenMalformado` ou
 * `TokenVencido` — quem chama traduz para o HTTP (auth/usuario-token.ts).
 *
 * A ordem das conferências é a defesa: assinatura primeiro, conteúdo depois. Enquanto
 * o HMAC não fecha, nada do token é dado confiável — nem o cabeçalho que diz qual
 * algoritmo usar. É essa inversão que produz a família inteira de furos de `alg`.
 */
export function verificaToken(
  token: string,
  segredo: string,
  agoraMs: number = Date.now(),
): ClaimsSessao {
  if (token.length === 0 || token.length > TAMANHO_MAXIMO) {
    throw new TokenMalformado("tamanho fora do aceitável");
  }
  const partes = token.split(".");
  if (partes.length !== 3) {
    throw new TokenMalformado("não tem três segmentos");
  }
  const [cabecalhoB64, claimsB64, assinaturaB64] = partes as [string, string, string];

  const esperada = assinatura(`${cabecalhoB64}.${claimsB64}`, segredo);
  const recebida = Buffer.from(assinaturaB64, "base64url");
  if (recebida.length !== esperada.length || !timingSafeEqual(recebida, esperada)) {
    throw new AssinaturaInvalida("assinatura não confere");
  }

  // Só agora o conteúdo é confiável.
  const cabecalho = analisaJson(cabecalhoB64);
  if (cabecalho.alg !== CABECALHO.alg || cabecalho.typ !== CABECALHO.typ) {
    // Assinado com o nosso segredo e ainda assim recusado: é token de uma versão
    // futura do formato, não ataque. Recusar é o lado certo de errar.
    throw new TokenMalformado("cabeçalho não é HS256/JWT");
  }

  const claims = analisaJson(claimsB64);
  if (
    typeof claims.sub !== "string" ||
    typeof claims.tid !== "string" ||
    typeof claims.ver !== "number" ||
    typeof claims.iat !== "number" ||
    typeof claims.exp !== "number"
  ) {
    throw new TokenMalformado("claims incompletos");
  }
  if (claims.exp * 1000 <= agoraMs) {
    throw new TokenVencido("sessão expirada");
  }

  return {
    sub: claims.sub,
    tid: claims.tid,
    ver: claims.ver,
    iat: claims.iat,
    exp: claims.exp,
  };
}

function analisaJson(segmento: string): Record<string, unknown> {
  let cru: unknown;
  try {
    cru = JSON.parse(Buffer.from(segmento, "base64url").toString("utf-8"));
  } catch {
    throw new TokenMalformado("segmento não é JSON");
  }
  if (typeof cru !== "object" || cru === null || Array.isArray(cru)) {
    throw new TokenMalformado("segmento não é objeto");
  }
  return cru as Record<string, unknown>;
}
