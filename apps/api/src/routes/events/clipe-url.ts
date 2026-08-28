import type { FastifyInstance } from "fastify";
import { autenticaUsuario, exigePapelNaLoja } from "../../auth/usuario-token.js";
import { validaRespostaClipeUrl } from "../../schemas/dashboard.js";
import { ClipeNaoDisponivel, EventoDesconhecido } from "./errors.js";

// GET /v1/events/{event_id}/clip-url (§4.4, NFR-7): a URL assinada com que o dashboard
// lê o clipe direto do bucket.
//
// O vídeo não passa pela API nem na leitura -- é a mesma decisão do upload, e é o que
// mantém o control plane sem I/O pesado e o egress previsível. A consequência é que
// depois que a URL sai daqui o download acontece fora do alcance da nuvem: não há como
// registrar "fulano baixou". Por isso a auditoria (§6, R-8) grava a **emissão**, que é
// o único instante em que a API sabe quem pediu, e por isso a validade é curta.

export default async function eventsClipeUrlRoutes(app: FastifyInstance) {
  app.get("/v1/events/:eventId/clip-url", async (request, reply) => {
    const usuario = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const { eventId } = request.params as { eventId: string };

    const evento = await app.prisma.evento.findFirst({
      where: { eventId, tenantId: usuario.tenantId },
      select: { eventId: true, lojaId: true, clipeEstado: true, clipeObjectKey: true },
    });
    if (!evento) {
      throw new EventoDesconhecido();
    }
    exigePapelNaLoja(usuario, evento.lojaId);

    // Os dois desfechos que não são "tem vídeo" respondem 409 com mensagens diferentes,
    // porque o dashboard faz coisas diferentes com eles: em `pendente` mostra
    // "processando" e volta a perguntar; em `indisponivel` libera a decisão sem vídeo,
    // que é o que impede o gerente de esperar para sempre por um upload que não vem.
    if (evento.clipeEstado === "pendente") {
      throw new ClipeNaoDisponivel("clipe ainda está subindo da loja");
    }
    if (evento.clipeEstado === "indisponivel") {
      throw new ClipeNaoDisponivel("não haverá vídeo para este evento");
    }
    if (evento.clipeObjectKey === null) {
      // Estado impossível: `disponivel` só é gravado pelo PATCH junto com a chave. Se
      // acontecer é bug interno, não erro do cliente -- 500 e log, nunca um 200 com URL
      // nula que o dashboard trataria como "clipe não chegou".
      request.log.error(
        { eventId: evento.eventId },
        "evento com clipe disponível e sem chave de objeto",
      );
      return reply.code(500).send();
    }

    // A auditoria é gravada ANTES de assinar. Nesta ordem, uma falha ao assinar deixa
    // uma linha de auditoria a mais (registro de um acesso que não chegou a acontecer);
    // na ordem inversa, uma falha ao gravar deixaria uma URL emitida sem registro. Errar
    // para o lado de auditar demais é o único lado aceitável (R-8).
    await app.prisma.auditoria.create({
      data: {
        tenantId: usuario.tenantId,
        acao: "clipe_url_emitida",
        usuarioId: usuario.usuarioId,
        usuarioEmail: usuario.email,
        eventId: evento.eventId,
        objectKey: evento.clipeObjectKey,
        // Melhor esforço: atrás de proxy é o que o cabeçalho disser. Serve para
        // investigar um acesso, nunca para autorizar um.
        ip: request.ip,
        userAgent: request.headers["user-agent"] ?? null,
      },
    });

    const url = await app.armazenamento.urlDeLeitura(evento.clipeObjectKey, app.leituraExpiraEmS);

    const documento = {
      schema_version: 1,
      event_id: evento.eventId,
      url,
      expires_in_s: app.leituraExpiraEmS,
      object_key: evento.clipeObjectKey,
    };

    const validacaoResposta = validaRespostaClipeUrl(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, eventId: evento.eventId },
        "resposta de GET /v1/events/{event_id}/clip-url não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(200).send(documento);
  });
}
