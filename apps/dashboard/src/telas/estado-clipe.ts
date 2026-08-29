import type { Evento } from "../api/tipos.js";

// O que a tela do evento faz com o clipe, como função pura -- fora do componente porque é
// aqui que mora a distinção mais fácil de errar da §4.5, e ela merece teste próprio.
//
// `clip_state` é o desfecho do **upload** (os bytes chegaram ao bucket?); `clip.status` é
// o desfecho do **corte** na borda. São campos com nomes parecidos e perguntas
// diferentes, e confundi-los é a armadilha registrada no CLAUDE.md: um evento cortado com
// `status: "ok"` pode perfeitamente estar com o upload pendente.

export type ApresentacaoClipe =
  /** Os bytes estão no bucket: pede a URL assinada e toca. */
  | { tipo: "player" }
  /** A nuvem conhece o evento e o clipe ainda está subindo da loja. Vale esperar. */
  | { tipo: "subindo" }
  /** Não vai haver vídeo. Esperar é perder tempo; o motivo diz o que o técnico faz. */
  | { tipo: "sem_video"; motivo: string };

const MOTIVO_SEM_DETALHE = "o clipe não chegou, e a loja não disse por quê";

export function apresentacaoDoClipe(evento: Evento): ApresentacaoClipe {
  switch (evento.clip_state) {
    case "disponivel":
      return { tipo: "player" };
    case "pendente":
      return { tipo: "subindo" };
    case "indisponivel":
      // `clip_error` é o que distingue `clipe despejado pelo teto de disco` de `câmera
      // reconectou no meio do corte` -- ações diferentes do técnico. Quando vem vazio, a
      // tela ainda tem que dizer alguma coisa: um espaço em branco no lugar do vídeo é
      // indistinguível de um app quebrado.
      return { tipo: "sem_video", motivo: evento.clip_error ?? MOTIVO_SEM_DETALHE };
  }
}

/**
 * Se o triador pode decidir agora. É **sempre** sim, e a constante existe para que
 * mudar isso seja uma decisão consciente com este comentário na frente: travar a decisão
 * enquanto o clipe sobe deixa o gerente esperando por um upload que pode nunca chegar, e
 * evento sem triagem é dívida operacional visível (§4.5). Vídeo ajuda a decidir; não é
 * pré-requisito para decidir.
 */
export function decisaoLiberada(_evento: Evento): boolean {
  return true;
}

/** Se vale voltar a perguntar pelo evento: só enquanto os bytes ainda podem chegar. */
export function valeReconsultar(evento: Evento): boolean {
  return evento.clip_state === "pendente";
}
