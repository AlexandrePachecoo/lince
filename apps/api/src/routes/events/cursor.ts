import { RequisicaoInvalida } from "../../auth/errors.js";

// Paginação por cursor (keyset), e não por OFFSET. A diferença não é desempenho: a fila
// de triagem cresce por cima enquanto alguém a percorre, e com OFFSET cada evento novo
// empurra um antigo para uma página que já foi lida -- some da tela sem ninguém decidir
// nada. Evento sem triagem é dívida operacional visível (§4.5), e uma paginação que
// esconde eventos em silêncio é a forma mais fácil de tornar essa dívida invisível.
//
// A chave é (ocorrido_em, event_id) e não ocorrido_em sozinho: dois eventos de câmeras
// diferentes podem ter o mesmo instante, e um cursor ambíguo pula ou repete um deles.

export interface PosicaoFila {
  ocorridoEm: Date;
  eventId: string;
}

const SEPARADOR = "|";

export function codificaCursor(posicao: PosicaoFila): string {
  return Buffer.from(
    `${posicao.ocorridoEm.toISOString()}${SEPARADOR}${posicao.eventId}`,
    "utf-8",
  ).toString("base64url");
}

/**
 * Decodifica o cursor devolvido pela página anterior. Cursor corrompido é 400, e não um
 * silencioso "começa do início": voltar ao topo sem avisar faria o dashboard repetir a
 * primeira página para sempre, o que parece a fila não andar.
 */
export function decodificaCursor(texto: string): PosicaoFila {
  const cru = Buffer.from(texto, "base64url").toString("utf-8");
  const corte = cru.indexOf(SEPARADOR);
  if (corte <= 0) {
    throw new RequisicaoInvalida("cursor inválido");
  }
  const ocorridoEm = new Date(cru.slice(0, corte));
  const eventId = cru.slice(corte + SEPARADOR.length);
  if (Number.isNaN(ocorridoEm.getTime()) || eventId.length === 0) {
    throw new RequisicaoInvalida("cursor inválido");
  }
  return { ocorridoEm, eventId };
}
