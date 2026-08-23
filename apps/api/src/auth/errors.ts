import { AppError } from "../app-error.js";

// Mapeamento deliberado (ver policy.py do agente, classify_config_response): qualquer
// um destes precisa cair em 4xx fora de {408, 429} para o agente classificar como
// RECUSADA (erro "humano": credencial errada), não TENTAR_DEPOIS (instabilidade).
// Nunca 404 -- não há id de recurso na URL desta rota, identidade é só o header.

export class TokenAusente extends AppError {
  readonly status = 401;
  constructor() {
    super("Authorization: Bearer <token> ausente");
  }
}

export class TokenInvalido extends AppError {
  readonly status = 401;
  constructor() {
    super("token não reconhecido");
  }
}

export class AgenteInativo extends AppError {
  readonly status = 403;
  constructor() {
    super("agente desativado");
  }
}

// Erros do POST /v1/agents/register (§5.1). BootstrapTokenJaUsado é 409, não 401: não
// é credencial errada, é conflito de estado -- vale distinguir de TokenInvalido para
// quem depura um provisionamento que falhou (token válido, mas já trocado antes).
export class BootstrapTokenInvalido extends AppError {
  readonly status = 401;
  constructor() {
    super("token de bootstrap não reconhecido");
  }
}

export class BootstrapTokenExpirado extends AppError {
  readonly status = 401;
  constructor() {
    super("token de bootstrap expirado");
  }
}

export class BootstrapTokenJaUsado extends AppError {
  readonly status = 409;
  constructor() {
    super("token de bootstrap já foi usado");
  }
}

// A credencial do agente é escopada a UMA loja (§5.1), e o POST /v1/events traz
// tenant_id/store_id no corpo. Divergirem é o caso que o NFR-6 existe para impedir.
//
// 403, e não 400, é decisão de qual lado errar: classify_response (outbox/policy.py)
// manda 400 para a fila morta e 403 para retry lento. Um box provisionado com o
// tenant errado é erro humano de instalação, e os eventos que ele está mandando são
// reais -- descartar o dia inteiro da loja enquanto alguém não conserta o cadastro é
// pior do que uma fila insistindo até o conserto chegar.
export class EscopoDoAgente extends AppError {
  readonly status = 403;
  constructor() {
    super("evento não pertence à loja desta credencial");
  }
}

export class RequisicaoInvalida extends AppError {
  readonly status = 400;
}
