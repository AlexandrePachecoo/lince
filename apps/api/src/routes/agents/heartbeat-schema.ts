import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import type { ErrorObject } from "ajv";

// Mesmo padrão de src/config/schema-validator.ts e register-schema.ts: valida o corpo
// de POST /v1/agents/heartbeat contra @lince/shared, em vez de reimplementar o
// contrato em TS (packages/shared/README.md). Não há resposta com corpo aqui (204),
// então só existe validador de requisição.

const requireLocal = createRequire(import.meta.url);

function carregaSchema(especificador: string): object {
  const caminho = requireLocal.resolve(especificador);
  return JSON.parse(readFileSync(caminho, "utf-8"));
}

const heartbeatSchema = carregaSchema("@lince/shared/schemas/heartbeat.v1.json");
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
const validateRequest = ajv.compile(heartbeatSchema);

export type ResultadoValidacao = { valido: true } | { valido: false; erros: ErrorObject[] };

export function validaRequisicaoHeartbeat(corpo: unknown): ResultadoValidacao {
  if (validateRequest(corpo)) {
    return { valido: true };
  }
  return { valido: false, erros: validateRequest.errors ?? [] };
}
