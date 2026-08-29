import type { Evento, Fila } from "../api/tipos.js";

// A acumulação das páginas da fila, como função pura. Fora do componente pelo mesmo
// motivo de estado-clipe.ts: é regra, não desenho, e regra se testa sem montar tela.
//
// A fila cresce **por cima** enquanto alguém a percorre -- o agente não para de mandar
// evento porque o gerente abriu o app. É por isso que a API pagina por cursor e não por
// OFFSET (§4.5), e é por isso que juntar páginas aqui precisa de cuidado: entre o pedido
// da página 1 e o da página 2, eventos novos entraram no topo.

export interface EstadoDaFila {
  eventos: Evento[];
  /** Nulo quando não há mais página. */
  proximoCursor: string | null;
}

export const FILA_VAZIA: EstadoDaFila = { eventos: [], proximoCursor: null };

/**
 * Junta a página recém-chegada ao que já está na tela.
 *
 * Deduplica por `event_id` mantendo a **primeira** ocorrência. O caso real: o gerente
 * decide um evento, a lista é recarregada do topo e a página seguinte, pedida com um
 * cursor de antes, repete alguém. Repetido na tela é pior do que parece — a mesma
 * ocorrência aparecendo duas vezes faz o gerente achar que houve dois furtos.
 */
export function acumulaPagina(atual: EstadoDaFila, pagina: Fila): EstadoDaFila {
  const vistos = new Set(atual.eventos.map((evento) => evento.event_id));
  const novos = pagina.eventos.filter((evento) => !vistos.has(evento.event_id));

  return {
    eventos: [...atual.eventos, ...novos],
    proximoCursor: pagina.proximo_cursor,
  };
}

/**
 * Tira um evento da fila depois de decidido. A fila mostra o que **falta** decidir
 * (`triagem=pendentes`), então o evento sai da tela na hora, sem esperar recarga: quem
 * está triando trinta alertas precisa ver a pilha diminuir.
 */
export function removeDaFila(atual: EstadoDaFila, eventId: string): EstadoDaFila {
  return {
    ...atual,
    eventos: atual.eventos.filter((evento) => evento.event_id !== eventId),
  };
}
