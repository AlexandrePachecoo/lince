import { describe, expect, test } from "vitest";
import { descreveGatilho, inicioDoGestoNoVideo, rotuloDaOrigem } from "../src/telas/gatilho.js";
import { evento, eventoDeGesto } from "./fixturas.js";

// A frase que diz ao triador o que procurar no vídeo. Errar aqui não quebra nada na tela:
// manda o gerente assistir o trecho errado e chutar a decisão — e a decisão chutada é a
// que envenena a métrica do R-1.

describe("o que trouxe o evento para a fila", () => {
  test("o gesto diz a mão e em que segundo do vídeo ele começa", () => {
    // pré-roll medido de 5 s, gesto começou 1,25 s antes do disparo: 3,75 s de vídeo.
    expect(descreveGatilho(eventoDeGesto()).detalhe).toBe(
      "gesto de esconder — mão direita — começa em 0:03 do vídeo",
    );
  });

  test("o início no vídeo sai do pré-roll **medido**, não do pedido", () => {
    // Câmera recém-reconectada entrega 2 s de pré-roll em vez de 5. Com o pedido, a tela
    // mandaria para 0:03, um segundo depois de o gesto já ter começado em 0:00.
    const curto = eventoDeGesto({
      clip: { status: "ok", pre_roll_s: 2, pre_roll_requested_s: 5 },
    });
    expect(inicioDoGestoNoVideo(curto)).toBe(0.75);
  });

  test("gesto mais antigo que o pré-roll começa no 0:00, e não num segundo negativo", () => {
    const antigo = eventoDeGesto({
      gesture: { method: { id: "regra-bolsa", version: "1" }, track_id: 7, started_s_before: 9 },
    });
    expect(inicioDoGestoNoVideo(antigo)).toBe(0);
  });

  test("sem clipe ou sem início, a frase não inventa um segundo", () => {
    const semClipe = eventoDeGesto({ clip: null });
    expect(inicioDoGestoNoVideo(semClipe)).toBeNull();
    expect(descreveGatilho(semClipe).detalhe).toBe("gesto de esconder — mão direita");

    const semMao = eventoDeGesto({
      clip: null,
      gesture: { method: { id: "regra-bolsa", version: "1" }, track_id: 7, hand: null },
    });
    expect(descreveGatilho(semMao).detalhe).toBe("gesto de esconder");
  });

  test("a saída e o teste de instalação continuam ditos como antes", () => {
    expect(descreveGatilho(evento()).detalhe).toBe(
      "saiu sem passar no caixa (saida-sem-passar-no-caixa)",
    );
    expect(descreveGatilho(evento({ source: "manual", rule: null })).detalhe).toBe(
      "teste de instalação",
    );
    expect(descreveGatilho(eventoDeGesto()).curto).toBe("gesto");
  });

  test("a métrica rotula cada linha pelo gatilho", () => {
    expect(rotuloDaOrigem("gesture")).toBe("gesto");
    expect(rotuloDaOrigem("rule")).toBe("saída");
  });
});
