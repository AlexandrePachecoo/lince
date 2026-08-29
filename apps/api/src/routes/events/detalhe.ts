import type { FastifyInstance } from "fastify";
import { autenticaUsuario, exigePapelNaLoja } from "../../auth/usuario-token.js";
import { validaRespostaEventoDetalhe } from "../../schemas/dashboard.js";
import { documentoDoEvento, inclusaoDaFila } from "./apresentacao.js";
import { EventoDesconhecido } from "./errors.js";

// GET /v1/events/{event_id} (§4.5): um evento só, para quem chegou nele sem passar pela
// fila.
//
// A fila já devolve o evento inteiro, então esta rota não serve à navegação normal --
// serve a quem tem o id e não tem a lista: recarregar a tela do evento no meio da
// triagem, abrir o link mandado ao técnico e, quando a notificação existir, tocar nela.
// Sem isso, um F5 no corredor volta o gerente para o topo da fila.
//
// A URL do clipe **não** vem junto, e a economia de um round-trip não paga o que
// custaria: a auditoria do R-8 grava a emissão da URL, porque depois que ela sai o
// download acontece fora do alcance da nuvem (§4.4). Emitir a cada abertura encheria a
// tabela de acessos que ninguém chegou a assistir, e uma auditoria assim não responde
// mais a pergunta para a qual foi feita.

export default async function eventsDetalheRoutes(app: FastifyInstance) {
  app.get("/v1/events/:eventId", async (request, reply) => {
    const usuario = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const { eventId } = request.params as { eventId: string };

    // Tenant no WHERE (NFR-6), como em clipe-url.ts e triagem.ts: evento de outro tenant
    // não chega a ser lido, e responde o mesmo 404 de evento inexistente. `eventId` não é
    // conferido contra formato nenhum de propósito -- um id malformado simplesmente não
    // casa linha alguma e cai neste mesmo 404, que é a resposta certa de qualquer forma.
    const evento = await app.prisma.evento.findFirst({
      where: { eventId, tenantId: usuario.tenantId },
      // A mesma inclusão da fila, e não uma consulta enxuta: é ela que carrega a triagem
      // vigente e a contagem de que `revisada` depende. Pedir menos aqui quebraria em
      // documentoDoEvento, no TypeScript, e não em produção com um campo faltando.
      include: inclusaoDaFila,
    });
    if (!evento) {
      throw new EventoDesconhecido();
    }

    // Dentro do tenant a recusa é 403: quem está autenticado já sabe que as lojas da rede
    // existem, e "você não alcança esta loja" é acionável para quem pede acesso ao admin.
    // Fora do tenant a resposta já foi 404 acima -- confirmar que o event_id existe em
    // outra rede seria vazamento.
    exigePapelNaLoja(usuario, evento.lojaId);

    const documento = { schema_version: 1, evento: documentoDoEvento(evento) };

    const validacaoResposta = validaRespostaEventoDetalhe(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, eventId: evento.eventId },
        "resposta de GET /v1/events/{event_id} não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(200).send(documento);
  });
}
