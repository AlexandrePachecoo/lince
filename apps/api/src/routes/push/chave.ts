import type { FastifyInstance } from "fastify";
import { autenticaUsuario } from "../../auth/usuario-token.js";
import { validaRespostaPushChave } from "../../schemas/dashboard.js";
import { PushDesligado } from "./errors.js";

// GET /v1/push/chave: a chave pública VAPID para o navegador assinar a inscrição.
//
// Vem da API, e não embutida no build do dashboard: a chave é do servidor que envia. Um
// PWA guardado no cache do celular com a chave de antes de uma rotação inscreveria o
// aparelho num par que o servidor já não tem — a inscrição seria aceita e nada chegaria,
// sem erro em lugar nenhum.
//
// Exige sessão mesmo sendo pública por natureza: quem não tem sessão não tem o que
// inscrever, e uma rota aberta a mais é uma rota a mais para alguém mapear.

export default async function pushChaveRoutes(app: FastifyInstance) {
  app.get("/v1/push/chave", async (request, reply) => {
    await autenticaUsuario(app.prisma, request.headers.authorization, app.sessaoSegredo);
    if (!app.configPush) {
      throw new PushDesligado();
    }

    const documento = { schema_version: 1, chave_publica: app.configPush.vapid.publica };
    const validacao = validaRespostaPushChave(documento);
    if (!validacao.valido) {
      request.log.error(
        { erros: validacao.erros },
        "VAPID_PUBLIC_KEY não bate com push-chave.v1.json",
      );
      return reply.code(500).send();
    }
    return reply.code(200).send(documento);
  });
}
