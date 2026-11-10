import { afterEach, describe, expect, test, vi } from "vitest";
import {
  type AmbienteNavegador,
  ativaAvisos,
  chaveParaBytes,
  desativaAvisos,
  estadoDosAvisos,
  sincronizaAvisos,
} from "../src/push/avisos.js";
import { CHAVE_VAPID, inscricaoPush } from "./fixturas.js";

// Ligar o aviso no celular, do lado do navegador. jsdom não tem service worker nem
// PushManager; o ambiente entra por parâmetro e aqui é uma imitação mínima deles. O que
// é real é o resto: a chamada à API, a ordem dos passos e o que acontece quando um deles
// falha.

class InscricaoFalsa {
  desinscrita = false;
  constructor(
    private readonly gerente: PushManagerFalso,
    readonly options: { applicationServerKey: ArrayBuffer },
    readonly endpoint = inscricaoPush().endpoint,
  ) {}
  toJSON() {
    return inscricaoPush(this.endpoint);
  }
  async unsubscribe() {
    this.desinscrita = true;
    this.gerente.atual = null;
    return true;
  }
}

class PushManagerFalso {
  atual: InscricaoFalsa | null = null;
  pedidos: PushSubscriptionOptionsInit[] = [];
  async getSubscription() {
    return this.atual;
  }
  async subscribe(opcoes: PushSubscriptionOptionsInit) {
    this.pedidos.push(opcoes);
    const chave = opcoes.applicationServerKey as Uint8Array<ArrayBuffer>;
    this.atual = new InscricaoFalsa(this, { applicationServerKey: chave.buffer });
    return this.atual;
  }
}

interface OpcoesAmbiente {
  permissao?: NotificationPermission;
  resposta?: NotificationPermission;
  ios?: boolean;
  suportado?: boolean;
  inscrito?: boolean;
}

function ambiente(opcoes: OpcoesAmbiente = {}) {
  const pushManager = new PushManagerFalso();
  const registro = { pushManager };
  const registrados: string[] = [];
  let permissao: NotificationPermission = opcoes.permissao ?? "default";
  if (opcoes.inscrito) {
    pushManager.atual = new InscricaoFalsa(pushManager, {
      applicationServerKey: chaveParaBytes(CHAVE_VAPID).buffer,
    });
  }
  const amb: AmbienteNavegador = {
    serviceWorker: {
      getRegistration: async () =>
        registrados.length > 0 || opcoes.inscrito ? registro : undefined,
      register: async (url: string) => {
        registrados.push(url);
        return registro;
      },
      ready: Promise.resolve(registro),
    } as unknown as ServiceWorkerContainer,
    pushSuportado: opcoes.suportado ?? true,
    permissao: () => permissao,
    pedePermissao: async () => {
      permissao = opcoes.resposta ?? "granted";
      return permissao;
    },
    iosForaDaTelaInicial: opcoes.ios ?? false,
  };
  return { amb, pushManager, registrados };
}

interface Pedido {
  metodo: string;
  url: string;
  corpo: unknown;
}

function fingeApi(chave: { status: number; corpo?: unknown } = { status: 200 }) {
  const pedidos: Pedido[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit = {}) => {
      const metodo = init.method ?? "GET";
      pedidos.push({
        metodo,
        url,
        corpo: init.body === undefined ? undefined : JSON.parse(String(init.body)),
      });
      if (url === "/v1/push/chave") {
        return {
          ok: chave.status < 400,
          status: chave.status,
          json: async () => chave.corpo ?? { schema_version: 1, chave_publica: CHAVE_VAPID },
        } as Response;
      }
      return { ok: true, status: 204, json: async () => undefined } as Response;
    }),
  );
  return pedidos;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ligar e desligar os avisos", () => {
  test("a chave VAPID volta aos 65 bytes do ponto P-256", () => {
    const bytes = chaveParaBytes(CHAVE_VAPID);
    expect(bytes).toHaveLength(65);
    expect(bytes[0]).toBe(4);
  });

  test("o estado diz o que oferecer: iPhone fora da tela de início, bloqueado, ligado", async () => {
    expect(await estadoDosAvisos(ambiente({ ios: true }).amb)).toBe("so_na_tela_inicial");
    expect(await estadoDosAvisos(ambiente({ suportado: false }).amb)).toBe("sem_suporte");
    expect(await estadoDosAvisos(ambiente({ permissao: "denied" }).amb)).toBe("negado");
    expect(await estadoDosAvisos(ambiente().amb)).toBe("inativo");
    expect(await estadoDosAvisos(ambiente({ permissao: "granted" }).amb)).toBe("inativo");
    expect(await estadoDosAvisos(ambiente({ permissao: "granted", inscrito: true }).amb)).toBe(
      "ativo",
    );
  });

  test("ativar: permissão, service worker na raiz, inscrição com a chave do servidor, API", async () => {
    const pedidos = fingeApi();
    const { amb, pushManager, registrados } = ambiente();

    expect(await ativaAvisos("tok", amb)).toBe("ativo");

    // Na raiz: o escopo do worker é o diretório dele, e só na raiz ele cobre o app todo.
    expect(registrados).toEqual(["/sw.js"]);
    const [pedido] = pushManager.pedidos;
    // `userVisibleOnly` é exigência do Chrome, e é também a promessa ao gerente: todo
    // aviso aparece. Push silencioso não existe neste produto.
    expect(pedido?.userVisibleOnly).toBe(true);
    expect(Array.from(pedido?.applicationServerKey as Uint8Array)).toEqual(
      Array.from(chaveParaBytes(CHAVE_VAPID)),
    );
    expect(pedidos.map((p) => `${p.metodo} ${p.url}`)).toEqual([
      "GET /v1/push/chave",
      "POST /v1/push/inscricoes",
    ]);
    expect(pedidos[1]?.corpo).toEqual({ schema_version: 1, inscricao: inscricaoPush() });
  });

  test("fechar o pedido de permissão não é bloquear, e não toca na API", async () => {
    const pedidos = fingeApi();

    expect(await ativaAvisos("tok", ambiente({ resposta: "default" }).amb)).toBe("inativo");
    expect(await ativaAvisos("tok", ambiente({ resposta: "denied" }).amb)).toBe("negado");
    expect(pedidos).toEqual([]);
  });

  test("servidor sem chaves VAPID: indisponível, e o aparelho não se inscreve à toa", async () => {
    fingeApi({ status: 503, corpo: { erro: "desligada" } });
    const { amb, pushManager } = ambiente();

    expect(await ativaAvisos("tok", amb)).toBe("indisponivel");
    expect(pushManager.pedidos).toEqual([]);
  });

  test("inscrição com a chave de antes de uma rotação é desfeita e refeita", async () => {
    // O navegador recusa reinscrever com outra chave, e a inscrição antiga nunca mais
    // recebe nada: sem isto, o gerente "liga" o aviso e continua sem aviso.
    fingeApi();
    const { amb, pushManager } = ambiente({ permissao: "granted" });
    const antiga = new InscricaoFalsa(pushManager, { applicationServerKey: new ArrayBuffer(65) });
    pushManager.atual = antiga;

    expect(await ativaAvisos("tok", amb)).toBe("ativo");
    expect(antiga.desinscrita).toBe(true);
    expect(pushManager.pedidos).toHaveLength(1);
  });

  test("ao abrir, o aparelho já inscrito é regravado para quem entrou agora", async () => {
    // O celular do turno: sem isto, os avisos seguiriam para quem já saiu dele.
    const pedidos = fingeApi();

    await sincronizaAvisos("tok", ambiente({ permissao: "granted", inscrito: true }).amb);
    await sincronizaAvisos("tok", ambiente({ permissao: "granted" }).amb);

    expect(pedidos.map((p) => `${p.metodo} ${p.url}`)).toEqual(["POST /v1/push/inscricoes"]);
  });

  test("desligar tira da API e do navegador; o navegador desinscreve mesmo sem API", async () => {
    const pedidos = fingeApi();
    const ligado = ambiente({ permissao: "granted", inscrito: true });
    const inscricao = ligado.pushManager.atual;

    await desativaAvisos("tok", ligado.amb);

    expect(pedidos.map((p) => `${p.metodo} ${p.url}`)).toEqual(["DELETE /v1/push/inscricoes"]);
    expect(pedidos[0]?.corpo).toEqual({ schema_version: 1, endpoint: inscricaoPush().endpoint });
    expect(inscricao?.desinscrita).toBe(true);

    // Sessão vencida: a API recusa, e o aparelho sai da lista do mesmo jeito. O próximo
    // envio para ele volta 410 e a API apaga a linha sozinha.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: false, status: 401, json: async () => ({}) }) as Response),
    );
    const vencido = ambiente({ permissao: "granted", inscrito: true });
    const daSessaoVencida = vencido.pushManager.atual;
    await expect(desativaAvisos("tok", vencido.amb)).rejects.toThrow();
    expect(daSessaoVencida?.desinscrita).toBe(true);
  });
});
