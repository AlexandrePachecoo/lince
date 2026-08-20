import type { ConfigDocumento } from "./document-builder.js";

// O agente trata o ETag como opaco e só o ecoa de volta em If-None-Match (§5.4) --
// mas o header pode carregar aspas, prefixo fraco (W/) ou uma lista (RFC 7232), então
// a comparação aqui normaliza os dois lados em vez de depender de igualdade de string
// exata do valor bruto do header.

// ETag e config_version são o MESMO hash (document-builder.ts calcula
// config_version); calculaEtag só formata como ETag forte (aspas), sem recalcular
// nada -- uma fonte de verdade, não duas rotinas de hash a manter sincronizadas.
export function calculaEtag(documento: ConfigDocumento): string {
  return `"${documento.config_version}"`;
}

export function etagBate(ifNoneMatch: string | undefined, etagAtual: string): boolean {
  if (!ifNoneMatch) {
    return false;
  }
  if (ifNoneMatch.trim() === "*") {
    return true;
  }
  const candidatos = ifNoneMatch.split(",").map((valor) => normaliza(valor));
  return candidatos.includes(normaliza(etagAtual));
}

function normaliza(etag: string): string {
  return etag.trim().replace(/^W\//, "");
}
