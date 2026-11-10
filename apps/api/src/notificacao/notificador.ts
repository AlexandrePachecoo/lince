import type { InscricaoPush, PrismaClient } from "@prisma/client";
import type { FastifyBaseLogger } from "fastify";
import webpush from "web-push";
import { validaNotificacao } from "../schemas/dashboard.js";
import type { ConfigPush } from "./config.js";
import { type ConteudoNotificacao, conteudoDoAlerta, conteudoDoTeto } from "./conteudo.js";
import { type EventoParaAlerta, decideAlerta } from "./decide.js";

// O envio do alerta para os celulares (§4.5).
//
// **Fora do caminho da resposta ao agente.** O `POST /v1/events` responde assim que o
// evento está gravado, e a notificação segue depois: um serviço de push lento não pode
// segurar a resposta, senão a borda vence o timeout, reenvia, e a fila dela cresce por
// causa de um celular. O evento já está na fila de triagem; o alerta é o aviso de que
// ele chegou, não o evento.
//
// **O custo, assumido:** sem BullMQ (R-11), uma notificação em voo quando a API cai se
// perde. A decisão já está gravada em `alerta` e o evento na fila; o que falta é o
// toque no celular. Trocar por job durável fica para quando houver um segundo canal
// (WhatsApp, §4.3) — aí o retry com backoff passa a ser do canal, não deste arquivo.

/** O mínimo que o envio precisa saber, e que um teste consegue observar. */
export interface ResultadoProcessamento {
  decisao: string;
  enviados: number;
}

export class Notificador {
  private readonly emVoo = new Set<Promise<unknown>>();

  constructor(
    private readonly prisma: PrismaClient,
    private readonly config: ConfigPush,
    private readonly log: FastifyBaseLogger,
    private readonly relogio: () => Date = () => new Date(),
  ) {}

  /** Agenda e devolve na hora. Nunca lança: falha de notificação vira log, não 500. */
  aoNovoEvento(evento: EventoParaAlerta): void {
    const tarefa = this.processa(evento).catch((erro: unknown) => {
      this.log.error({ err: erro, eventId: evento.eventId }, "notificação do evento falhou");
    });
    this.emVoo.add(tarefa);
    void tarefa.finally(() => this.emVoo.delete(tarefa));
  }

  /**
   * Resolve quando não há envio em voo. Existe para o teste esperar sem `sleep` e para o
   * encerramento da API não cortar um envio no meio.
   */
  async ocioso(): Promise<void> {
    while (this.emVoo.size > 0) {
      await Promise.allSettled([...this.emVoo]);
    }
  }

  async processa(evento: EventoParaAlerta): Promise<ResultadoProcessamento> {
    const { decisao, primeiroTetoDoDia, loja } = await decideAlerta(
      this.prisma,
      evento,
      this.relogio(),
      this.config,
    );
    if (decisao === "notificado") {
      return { decisao, enviados: await this.envia(evento, conteudoDoAlerta(evento, loja)) };
    }
    if (decisao === "teto" && primeiroTetoDoDia) {
      const aviso = conteudoDoTeto(loja, evento.lojaId, this.config.tetoDiarioPorLoja);
      return { decisao, enviados: await this.envia(evento, aviso) };
    }
    return { decisao, enviados: 0 };
  }

  private async envia(evento: EventoParaAlerta, conteudo: ConteudoNotificacao): Promise<number> {
    // Defesa em profundidade, como nas respostas das rotas: o service worker valida o
    // mínimo, e um conteúdo fora do contrato viraria uma notificação em branco na tela
    // de bloqueio — que o gerente aprende a ignorar.
    const validacao = validaNotificacao(conteudo);
    if (!validacao.valido) {
      throw new Error(
        `notificação fora de notificacao.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }

    // Quem recebe é decidido agora, e não na inscrição: o usuário desativado ou tirado da
    // loja deixa de receber no evento seguinte, sem ninguém lembrar de apagar inscrição.
    const inscricoes = await this.prisma.inscricaoPush.findMany({
      where: {
        tenantId: evento.tenantId,
        usuario: {
          ativo: true,
          tenantId: evento.tenantId,
          lojas: { some: { lojaId: evento.lojaId } },
        },
      },
    });
    const corpo = JSON.stringify(conteudo);
    const resultados = await Promise.all(
      inscricoes.map((inscricao) => this.enviaUma(evento, inscricao, corpo)),
    );
    return resultados.filter(Boolean).length;
  }

  private async enviaUma(
    evento: EventoParaAlerta,
    inscricao: InscricaoPush,
    corpo: string,
  ): Promise<boolean> {
    const registro = {
      tenantId: evento.tenantId,
      eventId: evento.eventId,
      usuarioId: inscricao.usuarioId,
      inscricaoId: inscricao.id,
    };
    try {
      const resposta = await webpush.sendNotification(
        { endpoint: inscricao.endpoint, keys: { p256dh: inscricao.p256dh, auth: inscricao.auth } },
        corpo,
        {
          vapidDetails: {
            subject: this.config.vapid.assunto,
            publicKey: this.config.vapid.publica,
            privateKey: this.config.vapid.privada,
          },
          TTL: this.config.ttlS,
          // `high` acorda o celular em economia de bateria. Um alerta de furto que chega
          // quando a tela acende por outro motivo chega tarde demais.
          urgency: "high",
          timeout: 10_000,
          ...(this.config.agente ? { agent: this.config.agente } : {}),
        },
      );
      await this.prisma.envioAlerta.create({
        data: { ...registro, status: "enviado", httpStatus: resposta.statusCode },
      });
      return true;
    } catch (erro) {
      const status = erro instanceof webpush.WebPushError ? erro.statusCode : null;
      // 404 e 410 são o serviço de push dizendo que o aparelho não existe mais para ele:
      // desinstalou, limpou os dados do site ou a inscrição venceu. Mandar de novo é
      // inútil para sempre; apagar agora poupa uma requisição perdida a cada alerta.
      const expirou = status === 404 || status === 410;
      if (expirou) {
        await this.prisma.inscricaoPush.deleteMany({ where: { id: inscricao.id } });
      }
      await this.prisma.envioAlerta.create({
        data: {
          ...registro,
          status: expirou ? "expirada" : "falhou",
          httpStatus: status,
          erro: (erro instanceof Error ? erro.message : String(erro)).slice(0, 500),
        },
      });
      return false;
    }
  }
}
