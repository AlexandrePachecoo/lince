import type { CameraMetrica, Evento } from "../api/tipos.js";

// O que trouxe o evento para a fila, em palavras de quem tria. Fica fora dos componentes,
// como estado-clipe.ts, porque as três telas falam do gatilho e uma frase diferente em
// cada uma faria o mesmo alerta parecer três coisas.
//
// A distinção não é decorativa. A saída pela porta se confere no fim do clipe, quando a
// pessoa cruza a linha; o gesto se confere no meio dele, quando a mão vai à bolsa. Quem
// não sabe qual dos dois procurar assiste ao trecho errado e chuta a decisão.

export interface DescricaoGatilho {
  /** Uma ou duas palavras, para a etiqueta da fila e a linha da métrica. */
  curto: string;
  /** A frase da ficha do evento: o que olhar no vídeo. */
  detalhe: string;
}

const MAOS = { left: "mão esquerda", right: "mão direita" } as const;

export function descreveGatilho(evento: Evento): DescricaoGatilho {
  switch (evento.source) {
    case "manual":
      return { curto: "teste", detalhe: "teste de instalação" };
    case "rule":
      return {
        curto: "saída",
        detalhe: `saiu sem passar no caixa (${evento.rule?.id ?? "regra da câmera"})`,
      };
    case "gesture":
      return { curto: "gesto", detalhe: detalheDoGesto(evento) };
  }
}

function detalheDoGesto(evento: Evento): string {
  const partes = ["gesto de esconder"];
  const mao = evento.gesture?.hand;
  if (mao === "left" || mao === "right") {
    partes.push(MAOS[mao]);
  }
  const noVideo = inicioDoGestoNoVideo(evento);
  if (noVideo !== null) {
    partes.push(`começa em ${minutoSegundo(noVideo)} do vídeo`);
  }
  return partes.join(" — ");
}

/**
 * Em que segundo do clipe o gesto começa, ou `null` se não dá para saber.
 *
 * O clipe começa `pre_roll_s` antes do disparo, e o gesto começou `started_s_before`
 * antes dele. O pré-roll é o **medido**, não o pedido: uma câmera que acabou de
 * reconectar entrega menos, e usar o pedido mandaria o triador para um segundo em que o
 * gesto ainda não começou — ou já acabou.
 */
export function inicioDoGestoNoVideo(evento: Evento): number | null {
  const antes = evento.gesture?.started_s_before;
  const preRoll = evento.clip?.pre_roll_s;
  if (typeof antes !== "number" || typeof preRoll !== "number") {
    return null;
  }
  return Math.max(0, preRoll - antes);
}

function minutoSegundo(segundos: number): string {
  const inteiro = Math.floor(segundos);
  const minutos = Math.floor(inteiro / 60);
  const resto = inteiro % 60;
  return `${minutos}:${String(resto).padStart(2, "0")}`;
}

/** O rótulo da linha da métrica: a ação que conserta um não conserta o outro. */
export function rotuloDaOrigem(source: CameraMetrica["source"]): string {
  return source === "gesture" ? "gesto" : "saída";
}
