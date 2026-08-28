import type { FastifyInstance } from "fastify";
import fp from "fastify-plugin";

declare module "fastify" {
  interface FastifyInstance {
    /** Segredo HS256 das sessões de usuário (§4.5). Decorado no app, e não lido do
     * ambiente dentro da rota, pelo mesmo motivo de uploadExpiraEmS: quem assina e
     * quem verifica precisam ser o mesmo valor, e a única forma de garantir isso é
     * não haver dois lugares que leem process.env. */
    sessaoSegredo: string;
    /** Validade do token emitido no login, em segundos. Vai também no corpo da
     * resposta, para o PWA saber quando marcar a sessão como vencida sem inspecionar
     * o token — o cliente não deve precisar entender o formato da credencial. */
    sessaoExpiraEmS: number;
  }
}

export interface AuthPluginOptions {
  sessaoSegredo: string;
  sessaoExpiraEmS: number;
}

export default fp<AuthPluginOptions>(async (app: FastifyInstance, opts: AuthPluginOptions) => {
  app.decorate("sessaoSegredo", opts.sessaoSegredo);
  app.decorate("sessaoExpiraEmS", opts.sessaoExpiraEmS);
});
