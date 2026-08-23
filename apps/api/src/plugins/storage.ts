import type { FastifyInstance } from "fastify";
import fp from "fastify-plugin";
import { ArmazenamentoClipes, type ConfigArmazenamento } from "../storage/clip-storage.js";

declare module "fastify" {
  interface FastifyInstance {
    armazenamento: ArmazenamentoClipes;
    /** Validade das URLs de upload emitidas no POST /v1/events, em segundos. Fica
     * decorada no app, e não lida do ambiente dentro da rota, para a rota não ter
     * como divergir do que a assinatura carrega -- é o mesmo número nos dois lugares
     * ou o agente calcula o vencimento errado. */
    uploadExpiraEmS: number;
  }
}

export interface StoragePluginOptions {
  armazenamento: ConfigArmazenamento;
  uploadExpiraEmS: number;
}

export default fp<StoragePluginOptions>(
  async (app: FastifyInstance, opts: StoragePluginOptions) => {
    app.decorate("armazenamento", new ArmazenamentoClipes(opts.armazenamento));
    app.decorate("uploadExpiraEmS", opts.uploadExpiraEmS);
  },
);
