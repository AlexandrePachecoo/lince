import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, test, vi } from "vitest";
import { App } from "../src/App.js";
import { lojasAdministradas } from "../src/auth/papeis.js";
import { guardaSessao } from "../src/auth/sessao.js";
import { Box, VARIAVEL_DO_BOX } from "../src/telas/Box.js";
import { fila, sessao, tokenDeInstalacao } from "./fixturas.js";

// A tela que gera o token de instalação do box (§5.1). O que ela precisa acertar é o
// que acontece com um segredo que aparece uma vez: sair inteiro para o `.env` do box, e
// não ficar em lugar nenhum do celular depois.

type Chamada = { url: string; metodo: string; autorizacao: string | undefined };

function respondeJson(corpo: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => corpo } as Response;
}

function montaRede(resposta: (chamada: Chamada) => Response) {
  const chamadas: Chamada[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const chamada = {
        url,
        metodo: init?.method ?? "GET",
        autorizacao: (init?.headers as Record<string, string> | undefined)?.authorization,
      };
      chamadas.push(chamada);
      return resposta(chamada);
    }),
  );
  return chamadas;
}

function abre(lojas: string[] = ["loja-centro"]) {
  return render(
    <MemoryRouter>
      <Box token="sessao-de-teste" lojas={lojas} />
    </MemoryRouter>,
  );
}

function usuarioCom(papel: "admin" | "gerente") {
  const base = sessao();
  return sessao({
    usuario: { ...base.usuario, lojas: [{ store_id: "loja-centro", papel }] },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  sessionStorage.clear();
});

describe("a tela de instalação do box", () => {
  test("gerar mostra a linha do .env inteira, com a variável que o box lê", async () => {
    // O instalador cola o que a tela mostra. Com o token solto, o nome da variável seria
    // digitado à mão — e um nome errado não dá erro: dá um box que sobe sem credencial.
    const gerado = tokenDeInstalacao();
    const chamadas = montaRede(() => respondeJson(gerado, 201));
    const usuario = userEvent.setup();
    abre();

    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));

    expect(await screen.findByTestId("linha-do-env")).toHaveTextContent(
      `LINCE_BOOTSTRAP_TOKEN=${gerado.bootstrap_token}`,
    );
    expect(VARIAVEL_DO_BOX).toBe("LINCE_BOOTSTRAP_TOKEN");
    expect(chamadas).toEqual([
      {
        url: "/v1/lojas/loja-centro/bootstrap-token",
        metodo: "POST",
        autorizacao: "Bearer sessao-de-teste",
      },
    ]);
  });

  test("copiar põe na área de transferência a mesma linha que está na tela", async () => {
    const gerado = tokenDeInstalacao();
    montaRede(() => respondeJson(gerado, 201));
    const usuario = userEvent.setup();
    abre();

    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));
    await usuario.click(await screen.findByRole("button", { name: "Copiar" }));

    expect(await navigator.clipboard.readText()).toBe(
      `LINCE_BOOTSTRAP_TOKEN=${gerado.bootstrap_token}`,
    );
    expect(screen.getByRole("button", { name: "Copiado" })).toBeInTheDocument();
  });

  test("cópia recusada pelo navegador é dita, e não parece ter dado certo", async () => {
    // `navigator.clipboard` falha fora de origem segura e quando o navegador nega. Em
    // silêncio, o instalador colaria no `.env` o que já estava na área de transferência.
    montaRede(() => respondeJson(tokenDeInstalacao(), 201));
    const usuario = userEvent.setup();
    abre();
    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));
    await screen.findByTestId("linha-do-env");
    vi.spyOn(navigator.clipboard, "writeText").mockRejectedValue(new Error("negado"));

    await usuario.click(screen.getByRole("button", { name: "Copiar" }));

    expect(await screen.findByText(/não deixou copiar/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copiado" })).not.toBeInTheDocument();
  });

  test("o token não é guardado em lugar nenhum do navegador", async () => {
    // Ele vira a credencial de um box. O celular do turno passa de mão em mão, e o que
    // fica em `localStorage` fica para o próximo que abrir o app.
    const gerado = tokenDeInstalacao();
    montaRede(() => respondeJson(gerado, 201));
    const usuario = userEvent.setup();
    abre();

    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));
    await screen.findByTestId("linha-do-env");

    const guardado = JSON.stringify({ ...localStorage }) + JSON.stringify({ ...sessionStorage });
    expect(guardado).not.toContain(gerado.bootstrap_token);
  });

  test("a tela avisa que o token aparece uma vez e que gerar outro cancela este", async () => {
    // Sem o aviso, quem perdeu o token gera outro com um box já no meio da instalação
    // com o primeiro — e o registro dele falha sem que ninguém ligue as duas coisas.
    montaRede(() => respondeJson(tokenDeInstalacao(), 201));
    const usuario = userEvent.setup();
    abre();

    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));

    expect(await screen.findByText(/Aparece só agora/)).toBeInTheDocument();
    expect(screen.getByText(/cancela este/)).toBeInTheDocument();
  });

  test("gerar de novo tira o token anterior da tela antes de o novo chegar", async () => {
    // A API vence o anterior no mesmo pedido. Deixá-lo visível enquanto o novo não
    // chega — ou se o pedido falhar — é oferecer para cópia um token que já morreu.
    let pedidos = 0;
    montaRede(() => {
      pedidos += 1;
      return pedidos === 1
        ? respondeJson(tokenDeInstalacao(), 201)
        : respondeJson({ erro: "erro interno" }, 500);
    });
    const usuario = userEvent.setup();
    abre();
    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));
    await screen.findByTestId("linha-do-env");

    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("erro interno");
    expect(screen.queryByTestId("linha-do-env")).not.toBeInTheDocument();
  });

  test("a recusa da API aparece com a mensagem dela", async () => {
    // A lista de lojas vem da sessão guardada, e o vínculo pode ter mudado desde o
    // login. Quem autoriza é a API; a tela precisa dizer o que ela disse.
    montaRede(() => respondeJson({ erro: "sem permissão para gerar token" }, 403));
    const usuario = userEvent.setup();
    abre();

    await usuario.click(screen.getByRole("button", { name: "Gerar token" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("sem permissão para gerar token");
  });

  test("quem não é admin de loja nenhuma lê o porquê, e não uma tela vazia", () => {
    montaRede(() => respondeJson({}, 500));
    abre([]);

    expect(screen.getByText(/Só o administrador da loja/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Gerar token" })).not.toBeInTheDocument();
  });
});

describe("quem vê o atalho da instalação", () => {
  test("lojasAdministradas devolve só as lojas em que o papel é admin", () => {
    const base = sessao().usuario;
    const usuario = {
      ...base,
      lojas: [
        { store_id: "loja-a", papel: "admin" as const },
        { store_id: "loja-b", papel: "gerente" as const },
        { store_id: "loja-c", papel: "admin" as const },
      ],
    };

    expect(lojasAdministradas(usuario)).toEqual(["loja-a", "loja-c"]);
  });

  function abreApp(papel: "admin" | "gerente", rota: string) {
    guardaSessao(usuarioCom(papel));
    montaRede(() => respondeJson(fila([], null)));
    return render(
      <MemoryRouter initialEntries={[rota]}>
        <App />
      </MemoryRouter>,
    );
  }

  test("o admin tem o atalho no cabeçalho da fila e chega à tela por ele", async () => {
    abreApp("admin", "/fila");
    const usuario = userEvent.setup();

    await usuario.click(await screen.findByRole("link", { name: "Box" }));

    expect(await screen.findByRole("heading", { name: "Instalar box" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Gerar token" })).toBeInTheDocument();
  });

  test("o gerente não vê o atalho, e o endereço direto não oferece o botão", async () => {
    // Um botão que só devolve 403 ocupa, no cabeçalho do celular, o lugar de um que serve.
    abreApp("gerente", "/fila");
    await screen.findByRole("heading", { name: "Fila de triagem" });
    expect(screen.queryByRole("link", { name: "Box" })).not.toBeInTheDocument();
  });

  test("gerente que abre /box direto lê que precisa do admin", async () => {
    abreApp("gerente", "/box");

    expect(await screen.findByText(/Só o administrador da loja/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Gerar token" })).not.toBeInTheDocument();
  });
});
