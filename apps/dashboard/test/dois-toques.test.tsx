import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { App } from "../src/App.js";
import { evento, fila, sessao } from "./fixturas.js";

// A NFR-9 como teste: **um** toque abre o evento com o vídeo rodando, o segundo é a
// decisão. É a régua do produto inteiro -- a triagem acontece em pé, num corredor, com o
// celular numa mão, e um toque a mais por evento é o que faz a fila não ser triada.
//
// A rede é fingida aqui (não há API de pé numa suíte de front), mas as respostas são as
// fixturas validadas contra packages/shared em contrato.test.ts -- não payloads inventados
// para o teste passar.

interface Chamada {
  url: string;
  metodo: string;
  corpo: unknown;
}

let chamadas: Chamada[];

function respondeJson(corpo: unknown, status = 200): Response {
  return {
    ok: status < 400,
    status,
    json: async () => corpo,
  } as Response;
}

function montaRede(opcoes: { clipeEstado?: "disponivel" | "pendente" | "indisponivel" } = {}) {
  const eventoDaVez = evento({
    clip_state: opcoes.clipeEstado ?? "disponivel",
    ...(opcoes.clipeEstado === "indisponivel"
      ? { clip_error: "buffer despejado pelo teto de disco" }
      : {}),
  });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const metodo = init?.method ?? "GET";
      chamadas.push({
        url,
        metodo,
        corpo: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
      });

      if (url.startsWith("/v1/auth/login")) {
        return respondeJson(sessao());
      }
      if (url.startsWith("/v1/events?")) {
        return respondeJson(fila([eventoDaVez], null));
      }
      if (url.endsWith("/clip-url")) {
        return eventoDaVez.clip_state === "disponivel"
          ? respondeJson({
              schema_version: 1,
              event_id: eventoDaVez.event_id,
              url: "https://bucket.exemplo/clipe.mp4?assinatura",
              expires_in_s: 300,
              object_key: "tenant-dev/2026-08-29/evento.mp4",
            })
          : respondeJson({ erro: "clipe ainda está subindo da loja" }, 409);
      }
      if (url.endsWith("/triagem")) {
        return respondeJson({ schema_version: 1, evento: eventoDaVez }, 201);
      }
      if (url.startsWith("/v1/events/")) {
        return respondeJson({ schema_version: 1, evento: eventoDaVez });
      }
      throw new Error(`rota não fingida no teste: ${metodo} ${url}`);
    }),
  );

  return eventoDaVez;
}

function abre(rota = "/fila") {
  return render(
    <MemoryRouter initialEntries={[rota]}>
      <App />
    </MemoryRouter>,
  );
}

async function entra() {
  const usuario = userEvent.setup();
  abre("/login");
  await usuario.type(screen.getByLabelText("E-mail"), "gerente@loja-dev.local");
  await usuario.type(screen.getByLabelText("Senha"), "senha-de-desenvolvimento");
  await usuario.click(screen.getByRole("button", { name: "Entrar" }));
  return usuario;
}

beforeEach(() => {
  chamadas = [];
});
afterEach(() => {
  vi.unstubAllGlobals();
});

describe("os dois toques da NFR-9", () => {
  test("um toque abre o evento com o vídeo já rodando", async () => {
    // `autoplay` sem `muted` é recusado por todo navegador de celular: o vídeo fica
    // parado, o gerente toca no play, e o primeiro toque virou dois. É a NFR-9 inteira
    // perdida num atributo.
    montaRede();
    const usuario = await entra();

    const linha = await screen.findByRole("link", { name: /cam3/ });
    await usuario.click(linha); // toque 1

    const video = await screen.findByTestId("clipe");
    expect(video).toHaveAttribute("src", "https://bucket.exemplo/clipe.mp4?assinatura");
    expect(video).toHaveAttribute("autoplay");
    expect(video).toHaveAttribute("playsinline");
    // `muted` o React define como **propriedade**, e o HTML não a reflete como atributo.
    // É a propriedade que a política de autoplay do navegador consulta, então é ela que
    // este teste precisa ver -- procurar o atributo aqui testaria o React, e falharia.
    expect((video as HTMLVideoElement).muted).toBe(true);
  });

  test("o segundo toque grava a decisão e volta para a fila", async () => {
    montaRede();
    const usuario = await entra();

    await usuario.click(await screen.findByRole("link", { name: /cam3/ })); // toque 1
    await screen.findByTestId("clipe");
    await usuario.click(screen.getByRole("button", { name: "Falso positivo" })); // toque 2

    await waitFor(() => {
      const triagem = chamadas.find((c) => c.url.endsWith("/triagem"));
      expect(triagem?.metodo).toBe("POST");
      expect(triagem?.corpo).toEqual({ decisao: "falso_positivo" });
    });

    // E de volta à fila, que é onde o próximo evento está: quem tria trinta alertas não
    // pode ter que voltar à mão depois de cada um.
    expect(await screen.findByRole("heading", { name: "Fila de triagem" })).toBeInTheDocument();
  });

  test("os três desfechos têm o mesmo peso na tela", async () => {
    // `inconclusivo` escondido num menu faz o triador chutar `falso_positivo` no clipe em
    // que não dá para ver nada -- e envenena exatamente a métrica que o R-1 manda vigiar.
    montaRede();
    const usuario = await entra();

    await usuario.click(await screen.findByRole("link", { name: /cam3/ }));
    await screen.findByTestId("clipe");

    for (const rotulo of ["Confirmar", "Falso positivo", "Não dá para ver"]) {
      expect(screen.getByRole("button", { name: rotulo })).toBeEnabled();
    }
  });

  test("sem vídeo, a decisão continua a um toque de distância", async () => {
    // O ponto do §4.5: vídeo ajuda a decidir, não é pré-requisito. Travar a decisão aqui
    // deixaria o evento sem triagem para sempre quando o upload não vem.
    montaRede({ clipeEstado: "indisponivel" });
    const usuario = await entra();

    await usuario.click(await screen.findByRole("link", { name: /cam3/ }));

    expect(await screen.findByText(/buffer despejado pelo teto de disco/)).toBeInTheDocument();
    expect(screen.queryByTestId("clipe")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmar" })).toBeEnabled();
  });

  test("clipe subindo diz que dá para decidir sem ele", async () => {
    // `pendente` e `indisponivel` pedem coisas diferentes do gerente: num vale esperar,
    // no outro não. Sem a distinção, a tela ou faz esperar sempre, ou nunca.
    montaRede({ clipeEstado: "pendente" });
    const usuario = await entra();

    await usuario.click(await screen.findByRole("link", { name: /cam3/ }));

    expect(await screen.findByText(/ainda está subindo/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmar" })).toBeEnabled();
  });
});

describe("o endereço do evento", () => {
  test("abrir /eventos/:id direto funciona sem passar pela fila", async () => {
    // É o que faz um F5 no meio da triagem voltar ao mesmo evento, e o que dá endereço à
    // notificação quando ela existir. Sem isso, recarregar no corredor devolve o gerente
    // ao topo da lista -- e ele perde onde estava.
    const eventoDaVez = montaRede();
    localStorage.setItem(
      "lince.sessao",
      JSON.stringify({
        token: "token-de-teste-opaco",
        expiraEm: Date.now() + 3_600_000,
        usuario: sessao().usuario,
      }),
    );

    abre(`/eventos/${eventoDaVez.event_id}`);

    expect(await screen.findByTestId("clipe")).toBeInTheDocument();
    expect(chamadas.some((c) => c.url === `/v1/events/${eventoDaVez.event_id}`)).toBe(true);
  });
});
