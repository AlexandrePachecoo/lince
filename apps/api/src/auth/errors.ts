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
