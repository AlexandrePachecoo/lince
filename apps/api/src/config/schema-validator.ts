import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import type { ErrorObject } from "ajv";

// Defesa em profundidade: o documento que document-builder.ts monta já deveria bater
// com packages/shared/schemas/config.v1.json por construção, mas validar aqui garante
// que a API NUNCA sirva algo que o próprio agente (config_loader.py, contra o mesmo
// schema) recusaria -- é o par, do lado da API, do que test_contrato_config.py garante
// do lado do agente. Consome o JSON Schema direto de @lince/shared: reimplementar o
// contrato em TS seria o erro mais caro do projeto (packages/shared/README.md).

const requireLocal = createRequire(import.meta.url);

function carregaSchema(especificador: string): object {
  const caminho = requireLocal.resolve(especificador);
  return JSON.parse(readFileSync(caminho, "utf-8"));
}

const configSchema = carregaSchema("@lince/shared/schemas/config.v1.json");
const commonSchema = carregaSchema("@lince/shared/schemas/common.v1.json");

// Requerido via createRequire (não import ESM): a interop de tipos do submódulo CJS
// ajv/dist/2020 sob moduleResolution NodeNext não expõe uma construct signature
// utilizável a partir de import ESM direto -- o import type abaixo preserva a
// tipagem de ErrorObject sem esse problema (import type não passa pela interop).
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
const validate = ajv.compile(configSchema);

export type ResultadoValidacao = { valido: true } | { valido: false; erros: ErrorObject[] };

export function validaDocumentoConfig(documento: unknown): ResultadoValidacao {
  if (validate(documento)) {
    return { valido: true };
  }
  return { valido: false, erros: validate.errors ?? [] };
}
