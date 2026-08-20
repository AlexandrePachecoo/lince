// Serialização determinística: mesmo conteúdo semântico -> sempre os mesmos bytes.
// É a base do ETag/config_version (etag.ts) -- sem isso, duas leituras do mesmo
// conteúdo em ordens diferentes de retorno do banco (ou de inserção de chaves em JS)
// produziriam hashes diferentes, e o agente baixaria o documento inteiro a cada poll
// de 30s mesmo quando nada mudou (§5.4 exige ETag estável para o mesmo conteúdo).
//
// Chaves ordenadas ordinalmente (por code point), recursivo. Arrays preservam a ordem
// em que chegam -- é responsabilidade de quem monta o valor (document-builder.ts)
// entregar arrays já em ordem estável (ex.: câmeras por camera_id).
export function canonicalize(value: unknown): string {
  return serializa(value);
}

function serializa(value: unknown): string {
  if (value === null || value === undefined) {
    return "null";
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(serializa).join(",")}]`;
  }
  if (typeof value === "object") {
    const chaves = Object.keys(value as Record<string, unknown>).sort();
    const pares = chaves.map((chave) => {
      const valorSerializado = serializa((value as Record<string, unknown>)[chave]);
      return `${JSON.stringify(chave)}:${valorSerializado}`;
    });
    return `{${pares.join(",")}}`;
  }
  throw new TypeError(`valor não serializável em canonicalize: ${typeof value}`);
}
