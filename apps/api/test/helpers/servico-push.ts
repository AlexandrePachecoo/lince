import { createECDH, randomBytes } from "node:crypto";
import { readFileSync } from "node:fs";
import type { IncomingHttpHeaders, Server } from "node:http";
import { Agent, createServer } from "node:https";
import type { AddressInfo } from "node:net";
import webpush from "web-push";
import type { ConfigPush } from "../../src/notificacao/config.js";

// Um serviço de push de verdade no sentido que importa: um servidor HTTP que recebe o
// POST que o web-push monta, com cabeçalhos VAPID, cifragem aes128gcm e TTL. O que ele
// não faz é entregar a um celular. Nada é mockado do lado da API: o mesmo
// sendNotification de produção, apontado para cá (CLAUDE.md, "nada de mock onde cabe o
// real").
//
// Fala HTTPS, porque o web-push só fala HTTPS — um servidor HTTP aqui faria todo envio
// falhar no handshake. O certificado em tls/ é autoassinado para 127.0.0.1, vale 100 anos
// e não serve para mais nada: o cliente confia nele por um `Agent` com `ca` explícito,
// sem desligar verificação nenhuma.

const TLS = new URL("./tls/", import.meta.url);
const CERTIFICADO = readFileSync(new URL("servico-push.crt", TLS));
const CHAVE = readFileSync(new URL("servico-push.key", TLS));

export interface PushRecebido {
  caminho: string;
  cabecalhos: IncomingHttpHeaders;
  bytes: number;
}

export class ServicoPushFalso {
  readonly recebidos: PushRecebido[] = [];
  /** Status a devolver por caminho. Sem entrada, 201 — o que FCM e Mozilla respondem. */
  readonly respostas = new Map<string, number>();

  private constructor(
    private readonly servidor: Server,
    readonly origem: string,
  ) {}

  static async sobe(): Promise<ServicoPushFalso> {
    const recebe = { instancia: undefined as ServicoPushFalso | undefined };
    const servidor = createServer({ key: CHAVE, cert: CERTIFICADO }, (req, res) => {
      const partes: Buffer[] = [];
      req.on("data", (parte: Buffer) => partes.push(parte));
      req.on("end", () => {
        const caminho = req.url ?? "/";
        const instancia = recebe.instancia;
        instancia?.recebidos.push({
          caminho,
          cabecalhos: req.headers,
          bytes: Buffer.concat(partes).length,
        });
        res.statusCode = instancia?.respostas.get(caminho) ?? 201;
        res.end();
      });
    });
    await new Promise<void>((resolve) => servidor.listen(0, "127.0.0.1", resolve));
    const { port } = servidor.address() as AddressInfo;
    recebe.instancia = new ServicoPushFalso(servidor, `https://127.0.0.1:${port}`);
    return recebe.instancia;
  }

  /**
   * Uma inscrição como `PushSubscription.toJSON()` devolve: chave P-256 de verdade e
   * segredo de 16 bytes. Com chaves inventadas a cifragem do web-push recusaria antes
   * de qualquer requisição, e o teste mediria a validação de chave, não o envio.
   */
  inscricao(nome: string) {
    const ecdh = createECDH("prime256v1");
    ecdh.generateKeys();
    return {
      endpoint: `${this.origem}/push/${nome}`,
      keys: {
        p256dh: ecdh.getPublicKey().toString("base64url"),
        auth: randomBytes(16).toString("base64url"),
      },
    };
  }

  recebidosEm(nome: string): PushRecebido[] {
    return this.recebidos.filter((r) => r.caminho === `/push/${nome}`);
  }

  limpa(): void {
    this.recebidos.length = 0;
    this.respostas.clear();
  }

  async fecha(): Promise<void> {
    await new Promise<void>((resolve) => this.servidor.close(() => resolve()));
  }
}

export function configPushTeste(origem: string, parcial: Partial<ConfigPush> = {}): ConfigPush {
  const chaves = webpush.generateVAPIDKeys();
  return {
    vapid: {
      publica: chaves.publicKey,
      privada: chaves.privateKey,
      assunto: "mailto:teste@lince.local",
    },
    origensPermitidas: [origem],
    janelaAgrupamentoS: 120,
    tetoDiarioPorLoja: 20,
    atrasoMaximoS: 1800,
    ttlS: 600,
    agente: new Agent({ ca: CERTIFICADO }),
    ...parcial,
  };
}
