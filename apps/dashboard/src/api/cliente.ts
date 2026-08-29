import type {
  ClipeUrl,
  Decisao,
  Evento,
  EventoDetalhe,
  Fila,
  MetricaFalsoPositivo,
  Sessao,
} from "./tipos.js";

// O acesso à API, num lugar só. Caminho relativo sempre: em desenvolvimento o Vite faz o
// proxy de /v1 (vite.config.ts) e em produção o dashboard é servido ao lado da API. Uma
// URL de base configurável no build é o tipo de variável que se esquece de trocar e que
// aponta o dashboard de uma loja para o localhost de alguém.

const PREFIXO = "/v1";

/**
 * Erro de HTTP com o status preservado. Quem chama precisa do número: 401 derruba a
 * sessão, 403 e 404 são telas diferentes, 409 no clipe não é falha nenhuma — é "o vídeo
 * ainda não chegou", e o triador decide sem ele.
 */
export class ErroHttp extends Error {
  constructor(
    readonly status: number,
    mensagem: string,
  ) {
    super(mensagem);
    this.name = "ErroHttp";
  }
}

/** Rede caiu, DNS falhou, o servidor não respondeu. Não é o mesmo que um status ruim: um
 *  tem "tentar de novo" como resposta razoável, o outro não. */
export class ErroDeRede extends Error {
  constructor(causa: unknown) {
    super("não foi possível falar com a nuvem");
    this.name = "ErroDeRede";
    this.cause = causa;
  }
}

interface OpcoesPedido {
  metodo?: "GET" | "POST";
  token?: string;
  corpo?: unknown;
}

async function pede<T>(caminho: string, opcoes: OpcoesPedido = {}): Promise<T> {
  const { metodo = "GET", token, corpo } = opcoes;

  let resposta: Response;
  try {
    resposta = await fetch(`${PREFIXO}${caminho}`, {
      method: metodo,
      headers: {
        ...(token !== undefined ? { authorization: `Bearer ${token}` } : {}),
        ...(corpo !== undefined ? { "content-type": "application/json" } : {}),
      },
      ...(corpo !== undefined ? { body: JSON.stringify(corpo) } : {}),
    });
  } catch (causa) {
    throw new ErroDeRede(causa);
  }

  if (!resposta.ok) {
    // A API responde `{ erro }` (server.ts). Um corpo que não seja esse JSON é proxy,
    // gateway ou API meio implantada -- e nesse caso a mensagem útil é o próprio status,
    // não o HTML de erro de alguém.
    let mensagem = `HTTP ${resposta.status}`;
    try {
      const json = (await resposta.json()) as { erro?: string };
      if (typeof json.erro === "string" && json.erro.length > 0) {
        mensagem = json.erro;
      }
    } catch {
      // corpo não-JSON: fica o status
    }
    throw new ErroHttp(resposta.status, mensagem);
  }

  return (await resposta.json()) as T;
}

export function login(email: string, senha: string): Promise<Sessao> {
  return pede<Sessao>("/auth/login", { metodo: "POST", corpo: { email, senha } });
}

export interface OpcoesFila {
  cursor?: string;
  limite?: number;
}

export function buscaFila(token: string, opcoes: OpcoesFila = {}): Promise<Fila> {
  const parametros = new URLSearchParams();
  // `triagem=pendentes` é o padrão da API, e é o certo aqui: a fila é lista de trabalho,
  // não histórico. Fica explícito na URL para que mudar isso um dia seja uma mudança
  // visível, e não a descoberta de que o padrão do outro lado mudou junto.
  parametros.set("triagem", "pendentes");
  if (opcoes.limite !== undefined) {
    parametros.set("limite", String(opcoes.limite));
  }
  if (opcoes.cursor !== undefined) {
    parametros.set("cursor", opcoes.cursor);
  }
  return pede<Fila>(`/events?${parametros.toString()}`, { token });
}

export function buscaEvento(token: string, eventId: string): Promise<EventoDetalhe> {
  return pede<EventoDetalhe>(`/events/${encodeURIComponent(eventId)}`, { token });
}

export function buscaClipeUrl(token: string, eventId: string): Promise<ClipeUrl> {
  return pede<ClipeUrl>(`/events/${encodeURIComponent(eventId)}/clip-url`, { token });
}

export function buscaMetrica(token: string, dias: number): Promise<MetricaFalsoPositivo> {
  return pede<MetricaFalsoPositivo>(`/metricas/falso-positivo?dias=${dias}`, { token });
}

export function decide(
  token: string,
  eventId: string,
  decisao: Decisao,
  observacao?: string,
): Promise<{ schema_version: 1; evento: Evento }> {
  return pede(`/events/${encodeURIComponent(eventId)}/triagem`, {
    metodo: "POST",
    token,
    corpo: { decisao, ...(observacao !== undefined ? { observacao } : {}) },
  });
}
