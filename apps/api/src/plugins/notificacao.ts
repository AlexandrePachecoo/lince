import type { FastifyInstance } from "fastify";
import fp from "fastify-plugin";
import type { ConfigPush } from "../notificacao/config.js";
import { Notificador } from "../notificacao/notificador.js";

declare module "fastify" {
  interface FastifyInstance {
    /** `null` é a notificação desligada: sem chaves VAPID a API sobe e só não avisa.
     * O evento continua chegando à fila — a notificação é o aviso, não o evento. */
    notificador: Notificador | null;
    /** A mesma configuração que o notificador usa, para as rotas de inscrição lerem a
     * chave pública e a lista de origens do mesmo lugar que o envio. */
    configPush: ConfigPush | null;
  }
}

export interface NotificacaoPluginOptions {
  push: ConfigPush | null;
  /** Relógio da decisão de alerta. Injetável para o teste do teto diário atravessar a
   * meia-noite da loja sem esperar por ela. */
  relogio?: (() => Date) | undefined;
}

export default fp<NotificacaoPluginOptions>(
  async (app: FastifyInstance, opts: NotificacaoPluginOptions) => {
    const notificador = opts.push
      ? new Notificador(app.prisma, opts.push, app.log, opts.relogio)
      : null;
    if (!notificador) {
      app.log.warn("VAPID_* não definidas: notificação por Web Push desligada");
    }
    app.decorate("notificador", notificador);
    app.decorate("configPush", opts.push);
    // Antes de o Prisma desconectar: um envio cortado no meio grava a decisão sem o
    // registro do envio, e "o gerente diz que não recebeu" fica sem resposta.
    app.addHook("preClose", async () => {
      await notificador?.ocioso();
    });
  },
);
