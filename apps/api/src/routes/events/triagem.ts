import type { DecisaoTriagem } from "@prisma/client";
import type { FastifyInstance } from "fastify";
import { RequisicaoInvalida } from "../../auth/errors.js";
import { autenticaUsuario, exigePapelNaLoja } from "../../auth/usuario-token.js";
import { validaRequisicaoTriagem, validaRespostaTriagem } from "../../schemas/dashboard.js";
import { documentoDoEvento, inclusaoDaFila } from "./apresentacao.js";
import { EventoDesconhecido } from "./errors.js";

// POST /v1/events/{event_id}/triagem (§4.5): a decisão humana.
//
// É o fecho do princípio do §1 -- o sistema não decide nada sozinho. Tudo o que a borda
// fez até aqui produziu um alerta; consequência nenhuma sai deste sistema sem uma linha
// desta tabela.
//
// **Append-only, e re-triagem é permitida.** Decidir de novo grava outra linha e a
// vigente passa a ser a última. O motivo é o dedo errado: NFR-9 pede dois toques, com o
// celular numa mão, num corredor de mercado. Uma decisão imutável transformaria um toque
// errado em falso positivo permanente na estatística da câmera -- exatamente o número
// que o R-1 manda vigiar. Guardar o histórico deixa corrigir sem apagar que se corrigiu.

interface CorpoTriagem {
  decisao: DecisaoTriagem;
  observacao?: string | null;
}

export default async function eventsTriagemRoutes(app: FastifyInstance) {
  app.post("/v1/events/:eventId/triagem", async (request, reply) => {
    const usuario = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const { eventId } = request.params as { eventId: string };

    const validacao = validaRequisicaoTriagem(request.body);
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com triagem.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const corpo = request.body as CorpoTriagem;

    // Tenant no WHERE (NFR-6): evento de outro tenant não chega a ser lido, e responde o
    // mesmo 404 de evento inexistente -- não vaza que a linha existe.
    const evento = await app.prisma.evento.findFirst({
      where: { eventId, tenantId: usuario.tenantId },
      select: { eventId: true, lojaId: true },
    });
    if (!evento) {
      throw new EventoDesconhecido();
    }

    // Dentro do tenant a recusa é 403, e não 404: quem está autenticado já sabe que as
    // lojas da rede existem, e "você não alcança esta loja" é acionável para quem pede
    // acesso ao admin. Qualquer papel tria -- admin, gerente e operador. Triagem é o
    // trabalho do produto, não um privilégio; restringi-la a um papel deixaria evento
    // parado justamente nos horários em que só há operador na loja.
    exigePapelNaLoja(usuario, evento.lojaId);

    await app.prisma.triagem.create({
      data: {
        eventId: evento.eventId,
        tenantId: usuario.tenantId,
        usuarioId: usuario.usuarioId,
        decisao: corpo.decisao,
        observacao: corpo.observacao ?? null,
      },
    });

    // Relê com a inclusão da fila para devolver o evento como ele passa a aparecer lá --
    // inclusive `revisada`, que depende da contagem de decisões e portanto não dá para
    // deduzir do que acabou de ser escrito.
    const atualizado = await app.prisma.evento.findUniqueOrThrow({
      where: { eventId: evento.eventId },
      include: inclusaoDaFila,
    });

    const documento = { schema_version: 1, evento: documentoDoEvento(atualizado) };

    const validacaoResposta = validaRespostaTriagem(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, eventId: evento.eventId },
        "resposta de POST /v1/events/{event_id}/triagem não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(201).send(documento);
  });
}
