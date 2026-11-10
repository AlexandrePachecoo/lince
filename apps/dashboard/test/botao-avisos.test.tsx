import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { AmbienteNavegador } from "../src/push/avisos.js";
import { BotaoAvisos } from "../src/telas/BotaoAvisos.js";
import { CHAVE_VAPID, inscricaoPush } from "./fixturas.js";

// O botão no cabeçalho da fila. O que ele precisa acertar é dizer a verdade: "Avisos
// ligados" quando o aparelho está inscrito, e, quando não dá para ligar, por quê — o
// iPhone fora da tela de início é o caso em que o gerente desiste achando que o app não
// tem aviso.

function ambiente(opcoes: { ios?: boolean; suportado?: boolean } = {}): AmbienteNavegador {
  let inscricao: {
    endpoint: string;
    toJSON(): unknown;
    unsubscribe(): Promise<boolean>;
    options: object;
  } | null = null;
  let permissao: NotificationPermission = "default";
  const pushManager = {
    getSubscription: async () => inscricao,
    subscribe: async () => {
      inscricao = {
        endpoint: inscricaoPush().endpoint,
        options: { applicationServerKey: null },
        toJSON: () => inscricaoPush(),
        unsubscribe: async () => {
          inscricao = null;
          return true;
        },
      };
      return inscricao;
    },
  };
  const registro = { pushManager };
  return {
    serviceWorker: {
      getRegistration: async () => registro,
      register: async () => registro,
      ready: Promise.resolve(registro),
    } as unknown as ServiceWorkerContainer,
    pushSuportado: opcoes.suportado ?? true,
    permissao: () => permissao,
    pedePermissao: async () => {
      permissao = "granted";
      return permissao;
    },
    iosForaDaTelaInicial: opcoes.ios ?? false,
  };
}

function fingeApi(statusDaChave = 200) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      url === "/v1/push/chave"
        ? ({
            ok: statusDaChave < 400,
            status: statusDaChave,
            json: async () => ({ schema_version: 1, chave_publica: CHAVE_VAPID }),
          } as Response)
        : ({ ok: true, status: 204, json: async () => undefined } as Response),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("o botão de avisos", () => {
  test("um toque liga, e o botão passa a dizer que está ligado", async () => {
    fingeApi();
    render(<BotaoAvisos token="tok" ambiente={ambiente()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Ativar avisos" }));

    const ligado = await screen.findByRole("button", { name: "Avisos ligados" });
    expect(ligado).toHaveAttribute("aria-pressed", "true");
  });

  test("ligado, um toque desliga", async () => {
    fingeApi();
    render(<BotaoAvisos token="tok" ambiente={ambiente()} />);
    await userEvent.click(await screen.findByRole("button", { name: "Ativar avisos" }));

    await userEvent.click(await screen.findByRole("button", { name: "Avisos ligados" }));

    expect(await screen.findByRole("button", { name: "Ativar avisos" })).toBeInTheDocument();
  });

  test("no iPhone fora da tela de início, o toque explica o passo que falta", async () => {
    fingeApi();
    render(<BotaoAvisos token="tok" ambiente={ambiente({ ios: true })} />);

    await userEvent.click(await screen.findByRole("button", { name: "Ativar avisos" }));

    expect(await screen.findByText(/Adicionar à Tela de Início/)).toBeInTheDocument();
  });

  test("servidor sem notificação: diz que está indisponível em vez de fingir que ligou", async () => {
    fingeApi(503);
    render(<BotaoAvisos token="tok" ambiente={ambiente()} />);

    await userEvent.click(await screen.findByRole("button", { name: "Ativar avisos" }));

    expect(await screen.findByText("Avisos indisponíveis")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Avisos ligados" })).not.toBeInTheDocument();
  });

  test("navegador sem Web Push: o botão nem aparece", async () => {
    fingeApi();
    const { container } = render(
      <BotaoAvisos token="tok" ambiente={ambiente({ suportado: false })} />,
    );
    // Sem relógio: só deixa a verificação do estado (assíncrona) terminar.
    await act(async () => {});
    expect(container).toBeEmptyDOMElement();
  });
});
