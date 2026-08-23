import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import type { ErrorObject } from "ajv";

// Mesmo padrão de src/config/schema-validator.ts: os três documentos do caminho do
// evento saem de @lince/shared, não de uma cópia em TS. Duplicar o contrato entre a
// API e o agente é o erro mais caro do projeto (packages/shared/README.md) -- e aqui
// ele seria especialmente caro, porque a divergência apareceria como evento indo para
// a fila morta na loja, longe de quem editou o tipo.
//
// Os três, e não só o de entrada: a resposta do POST também é contrato. O agente
// depende dela para renovar uma URL de upload vencida, e uma resposta malformada
// deixaria o clipe preso na fila para sempre (event-accepted.v1.json diz isso na
// própria descrição).

const requireLocal = createRequire(import.meta.url);

function carregaSchema(especificador: string): object {
  const caminho = requireLocal.resolve(especificador);
  return JSON.parse(readFileSync(caminho, "utf-8"));
}

const eventoSchema = carregaSchema("@lince/shared/schemas/event.v1.json");
const aceitoSchema = carregaSchema("@lince/shared/schemas/event-accepted.v1.json");
const clipeSchema = carregaSchema("@lince/shared/schemas/event-clip.v1.json");
const commonSchema = carregaSchema("@lince/shared/schemas/common.v1.json");

// Mesma interop via createRequire descrita em src/config/schema-validator.ts: o
// submódulo CJS ajv/dist/2020 não expõe uma construct signature utilizável a partir
// de import ESM direto sob moduleResolution NodeNext.
type Ajv2020Ctor = new (opts?: { allErrors?: boolean; strict?: boolean }) => {
  addSchema(schema: object): void;
  compile(schema: object): {
    (documento: unknown): boolean;
    errors?: ErrorObject[] | null;
  };
};
const Ajv2020 = requireLocal("ajv/dist/2020.js") as Ajv2020Ctor;

const ajv = new Ajv2020({ allErrors: true, strict: true });
ajv.addSchema(commonSchema);
const validaEvento = ajv.compile(eventoSchema);
const validaAceito = ajv.compile(aceitoSchema);
const validaClipe = ajv.compile(clipeSchema);

export type ResultadoValidacao = { valido: true } | { valido: false; erros: ErrorObject[] };

function resultado(
  validador: { (documento: unknown): boolean; errors?: ErrorObject[] | null },
  documento: unknown,
): ResultadoValidacao {
  if (validador(documento)) {
    return { valido: true };
  }
  return { valido: false, erros: validador.errors ?? [] };
}

/** Corpo do `POST /v1/events`. */
export function validaRequisicaoEvento(corpo: unknown): ResultadoValidacao {
  return resultado(validaEvento, corpo);
}

/** Resposta do `POST /v1/events`. */
export function validaRespostaEvento(documento: unknown): ResultadoValidacao {
  return resultado(validaAceito, documento);
}

/** Corpo do `PATCH /v1/events/{event_id}`. */
export function validaRequisicaoClipe(corpo: unknown): ResultadoValidacao {
  return resultado(validaClipe, corpo);
}
