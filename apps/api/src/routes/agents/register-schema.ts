import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import type { ErrorObject } from "ajv";

// Mesmo padrão de src/config/schema-validator.ts: valida requisição e resposta de
// POST /v1/agents/register contra @lince/shared, em vez de reimplementar o contrato
// em TS (packages/shared/README.md).

const requireLocal = createRequire(import.meta.url);

function carregaSchema(especificador: string): object {
  const caminho = requireLocal.resolve(especificador);
  return JSON.parse(readFileSync(caminho, "utf-8"));
}

const requestSchema = carregaSchema("@lince/shared/schemas/agent-register.v1.json");
const responseSchema = carregaSchema("@lince/shared/schemas/agent-register-response.v1.json");
const commonSchema = carregaSchema("@lince/shared/schemas/common.v1.json");

// Mesma interop via createRequire descrita em schema-validator.ts: o submódulo CJS
// ajv/dist/2020 não expõe uma construct signature utilizável a partir de import ESM
// direto sob moduleResolution NodeNext.
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
const validateRequest = ajv.compile(requestSchema);
const validateResponse = ajv.compile(responseSchema);

export type ResultadoValidacao = { valido: true } | { valido: false; erros: ErrorObject[] };

export function validaRequisicaoRegister(corpo: unknown): ResultadoValidacao {
  if (validateRequest(corpo)) {
    return { valido: true };
  }
  return { valido: false, erros: validateRequest.errors ?? [] };
}

export function validaRespostaRegister(documento: unknown): ResultadoValidacao {
  if (validateResponse(documento)) {
    return { valido: true };
  }
  return { valido: false, erros: validateResponse.errors ?? [] };
}
