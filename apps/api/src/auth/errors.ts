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

export class RequisicaoInvalida extends AppError {
  readonly status = 400;
}
