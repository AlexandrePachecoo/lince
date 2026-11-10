import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { URL as URLDoNode } from "node:url";
import { runInNewContext } from "node:vm";
import { describe, expect, test } from "vitest";

// O service worker de verdade (public/sw.js), carregado num contexto isolado com um
// `self` de mentira. jsdom não tem service worker, e o arquivo não passa pelo bundle —
// então é aqui que ele é exercitado, evento por evento.
//
// O que importa nele: todo push vira uma notificação visível (sem isso o navegador
// mostra a dele, genérica, ou revoga a inscrição), e o toque nunca sai do app.

// Pelo diretório do pacote, e não por `import.meta.url`: sob jsdom o `URL` global é o
// dele, e o `readFileSync` não o aceita.
const FONTE = readFileSync(resolve(process.cwd(), "public/sw.js"), "utf-8");

interface Janela {
  url: string;
  focada: boolean;
  navegouPara: string | null;
  focus(): Promise<void>;
  navigate?(url: string): Promise<Janela>;
}

function janela(url: string, comNavigate = true): Janela {
  const j: Janela = {
    url,
    focada: false,
    navegouPara: null,
    async focus() {
      j.focada = true;
    },
  };
  if (comNavigate) {
    j.navigate = async (destino: string) => {
      j.navegouPara = destino;
      return j;
    };
  }
  return j;
}

function carregaWorker(janelas: Janela[] = []) {
  const ouvintes = new Map<string, (evento: unknown) => void>();
  const notificacoes: Array<{ titulo: string; opcoes: Record<string, unknown> }> = [];
  const abertas: string[] = [];
  const self = {
    location: { origin: "https://lince.exemplo" },
    addEventListener: (tipo: string, ouvinte: (evento: unknown) => void) => {
      ouvintes.set(tipo, ouvinte);
    },
    skipWaiting: () => undefined,
    registration: {
      showNotification: async (titulo: string, opcoes: Record<string, unknown>) => {
        notificacoes.push({ titulo, opcoes });
      },
    },
    clients: {
      claim: async () => undefined,
      matchAll: async () => janelas,
      openWindow: async (url: string) => {
        abertas.push(url);
        return null;
      },
    },
  };
  runInNewContext(FONTE, { self, URL: URLDoNode });

  async function dispara(tipo: string, evento: Record<string, unknown>) {
    const pendentes: Promise<unknown>[] = [];
    const ouvinte = ouvintes.get(tipo);
    if (!ouvinte) {
      throw new Error(`o worker não ouve ${tipo}`);
    }
    ouvinte({ ...evento, waitUntil: (p: Promise<unknown>) => pendentes.push(p) });
    await Promise.all(pendentes);
  }

  return {
    notificacoes,
    abertas,
    push: (dados: unknown) =>
      dispara("push", {
        data:
          dados === null
            ? null
            : {
                json: () => (typeof dados === "string" ? JSON.parse(dados) : dados),
              },
      }),
    toque: (url: unknown) =>
      dispara("notificationclick", {
        notification: { data: { url }, close: () => undefined },
      }),
  };
}

const ALERTA = {
  schema_version: 1,
  tipo: "alerta",
  event_id: "0b6f1a52-1d0e-4c9a-9a0e-6f1f2c3d4e5f",
  titulo: "Possível furto — cam3",
  corpo: "gesto de esconder · Mercado Centro · 11:07",
  url: "/eventos/0b6f1a52-1d0e-4c9a-9a0e-6f1f2c3d4e5f",
  tag: "lince-loja-1-cam3",
};

describe("o service worker", () => {
  test("o push vira notificação com o texto da API, agrupada por câmera e tocando", async () => {
    const worker = carregaWorker();
    await worker.push(ALERTA);

    expect(worker.notificacoes).toHaveLength(1);
    const [n] = worker.notificacoes;
    expect(n?.titulo).toBe("Possível furto — cam3");
    expect(n?.opcoes.body).toBe(ALERTA.corpo);
    expect(n?.opcoes.tag).toBe(ALERTA.tag);
    // Sem `renotify`, o segundo furto da mesma câmera substitui o primeiro em silêncio.
    expect(n?.opcoes.renotify).toBe(true);
    expect(n?.opcoes.data).toEqual({ url: ALERTA.url });
  });

  test("push ilegível ainda mostra notificação, apontando para a fila", async () => {
    // Push sem notificação visível faz o Chrome mostrar a dele, genérica — e, repetido,
    // revogar a inscrição. Um conteúdo quebrado não pode custar o canal inteiro.
    const worker = carregaWorker();
    await worker.push(null);
    await worker.push("{ não é json");

    expect(worker.notificacoes).toHaveLength(2);
    for (const n of worker.notificacoes) {
      expect(n.titulo).toBe("lince: novo alerta");
      expect(n.opcoes.data).toEqual({ url: "/fila" });
    }
  });

  test("URL fora do app no conteúdo vira a fila", async () => {
    const worker = carregaWorker();
    await worker.push({ ...ALERTA, url: "https://golpe.exemplo/eventos/x" });
    await worker.push({ ...ALERTA, url: "//golpe.exemplo" });

    for (const n of worker.notificacoes) {
      expect(n.opcoes.data).toEqual({ url: "/fila" });
    }
  });

  test("o toque leva a janela do app que já está aberta para o evento", async () => {
    // Abrir uma segunda janela deixaria o gerente com duas filas, uma delas velha.
    const aberta = janela("https://lince.exemplo/fila");
    const worker = carregaWorker([janela("https://outro.site/"), aberta]);

    await worker.toque(ALERTA.url);

    expect(aberta.focada).toBe(true);
    expect(aberta.navegouPara).toBe(ALERTA.url);
    expect(worker.abertas).toEqual([]);
  });

  test("sem janela do app aberta, o toque abre uma no evento", async () => {
    const worker = carregaWorker([janela("https://outro.site/")]);

    await worker.toque(ALERTA.url);

    expect(worker.abertas).toEqual([ALERTA.url]);
  });

  test("o toque nunca abre endereço de fora, nem que o dado da notificação diga", async () => {
    const worker = carregaWorker();

    await worker.toque("https://golpe.exemplo/");

    expect(worker.abertas).toEqual(["/fila"]);
  });
});
