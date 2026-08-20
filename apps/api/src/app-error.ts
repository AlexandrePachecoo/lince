// Erro tipado com status HTTP explícito. O error handler global (server.ts) traduz
// qualquer AppError para { status, body } e deixa tudo o mais não mapeado virar 500 —
// nunca o contrário: um bug interno não pode escapar como 401/403 por acidente, e um
// erro de auth não pode escapar como 500 (o agente trataria como TENTAR_DEPOIS quando
// deveria ser RECUSADA — ver apps/agent/src/lince_agent/outbox/policy.py).
export abstract class AppError extends Error {
  abstract readonly status: number;
}
