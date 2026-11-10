import type { EventoOrigem } from "@prisma/client";
import type { EventoParaAlerta, LojaDoAlerta } from "./decide.js";

// O texto que aparece na tela de bloqueio do gerente (notificacao.v1.json).
//
// Atravessa o serviço de push do fabricante do celular — cifrado, mas fora do control
// plane — e por isso leva só o necessário para decidir abrir: câmera, gatilho e hora.
// Nada de clipe, URL assinada ou imagem (R-9). Quem abre cai na tela do evento, que
// exige a sessão do dashboard.

export interface ConteudoNotificacao {
  schema_version: 1;
  tipo: "alerta" | "teto";
  event_id: string | null;
  titulo: string;
  corpo: string;
  url: string;
  tag: string;
}

const GATILHOS: Record<EventoOrigem, string> = {
  rule: "saiu sem passar no caixa",
  gesture: "gesto de esconder",
  manual: "teste de instalação",
};

/** Hora local da loja, como o gerente lê no relógio da parede. */
export function horaDaLoja(instante: Date, fuso: string): string {
  return new Intl.DateTimeFormat("pt-BR", {
    timeZone: fuso,
    hour: "2-digit",
    minute: "2-digit",
  }).format(instante);
}

export function conteudoDoAlerta(
  evento: EventoParaAlerta,
  loja: LojaDoAlerta,
): ConteudoNotificacao {
  return {
    schema_version: 1,
    tipo: "alerta",
    event_id: evento.eventId,
    titulo: `Possível furto — ${evento.cameraId}`,
    corpo: `${GATILHOS[evento.source]} · ${loja.nome} · ${horaDaLoja(evento.ocorridoEm, loja.fusoHorario)}`,
    url: `/eventos/${evento.eventId}`,
    // Por câmera: a notificação seguinte da mesma câmera substitui a anterior na
    // bandeja, em vez de empilhar. Com a janela de agrupamento isso raramente acontece,
    // mas quando a janela vence no meio de uma rajada a bandeja continua com uma linha.
    tag: `lince-${evento.lojaId}-${evento.cameraId}`,
  };
}

export function conteudoDoTeto(
  loja: LojaDoAlerta,
  lojaId: string,
  teto: number,
): ConteudoNotificacao {
  return {
    schema_version: 1,
    tipo: "teto",
    event_id: null,
    titulo: `${loja.nome}: limite de alertas do dia`,
    corpo: `${teto} alertas hoje. Os próximos ficam só na fila — abra para triar.`,
    url: "/fila",
    tag: `lince-${lojaId}-teto`,
  };
}
