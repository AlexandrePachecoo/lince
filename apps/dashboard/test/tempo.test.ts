import { describe, expect, test } from "vitest";
import { instanteCurto, quandoFoi } from "../src/telas/tempo.js";

// A fila diz "há 4 min" e não "14:32" porque a pergunta que o gerente faz ao ler a fila é
// se ainda dá para agir — olhar a câmera, encontrar alguém no corredor. Isso é distância
// no tempo, não hora do relógio.

const AGORA = Date.parse("2026-08-29T14:00:00.000Z");

function ha(ms: number): string {
  return new Date(AGORA - ms).toISOString();
}

describe("há quanto tempo o evento aconteceu", () => {
  test("menos de um minuto é 'agora'", () => {
    expect(quandoFoi(ha(30_000), AGORA)).toBe("agora");
  });

  test("minutos, horas e dias, cada um na sua faixa", () => {
    expect(quandoFoi(ha(4 * 60_000), AGORA)).toBe("há 4 min");
    expect(quandoFoi(ha(90 * 60_000), AGORA)).toBe("há 1 hora");
    expect(quandoFoi(ha(5 * 3_600_000), AGORA)).toBe("há 5 horas");
    expect(quandoFoi(ha(26 * 3_600_000), AGORA)).toBe("ontem");
    expect(quandoFoi(ha(3 * 86_400_000), AGORA)).toBe("há 3 dias");
  });

  test("relógio do box adiantado não vira 'daqui a 3 min'", () => {
    // O §5.3 mede essa deriva justamente porque ela existe: um box com NTP quebrado
    // reporta `occurred_at` no futuro. "Daqui a 3 min" para um furto que já aconteceu faz
    // o gerente duvidar do app inteiro; "agora" é impreciso do jeito certo.
    const noFuturo = new Date(AGORA + 3 * 60_000).toISOString();

    expect(quandoFoi(noFuturo, AGORA)).toBe("agora");
  });

  test("data impossível não vira 'NaN'", () => {
    // O campo vem da API e é texto. Um valor que o `Date` não entende tem que degradar
    // para o próprio texto -- "há NaN min" na fila parece app quebrado e esconde que o
    // problema está no dado.
    expect(quandoFoi("nem-data-nem-nada", AGORA)).toBe("nem-data-nem-nada");
    expect(instanteCurto("nem-data-nem-nada")).toBe("nem-data-nem-nada");
  });

  test("o instante longo tem dia, mês e hora com segundos", () => {
    // É o `title` da fila e o registro na tela do evento. Os segundos importam: dois
    // eventos da mesma câmera no mesmo minuto são coisas diferentes, e sem eles o
    // gerente não tem como distinguir um do outro.
    const escrito = instanteCurto("2026-08-29T14:03:07.000Z");

    expect(escrito).toMatch(/\d{2}\/\d{2}/);
    expect(escrito).toMatch(/\d{2}:\d{2}:\d{2}/);
  });
});
