import type { Agent } from "node:https";

// O que a notificação por Web Push precisa saber (§4.5, R-12). Fica num tipo só, e não
// espalhado em decorações do app, porque quem decide (decide.ts) e quem envia
// (notificador.ts) leem os mesmos números: um teto diário lido de dois lugares é um teto
// que um dia diverge.

export interface ChavesVapid {
  /** Ponto P-256 em base64url. Vai para o navegador no `GET /v1/push/chave`. */
  publica: string;
  privada: string;
  /** `mailto:` ou URL `https:`. É quem o serviço de push procura quando a API abusa. */
  assunto: string;
}

export interface ConfigPush {
  vapid: ChavesVapid;
  /**
   * De onde um endpoint de inscrição pode ser. A API faz um POST para o endpoint que o
   * navegador mandou, e sem esta lista qualquer usuário logado faria a API bater em
   * qualquer endereço — inclusive os da rede interna dela. Entrada com `*.` casa o
   * sufixo do host (o Windows espalha os endpoints em `wns2-*.notify.windows.com`).
   */
  origensPermitidas: readonly string[];
  /**
   * Janela de agrupamento da §4.6, em segundos: dentro dela, um segundo evento da mesma
   * câmera e do mesmo gatilho vai para a fila sem notificar. Valor inicial a validar; é
   * o que impede uma câmera mal calibrada de mandar trinta notificações seguidas — e o
   * gerente que recebe trinta desliga a notificação (R-1).
   */
  janelaAgrupamentoS: number;
  /** Teto de notificações por loja e dia **da loja** (R-12). Passou dele, só fila. */
  tetoDiarioPorLoja: number;
  /**
   * Evento que chega mais velho que isto não notifica: é a fila da borda drenando depois
   * de o link voltar. Avisar às 15h de um furto das 11h manda o gerente correr atrás de
   * ninguém — o evento está na fila, que é onde coisa velha é triada.
   */
  atrasoMaximoS: number;
  /**
   * Quanto o serviço de push guarda a mensagem para um celular desligado. Curto pelo
   * mesmo motivo do atraso máximo: o celular que volta à rede uma hora depois não deve
   * tocar por um furto que já acabou.
   */
  ttlS: number;
  /**
   * Agente HTTPS do envio. Só o teste passa: o serviço de push falso fala TLS com um
   * certificado próprio, e o web-push fala só HTTPS. Em produção fica de fora e vale a
   * cadeia de certificados do sistema.
   */
  agente?: Agent | undefined;
}

/** Os serviços de push dos navegadores que o gerente usa no celular e no computador. */
export const ORIGENS_PUSH_PADRAO: readonly string[] = [
  "https://fcm.googleapis.com", // Chrome, Edge e Samsung Internet no Android
  "https://updates.push.services.mozilla.com", // Firefox
  "https://web.push.apple.com", // Safari e PWA no iPhone (iOS 16.4+)
  "https://*.notify.windows.com", // Edge no Windows
];

export const JANELA_AGRUPAMENTO_PADRAO_S = 120;
export const TETO_DIARIO_PADRAO = 20;
export const ATRASO_MAXIMO_PADRAO_S = 1800;
export const TTL_PADRAO_S = 600;

/**
 * Se o endpoint é de um serviço de push conhecido. Compara protocolo, host e porta da
 * URL já interpretada — nunca prefixo de texto, que `https://fcm.googleapis.com.evil`
 * passaria.
 */
export function endpointPermitido(endpoint: string, origens: readonly string[]): boolean {
  let url: URL;
  try {
    url = new URL(endpoint);
  } catch {
    return false;
  }
  return origens.some((origem) => {
    const curinga = origem.match(/^(https?):\/\/\*\.(.+)$/);
    if (curinga) {
      const [, protocolo, sufixo] = curinga;
      return url.protocol === `${protocolo}:` && url.hostname.endsWith(`.${sufixo}`);
    }
    try {
      return new URL(origem).origin === url.origin;
    } catch {
      return false;
    }
  });
}
