import { describe, expect, test } from "vitest";
import {
  apresentacaoDoClipe,
  decisaoLiberada,
  valeReconsultar,
} from "../src/telas/estado-clipe.js";
import { evento } from "./fixturas.js";

// A distinção mais fácil de errar da §4.5, e a que custa mais caro: `clip_state` é o
// desfecho do **upload**, `clip.status` é o desfecho do **corte** na borda.

describe("o que a tela faz com o clipe", () => {
  test("bytes no bucket -> player", () => {
    expect(apresentacaoDoClipe(evento({ clip_state: "disponivel" }))).toEqual({ tipo: "player" });
  });

  test("upload pendente -> espera, e é o único estado que vale reconsultar", () => {
    // `pendente` é o que o contrato do agente não nomeia: a nuvem conhece o evento,
    // emitiu a URL, e os bytes ainda não chegaram. Vale esperar; nos outros dois não.
    const subindo = evento({ clip_state: "pendente" });

    expect(apresentacaoDoClipe(subindo)).toEqual({ tipo: "subindo" });
    expect(valeReconsultar(subindo)).toBe(true);
    expect(valeReconsultar(evento({ clip_state: "disponivel" }))).toBe(false);
    expect(valeReconsultar(evento({ clip_state: "indisponivel" }))).toBe(false);
  });

  test("clipe cortado bem pode estar com o upload pendente", () => {
    // A armadilha registrada no CLAUDE.md, do lado de quem lê. Se a tela olhasse
    // `clip.status` para decidir se mostra o vídeo, mostraria um player apontando para
    // um objeto que ainda não existe no bucket.
    const cortouBemMasNaoSubiu = evento({
      clip_state: "pendente",
      clip: { status: "ok", duration_s: 15 },
    });

    expect(apresentacaoDoClipe(cortouBemMasNaoSubiu)).toEqual({ tipo: "subindo" });
  });

  test("não vai haver vídeo -> o motivo vai para a tela", () => {
    // É o que distingue `clipe despejado pelo teto de disco` de `câmera reconectou no
    // meio do corte`: ações diferentes do técnico. Sem o motivo, `indisponivel` é
    // indistinguível de "ainda está subindo", e o gerente espera à toa.
    const semVideo = evento({
      clip_state: "indisponivel",
      clip_error: "buffer despejado pelo teto de disco",
    });

    expect(apresentacaoDoClipe(semVideo)).toEqual({
      tipo: "sem_video",
      motivo: "buffer despejado pelo teto de disco",
    });
  });

  test("sem vídeo e sem motivo ainda diz alguma coisa", () => {
    // Um espaço em branco no lugar do vídeo é indistinguível de um app quebrado, e o
    // gerente fecha o app em vez de decidir.
    const apresentacao = apresentacaoDoClipe(
      evento({ clip_state: "indisponivel", clip_error: null }),
    );

    expect(apresentacao.tipo).toBe("sem_video");
    expect(apresentacao.tipo === "sem_video" && apresentacao.motivo.length).toBeGreaterThan(0);
  });

  test("nenhum estado do clipe trava a decisão", () => {
    // O ponto do §4.5: vídeo ajuda a decidir, não é pré-requisito para decidir. Travar em
    // `pendente` deixa o evento sem triagem para sempre quando o upload não vem -- e
    // evento sem triagem é dívida operacional visível, não um estado silencioso.
    for (const estado of ["pendente", "disponivel", "indisponivel"] as const) {
      expect(decisaoLiberada(evento({ clip_state: estado })), estado).toBe(true);
    }
  });
});
