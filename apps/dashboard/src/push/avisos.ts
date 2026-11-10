import { ErroHttp, buscaChavePush, inscrevePush, removeInscricaoPush } from "../api/cliente.js";
import type { InscricaoPushJson } from "../api/tipos.js";

// Os avisos no celular (§4.5), do lado do navegador: pedir permissão, inscrever o aparelho
// no serviço de push e contar à API para onde mandar.
//
// O ambiente do navegador entra por parâmetro, e não lido de `window` lá dentro: jsdom não
// tem service worker nem PushManager, e sem isto a única forma de testar seria não testar.

export type EstadoAvisos =
  /** Navegador sem Web Push. O botão some: não há o que oferecer. */
  | "sem_suporte"
  /** iPhone fora da tela de início. O Safari só entrega Web Push a app instalado (iOS
   *  16.4+), e dizer isso é o que separa "não funciona" de "falta um passo". */
  | "so_na_tela_inicial"
  /** O gerente bloqueou. Só ele desfaz, nas configurações do navegador. */
  | "negado"
  | "inativo"
  | "ativo"
  /** O servidor subiu sem chaves VAPID (`503`). */
  | "indisponivel";

export interface AmbienteNavegador {
  serviceWorker: ServiceWorkerContainer | null;
  pushSuportado: boolean;
  permissao(): NotificationPermission;
  pedePermissao(): Promise<NotificationPermission>;
  iosForaDaTelaInicial: boolean;
}

export function ambienteReal(): AmbienteNavegador {
  const temNotificacao = typeof Notification !== "undefined";
  const navegador = typeof navigator !== "undefined" ? navigator : undefined;
  const ios = /iPad|iPhone|iPod/.test(navegador?.userAgent ?? "");
  const instalado =
    (navegador as (Navigator & { standalone?: boolean }) | undefined)?.standalone === true ||
    (typeof matchMedia === "function" && matchMedia("(display-mode: standalone)").matches);
  return {
    serviceWorker: navegador && "serviceWorker" in navegador ? navegador.serviceWorker : null,
    pushSuportado: typeof window !== "undefined" && "PushManager" in window && temNotificacao,
    permissao: () => (temNotificacao ? Notification.permission : "denied"),
    pedePermissao: () => Notification.requestPermission(),
    iosForaDaTelaInicial: ios && !instalado,
  };
}

/** base64url sem preenchimento → bytes, que é o que `applicationServerKey` aceita. */
export function chaveParaBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const base64 = base64url.replace(/-/g, "+").replace(/_/g, "/");
  const preenchida = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
  const binario = atob(preenchida);
  // Sobre um ArrayBuffer explícito: o `applicationServerKey` não aceita a visão genérica,
  // que em tese poderia ser de memória compartilhada.
  const bytes = new Uint8Array(new ArrayBuffer(binario.length));
  for (let i = 0; i < binario.length; i++) {
    bytes[i] = binario.charCodeAt(i);
  }
  return bytes;
}

async function inscricaoAtual(amb: AmbienteNavegador): Promise<PushSubscription | null> {
  const registro = await amb.serviceWorker?.getRegistration();
  return (await registro?.pushManager.getSubscription()) ?? null;
}

export async function estadoDosAvisos(amb: AmbienteNavegador): Promise<EstadoAvisos> {
  if (amb.iosForaDaTelaInicial) {
    return "so_na_tela_inicial";
  }
  if (!amb.pushSuportado || amb.serviceWorker === null) {
    return "sem_suporte";
  }
  const permissao = amb.permissao();
  if (permissao === "denied") {
    return "negado";
  }
  return permissao === "granted" && (await inscricaoAtual(amb)) !== null ? "ativo" : "inativo";
}

function mesmaChave(atual: ArrayBuffer | null | undefined, nova: Uint8Array): boolean {
  if (!atual) {
    return false;
  }
  const bytes = new Uint8Array(atual);
  return bytes.length === nova.length && bytes.every((b, i) => b === nova[i]);
}

/**
 * Pede permissão, inscreve e conta à API. Tem que ser chamada de dentro de um toque: o
 * navegador recusa pedido de permissão que não vem de gesto do usuário.
 */
export async function ativaAvisos(token: string, amb: AmbienteNavegador): Promise<EstadoAvisos> {
  const inicial = await estadoDosAvisos(amb);
  if (inicial === "sem_suporte" || inicial === "so_na_tela_inicial" || inicial === "negado") {
    return inicial;
  }
  const permissao = await amb.pedePermissao();
  if (permissao !== "granted") {
    // Fechar o pedido sem responder ("default") não é bloquear: o botão continua lá.
    return permissao === "denied" ? "negado" : "inativo";
  }

  let chave: Uint8Array<ArrayBuffer>;
  try {
    chave = chaveParaBytes((await buscaChavePush(token)).chave_publica);
  } catch (erro) {
    if (erro instanceof ErroHttp && erro.status === 503) {
      return "indisponivel";
    }
    throw erro;
  }

  const container = amb.serviceWorker as ServiceWorkerContainer;
  await container.register("/sw.js");
  const registro = await container.ready;
  let inscricao = await registro.pushManager.getSubscription();
  // Inscrição feita com a chave de antes de uma rotação no servidor: o navegador recusa
  // reinscrever com outra chave, e a antiga nunca mais recebe nada. Desfaz e refaz.
  if (inscricao && !mesmaChave(inscricao.options.applicationServerKey, chave)) {
    await inscricao.unsubscribe();
    inscricao = null;
  }
  inscricao ??= await registro.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: chave,
  });
  await inscrevePush(token, inscricao.toJSON() as InscricaoPushJson);
  return "ativo";
}

/**
 * Regrava a inscrição deste aparelho para o usuário da sessão. É o que move o celular do
 * turno para quem entrou agora: sem isto, os alertas seguiriam indo para quem já saiu
 * e não tirou o aparelho da lista.
 */
export async function sincronizaAvisos(token: string, amb: AmbienteNavegador): Promise<void> {
  if ((await estadoDosAvisos(amb)) !== "ativo") {
    return;
  }
  const inscricao = await inscricaoAtual(amb);
  if (inscricao) {
    await inscrevePush(token, inscricao.toJSON() as InscricaoPushJson);
  }
}

/**
 * Tira o aparelho da lista, na API **e** no navegador. O navegador desinscreve mesmo que
 * a API falhe (sessão vencida, sem rede): sem a inscrição, o próximo envio para este
 * aparelho volta 410 e a API apaga a linha sozinha.
 */
export async function desativaAvisos(token: string, amb: AmbienteNavegador): Promise<void> {
  const inscricao = amb.serviceWorker ? await inscricaoAtual(amb) : null;
  if (!inscricao) {
    return;
  }
  try {
    await removeInscricaoPush(token, inscricao.endpoint);
  } finally {
    await inscricao.unsubscribe();
  }
}
