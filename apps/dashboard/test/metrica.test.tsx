import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { Metrica } from "../src/telas/Metrica.js";
import { cameraMetrica, metrica } from "./fixturas.js";

// A tela do R-1. O que ela precisa acertar não é o desenho: é não deixar o gerente sair
// dela com a impressão errada sobre qual câmera está ruim. Duas formas de errar isso, e
// as duas têm teste aqui — esconder a incerteza dos pendentes, e julgar pela média.

let chamadas: string[];

function montaRede(resposta: ReturnType<typeof metrica>) {
  chamadas = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      chamadas.push(url);
      return { ok: true, status: 200, json: async () => resposta } as Response;
    }),
  );
}

function abre() {
  return render(
    <MemoryRouter>
      <Metrica token="token-de-teste" />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  chamadas = [];
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("a tela de falso positivo por câmera", () => {
  test("a câmera fora do requisito diz o pior dia por extenso", async () => {
    // O que julga a NFR-2 é o teto **diário**, e é o dia da rajada que o gerente lembra
    // de ter recebido. Mostrar só a média deixaria a tela discordar do requisito que ela
    // existe para vigiar.
    montaRede(
      metrica([
        cameraMetrica({
          camera_id: "cam3",
          acima_do_limite: true,
          pior_dia: { dia: "2026-08-27", falso_positivo: 9 },
        }),
      ]),
    );
    abre();

    expect(await screen.findByText(/9 num só dia \(2026-08-27\)/)).toBeInTheDocument();
    expect(screen.getByText(/o limite é 3/)).toBeInTheDocument();
  });

  test("uma câmera dentro do limite não vira alarme", async () => {
    // Se toda câmera aparecesse marcada, a marca deixaria de significar alguma coisa e a
    // tela viraria ruído — que é como um painel morre.
    montaRede(
      metrica([cameraMetrica({ camera_id: "cam1", acima_do_limite: false, falso_positivo: 2 })]),
    );
    abre();

    expect(await screen.findByText("cam1")).toBeInTheDocument();
    expect(screen.queryByText(/num só dia/)).not.toBeInTheDocument();
    expect(screen.queryByText(/passou do limite/)).not.toBeInTheDocument();
  });

  test("câmera não triada mostra a ressalva, e não passa por câmera boa", async () => {
    // O jeito mais fácil de esta tela mentir: uma câmera que ninguém triou tem zero falso
    // positivo, cai para o fim da lista e parece a melhor da loja. A tela tem que dizer
    // que aquele zero é desconhecimento, não qualidade.
    montaRede(
      metrica([
        cameraMetrica({
          camera_id: "cam9",
          falso_positivo: 0,
          confirmado: 0,
          inconclusivo: 0,
          pendentes: 40,
          eventos: 40,
          pior_dia: null,
          acima_do_limite: false,
        }),
      ]),
    );
    abre();

    expect(await screen.findByText(/40 de 40 ainda sem triagem/)).toBeInTheDocument();
    expect(screen.getByText(/conta só o que foi decidido/)).toBeInTheDocument();
  });

  test("câmera inteiramente triada não ganha ressalva", async () => {
    montaRede(metrica([cameraMetrica({ pendentes: 0 })]));
    abre();

    expect(await screen.findByText("cam3")).toBeInTheDocument();
    expect(screen.queryByText(/sem triagem/)).not.toBeInTheDocument();
  });

  test("inconclusivo aparece separado do falso positivo", async () => {
    // Somá-los inflaria a métrica com um problema de outra causa e outra ação: "não dá
    // para ver nada" é ângulo e luz (R-3), não limiar.
    montaRede(metrica([cameraMetrica({ falso_positivo: 5, inconclusivo: 7 })]));
    abre();

    const falsos = await screen.findByText("alertas falsos");
    const semVer = screen.getByText("sem dar para ver");

    expect(within(falsos.parentElement as HTMLElement).getByText("5")).toBeInTheDocument();
    expect(within(semVer.parentElement as HTMLElement).getByText("7")).toBeInTheDocument();
  });

  test("o resumo conta quantas câmeras estão fora do requisito", async () => {
    montaRede(
      metrica([
        cameraMetrica({ camera_id: "cam3", acima_do_limite: true }),
        cameraMetrica({ camera_id: "cam1", acima_do_limite: true }),
        cameraMetrica({ camera_id: "cam2", acima_do_limite: false }),
      ]),
    );
    abre();

    expect(
      await screen.findByText("2 câmeras passaram do limite em algum dia."),
    ).toBeInTheDocument();
  });

  test("período sem alerta nenhum diz isso, em vez de ficar vazio", async () => {
    // A tela do dia bom. Um vazio se confunde com falha de carregamento, e o gerente
    // fecha o app achando que quebrou.
    montaRede(metrica([], 30));
    abre();

    expect(
      await screen.findByText(/Nenhum alerta de regra nos últimos 30 dias/),
    ).toBeInTheDocument();
  });

  test("trocar o período recarrega com o número de dias pedido", async () => {
    montaRede(metrica());
    const usuario = userEvent.setup();
    abre();

    await screen.findByText("cam3");
    await usuario.click(screen.getByRole("button", { name: "30 dias" }));

    expect(chamadas[0]).toContain("dias=7");
    expect(chamadas.at(-1)).toContain("dias=30");
  });

  test("a ordem da API é preservada na tela", async () => {
    // A API já ordena da pior para a melhor, e a tela não pode reordenar: duas fontes de
    // ordenação divergiriam, e o topo da lista deixaria de ser a resposta.
    montaRede(
      metrica([
        cameraMetrica({ camera_id: "pior" }),
        cameraMetrica({ camera_id: "meio" }),
        cameraMetrica({ camera_id: "melhor" }),
      ]),
    );
    abre();

    await screen.findByText("pior");
    const nomes = screen.getAllByText(/^(pior|meio|melhor)$/).map((e) => e.textContent);
    expect(nomes).toEqual(["pior", "meio", "melhor"]);
  });
});
