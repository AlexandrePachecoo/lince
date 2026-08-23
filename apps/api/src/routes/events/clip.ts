import type { FastifyInstance } from "fastify";
import { autenticaAgente } from "../../auth/agent-token.js";
import { RequisicaoInvalida } from "../../auth/errors.js";
import { EventoDesconhecido } from "./errors.js";
import { validaRequisicaoClipe } from "./schemas.js";

// PATCH /v1/events/{event_id} (§5.2). O desfecho do clipe, depois do PUT no R2.
//
// É uma rota separada do POST porque o alerta é disparado antes de o clipe terminar
// de subir (§2.3): é isso que protege o NFR-1 quando o link da loja está lento. O
// evento já está triável quando esta chamada chega -- o que ela muda é se o triador
// vai ter vídeo ou não.
//
// Sem corpo na resposta (204), mesma leitura que o heartbeat: não há estado a devolver.

interface CorpoClipe {
  schema_version: 1;
  clip: { status: "ok" | "clip_failed"; error?: string | null };
  object_key?: string | null;
}

export default async function eventsClipRoutes(app: FastifyInstance) {
  app.patch("/v1/events/:eventId", async (request, reply) => {
    const agente = await autenticaAgente(app.prisma, request.headers.authorization);

    const validacao = validaRequisicaoClipe(request.body);
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com event-clip.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const corpo = request.body as CorpoClipe;
    const { eventId } = request.params as { eventId: string };

    // O tenant entra no WHERE, não numa comparação depois da leitura (NFR-6): assim
    // não existe caminho em que a linha de outra loja chegue a ser lida para só então
    // ser recusada. Não achar e não ser seu são a mesma resposta -- ver errors.ts.
    const evento = await app.prisma.evento.findFirst({
      where: { eventId, tenantId: agente.tenantId, lojaId: agente.lojaId },
    });
    if (!evento) {
      throw new EventoDesconhecido();
    }

    const subiu = corpo.clip.status === "ok";

    if (subiu && evento.clipeObjectKey === null) {
      // Estado incoerente: o evento subiu declarando que não havia clipe, então esta
      // API nunca emitiu URL nenhuma para ele e não há objeto no R2 para confirmar.
      // Aceitar marcaria o clipe como disponível numa chave que não existe, e o
      // sintoma apareceria lá na frente como player quebrado na triagem.
      throw new RequisicaoInvalida("evento subiu sem clipe; não há upload a confirmar");
    }

    // A chave do objeto vem do que a API emitiu, nunca do que o cliente mandou. O
    // campo existe no contrato para conciliação (event-clip.v1.json), e é isso que ele
    // é usado para fazer aqui: divergir é sinal de bug de um dos lados e vira log --
    // gravar o valor do cliente deixaria uma credencial de loja apontar o evento para
    // um objeto qualquer do bucket.
    if (corpo.object_key && corpo.object_key !== evento.clipeObjectKey) {
      request.log.warn(
        { eventId, chaveDoAgente: corpo.object_key, chaveDaApi: evento.clipeObjectKey },
        "object_key do PATCH diverge da chave emitida; mantendo a da API",
      );
    }

    await app.prisma.evento.update({
      where: { eventId: evento.eventId },
      data: {
        clipeEstado: subiu ? "disponivel" : "indisponivel",
        // O erro do PATCH é o do UPLOAD, e substitui o do corte de propósito: um
        // clipe cortado sem problema que morreu no teto de disco tem "teto de disco"
        // como explicação útil, e o campo do corte estava vazio de qualquer jeito.
        clipeErro: subiu ? null : (corpo.clip.error ?? null),
        clipeResolvidoEm: new Date(),
      },
    });

    return reply.code(204).send();
  });
}
