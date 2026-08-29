import { afterEach, describe, expect, test, vi } from "vitest";
import { ErroDeRede, ErroHttp, buscaFila, decide, login } from "../src/api/cliente.js";
import { fila, sessao } from "./fixturas.js";

// O que o cliente faz com a resposta, que é onde mora a diferença entre uma tela útil e
// um "algo deu errado". O status precisa chegar inteiro a quem chama: 401 derruba a
// sessão, 403 e 404 são telas diferentes, e 409 no clipe não é falha nenhuma.

function respondeJson(corpo: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => corpo } as Response;
}

type Chamada = [url: string, init?: RequestInit];

// O espião declara os parâmetros que o `fetch` recebe, e não `() => resposta`: sem isso o
// TypeScript infere `calls: []` e as asserções sobre a URL e o cabeçalho não compilam.
function fingeFetch(resposta: Response | (() => Promise<never>)) {
  const espia = vi.fn(async (..._chamada: Chamada) =>
    typeof resposta === "function" ? await resposta() : resposta,
  );
  vi.stubGlobal("fetch", espia);
  return espia;
}

// `noUncheckedIndexedAccess` (tsconfig) faz `calls[0]` ser possivelmente indefinido, e com
// razão: um teste que afirma sobre a primeira chamada quando nenhuma aconteceu passaria em
// silêncio se o índice fosse tratado como certo. Aqui a ausência vira falha explícita.
function primeiraChamada(espia: { mock: { calls: Chamada[] } }): Chamada {
  const chamada = espia.mock.calls[0];
  if (chamada === undefined) {
    throw new Error("o cliente não chegou a chamar o fetch");
  }
  return chamada;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("o acesso à API", () => {
  test("o token vai no cabeçalho, nunca na URL", () => {
    // URL entra em log de proxy, em histórico do navegador e em captura de tela. O token
    // é credencial de sessão: sai daqui só no cabeçalho.
    const espia = fingeFetch(respondeJson(fila([])));

    void buscaFila("token-secreto");

    const [url, init] = primeiraChamada(espia);
    expect(init).toBeDefined();
    expect(url).not.toContain("token-secreto");
    expect((init?.headers as Record<string, string>).authorization).toBe("Bearer token-secreto");
  });

  test("a fila pede explicitamente o que falta decidir", () => {
    // `triagem=pendentes` já é o padrão da API. Explicitá-lo aqui faz de uma mudança
    // daquele padrão uma mudança visível, e não a descoberta de que a tela passou a
    // mostrar histórico.
    const espia = fingeFetch(respondeJson(fila([])));

    void buscaFila("t", { limite: 25, cursor: "cursor-opaco" });

    const [url] = primeiraChamada(espia);
    expect(url).toContain("triagem=pendentes");
    expect(url).toContain("limite=25");
    expect(url).toContain("cursor=cursor-opaco");
  });

  test("o status sobrevive ao erro, e a mensagem da API também", async () => {
    // Sem o status, quem chama não distingue "sua sessão venceu" de "você não acessa esta
    // loja" -- e as duas telas pedem coisas diferentes do gerente.
    fingeFetch(respondeJson({ erro: "você não tem acesso a esta loja" }, 403));

    await expect(buscaFila("t")).rejects.toMatchObject({
      status: 403,
      message: "você não tem acesso a esta loja",
    });
    await expect(buscaFila("t")).rejects.toBeInstanceOf(ErroHttp);
  });

  test("erro sem corpo JSON não vira exceção de parsing", async () => {
    // Um HTML de gateway no lugar do `{ erro }` da API é proxy, redirecionamento ou API
    // meio implantada. Estourar no `json()` trocaria um 502 legível por um erro de
    // sintaxe, e mandaria depurar no lugar errado.
    fingeFetch({
      ok: false,
      status: 502,
      json: async () => {
        throw new SyntaxError("não é JSON");
      },
    } as unknown as Response);

    await expect(buscaFila("t")).rejects.toMatchObject({ status: 502, message: "HTTP 502" });
  });

  test("rede caída é um erro diferente de status ruim", async () => {
    // Um tem "tente de novo" como resposta razoável; o outro não. A tela do login diz
    // "confira a internet" num caso e "e-mail ou senha incorretos" no outro.
    fingeFetch(() => Promise.reject(new TypeError("Failed to fetch")));

    await expect(login("a@b.local", "x")).rejects.toBeInstanceOf(ErroDeRede);
  });

  test("a decisão sem observação não manda o campo", async () => {
    // `observacao: undefined` viraria ausente no JSON de qualquer forma, mas mandar
    // `null` explícito é diferente de não mandar -- e o corpo é validado contra
    // triagem.v1.json do outro lado.
    const espia = fingeFetch(respondeJson({ schema_version: 1, evento: {} }, 201));

    await decide("t", "evt-1", "confirmado");

    const [, init] = primeiraChamada(espia);
    expect(JSON.parse(String(init?.body))).toEqual({ decisao: "confirmado" });
  });

  test("o login não manda cabeçalho de autorização", async () => {
    // Não há token ainda. Mandar um vazio ou um `Bearer undefined` faria a API responder
    // 401 por credencial malformada, num caminho em que credencial nenhuma é o certo.
    const espia = fingeFetch(respondeJson(sessao()));

    await login("gerente@loja-dev.local", "senha");

    const [, init] = primeiraChamada(espia);
    expect((init?.headers as Record<string, string>).authorization).toBeUndefined();
  });
});
