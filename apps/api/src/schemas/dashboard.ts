import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import type { ErrorObject } from "ajv";

// Validação dos payloads do dashboard (§4.5), do mesmo jeito que routes/events/schemas.ts
// faz com os do agente: contra os arquivos de @lince/shared, nunca contra uma cópia em
// TypeScript.
//
// Estes são os primeiros documentos da §5 que **não** falam com o agente Python -- do
// outro lado está o PWA, que é TypeScript e poderia, em tese, importar um tipo. Ainda
// assim moram em packages/shared, e a razão é a que o README daquele pacote já dá: o
// custo que se quer evitar não é o de escrever o tipo duas vezes, é o de a API e o
// cliente discordarem sobre o contrato sem ninguém perceber. Isso não depende de os
// dois lados falarem a mesma linguagem.
//
// Um único Ajv para todos eles, e não um por rota: os schemas se referenciam entre si
// (triagem.v1.json aponta para o enum de decisão em fila-triagem.v1.json, e as duas
// respostas de usuário compartilham $defs/usuario). $ref entre arquivos só resolve
// dentro da mesma instância.

const requireLocal = createRequire(import.meta.url);

function carregaSchema(especificador: string): object {
  const caminho = requireLocal.resolve(especificador);
  return JSON.parse(readFileSync(caminho, "utf-8"));
}

// Mesma interop via createRequire descrita em src/config/schema-validator.ts: o
// submódulo CJS ajv/dist/2020 não expõe uma construct signature utilizável a partir de
// import ESM direto sob moduleResolution NodeNext.
type Ajv2020Ctor = new (opts?: { allErrors?: boolean; strict?: boolean }) => {
  addSchema(schema: object): void;
  compile(schema: object): {
    (documento: unknown): boolean;
    errors?: ErrorObject[] | null;
  };
};
const Ajv2020 = requireLocal("ajv/dist/2020.js") as Ajv2020Ctor;

const ajv = new Ajv2020({ allErrors: true, strict: true });
ajv.addSchema(carregaSchema("@lince/shared/schemas/common.v1.json"));

// Estes dois são compilados primeiro porque os outros apontam para os $defs deles, e
// compilar já registra o $id na instância -- chamar addSchema além disso duplicaria a
// chave e estouraria na subida. A ordem aqui é a ordem das dependências.
const validaUsuario = ajv.compile(carregaSchema("@lince/shared/schemas/usuario.v1.json"));
const validaFila = ajv.compile(carregaSchema("@lince/shared/schemas/fila-triagem.v1.json"));

const validaLogin = ajv.compile(carregaSchema("@lince/shared/schemas/auth-login.v1.json"));
const validaSessao = ajv.compile(carregaSchema("@lince/shared/schemas/auth-sessao.v1.json"));
const validaUsuarioNovo = ajv.compile(carregaSchema("@lince/shared/schemas/usuario-novo.v1.json"));
const validaUsuarioAlteracao = ajv.compile(
  carregaSchema("@lince/shared/schemas/usuario-alteracao.v1.json"),
);
const validaUsuarios = ajv.compile(carregaSchema("@lince/shared/schemas/usuarios.v1.json"));
const validaTriagem = ajv.compile(carregaSchema("@lince/shared/schemas/triagem.v1.json"));
const validaTriagemAceita = ajv.compile(
  carregaSchema("@lince/shared/schemas/triagem-aceita.v1.json"),
);
const validaClipeUrl = ajv.compile(carregaSchema("@lince/shared/schemas/clipe-url.v1.json"));

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

/** Corpo do `POST /v1/auth/login`. */
export function validaRequisicaoLogin(corpo: unknown): ResultadoValidacao {
  return resultado(validaLogin, corpo);
}

/** Resposta do `POST /v1/auth/login`. */
export function validaRespostaSessao(documento: unknown): ResultadoValidacao {
  return resultado(validaSessao, documento);
}

/** Corpo do `POST /v1/usuarios`. */
export function validaRequisicaoUsuarioNovo(corpo: unknown): ResultadoValidacao {
  return resultado(validaUsuarioNovo, corpo);
}

/** Corpo do `PATCH /v1/usuarios/{id}`. */
export function validaRequisicaoUsuarioAlteracao(corpo: unknown): ResultadoValidacao {
  return resultado(validaUsuarioAlteracao, corpo);
}

/** Resposta com um usuário (`POST /v1/usuarios`, `PATCH /v1/usuarios/{id}`). */
export function validaRespostaUsuario(documento: unknown): ResultadoValidacao {
  return resultado(validaUsuario, documento);
}

/** Resposta do `GET /v1/usuarios`. */
export function validaRespostaUsuarios(documento: unknown): ResultadoValidacao {
  return resultado(validaUsuarios, documento);
}

/** Resposta do `GET /v1/events`. */
export function validaRespostaFila(documento: unknown): ResultadoValidacao {
  return resultado(validaFila, documento);
}

/** Corpo do `POST /v1/events/{event_id}/triagem`. */
export function validaRequisicaoTriagem(corpo: unknown): ResultadoValidacao {
  return resultado(validaTriagem, corpo);
}

/** Resposta do `POST /v1/events/{event_id}/triagem`. */
export function validaRespostaTriagem(documento: unknown): ResultadoValidacao {
  return resultado(validaTriagemAceita, documento);
}

/** Resposta do `GET /v1/events/{event_id}/clip-url`. */
export function validaRespostaClipeUrl(documento: unknown): ResultadoValidacao {
  return resultado(validaClipeUrl, documento);
}
