import type { FastifyInstance } from "fastify";
import { RequisicaoInvalida } from "../../auth/errors.js";
import { autenticaUsuario } from "../../auth/usuario-token.js";
import { endpointPermitido } from "../../notificacao/config.js";
import {
  validaRequisicaoPushInscricao,
  validaRequisicaoPushRemocao,
} from "../../schemas/dashboard.js";
import { EndpointNaoPermitido, PushDesligado } from "./errors.js";

// POST e DELETE /v1/push/inscricoes: o aparelho que recebe alerta (§4.5).
//
// A inscrição é do **usuário**, não da loja. Que lojas ela cobre é decidido no envio, a
// cada evento, pelos vínculos de agora: tirar um gerente de uma loja corta o alerta no
// evento seguinte, sem ninguém lembrar de apagar inscrição.

interface CorpoInscricao {
  inscricao: { endpoint: string; keys: { p256dh: string; auth: string } };
}

export default async function pushInscricoesRoutes(app: FastifyInstance) {
  app.post("/v1/push/inscricoes", async (request, reply) => {
    const usuario = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    if (!app.configPush) {
      throw new PushDesligado();
    }
    const validacao = validaRequisicaoPushInscricao(request.body);
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com push-inscricao.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const { endpoint, keys } = (request.body as CorpoInscricao).inscricao;

    // A API vai fazer um POST para este endereço a cada alerta. Sem a lista, um usuário
    // logado apontaria a API para qualquer lugar — inclusive para dentro da rede dela.
    if (!endpointPermitido(endpoint, app.configPush.origensPermitidas)) {
      throw new EndpointNaoPermitido();
    }

    // Upsert pelo endpoint, que é único no sistema: o mesmo navegador entrando com outro
    // usuário passa a ser do novo. O celular de turno não pode seguir recebendo os
    // alertas de quem saiu dele — e as chaves mudam se o navegador reinscrever.
    await app.prisma.inscricaoPush.upsert({
      where: { endpoint },
      create: {
        tenantId: usuario.tenantId,
        usuarioId: usuario.usuarioId,
        endpoint,
        p256dh: keys.p256dh,
        auth: keys.auth,
      },
      update: {
        tenantId: usuario.tenantId,
        usuarioId: usuario.usuarioId,
        p256dh: keys.p256dh,
        auth: keys.auth,
      },
    });
    return reply.code(204).send();
  });

  app.delete("/v1/push/inscricoes", async (request, reply) => {
    const usuario = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const validacao = validaRequisicaoPushRemocao(request.body);
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com push-remocao.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const { endpoint } = request.body as { endpoint: string };

    // Só a do próprio usuário. 204 mesmo quando não havia nada: sair da sessão num
    // aparelho que nunca se inscreveu não é erro, e responder diferente diria a quem
    // pergunta se aquele endpoint existe para outra pessoa.
    await app.prisma.inscricaoPush.deleteMany({
      where: { endpoint, usuarioId: usuario.usuarioId },
    });
    return reply.code(204).send();
  });
}
