import type { FastifyInstance } from "fastify";
import { autenticaAgente } from "../../auth/agent-token.js";
import { EscopoDoAgente, RequisicaoInvalida } from "../../auth/errors.js";
import { chaveDoClipe } from "../../storage/object-key.js";
import { validaRequisicaoEvento, validaRespostaEvento } from "./schemas.js";

// POST /v1/events (§5.2). Recebe o evento que a borda decidiu emitir e devolve a URL
// pré-assinada para o clipe subir direto no R2 -- o vídeo não passa por aqui (§4.4).
//
// Idempotente por event_id, e a idempotência não é enfeite: é o que sustenta a
// política de reenvio do §5.4. O agente reposta o mesmo evento quando não viu a
// resposta anterior E quando quer uma URL de upload nova, porque a que ele tinha
// venceu. As duas situações chegam aqui iguais e precisam sair iguais.

interface BlocoClipe {
  status: "ok" | "clip_failed";
  error?: string | null;
  [campo: string]: unknown;
}

interface CorpoEvento {
  schema_version: 1;
  event_id: string;
  tenant_id: string;
  store_id: string;
  camera_id: string;
  occurred_at: string;
  reported_at: string;
  source: "rule" | "manual";
  rule?: { id: string; version: number } | null;
  versions: Record<string, unknown>;
  clip: BlocoClipe;
}

export default async function eventsCreateRoutes(app: FastifyInstance) {
  app.post("/v1/events", async (request, reply) => {
    const agente = await autenticaAgente(app.prisma, request.headers.authorization);

    const validacao = validaRequisicaoEvento(request.body);
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com event.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const evento = request.body as CorpoEvento;

    // O agente manda a chave de idempotência nos dois lugares (outbox/http.py): no
    // corpo e no cabeçalho, para um proxy poder deduplicar sem abrir o payload.
    // Divergirem significa proxy remontando requisição ou cliente com bug -- aceitar
    // escolheria em silêncio qual das duas é a verdade, e a escolha errada grava um
    // evento com o id de outro.
    const chaveIdempotencia = request.headers["idempotency-key"];
    if (typeof chaveIdempotencia === "string" && chaveIdempotencia !== evento.event_id) {
      throw new RequisicaoInvalida("Idempotency-Key não bate com o event_id do corpo");
    }

    if (evento.tenant_id !== agente.tenantId || evento.store_id !== agente.lojaId) {
      throw new EscopoDoAgente();
    }

    const existente = await app.prisma.evento.findUnique({
      where: { eventId: evento.event_id },
    });
    // UUID colidindo entre tenants não acontece na prática; a checagem existe porque o
    // custo dela é uma comparação e o custo de estar errado é um agente sobrescrevendo
    // evento de outra loja (NFR-6).
    if (existente && existente.tenantId !== agente.tenantId) {
      throw new EscopoDoAgente();
    }

    const corteFalhou = evento.clip.status === "clip_failed";
    const chave = chaveDoClipe(evento.tenant_id, evento.event_id, new Date(evento.occurred_at));

    const registro = await app.prisma.evento.upsert({
      where: { eventId: evento.event_id },
      create: {
        eventId: evento.event_id,
        tenantId: evento.tenant_id,
        lojaId: evento.store_id,
        cameraId: evento.camera_id,
        agenteId: agente.agenteId,
        ocorridoEm: new Date(evento.occurred_at),
        reportadoEm: new Date(evento.reported_at),
        source: evento.source,
        regraId: evento.rule?.id ?? null,
        regraVersao: evento.rule?.version ?? null,
        versions: evento.versions as object,
        clip: evento.clip as object,
        // `clip.status` do POST é o desfecho do CORTE, não do upload. `clip_failed`
        // aqui é a borda avisando que não há arquivo nenhum -- o evento é válido e
        // triável, só sem vídeo, e não faz sentido emitir URL para ele.
        clipeEstado: corteFalhou ? "indisponivel" : "pendente",
        clipeObjectKey: corteFalhou ? null : chave,
        clipeErro: corteFalhou ? (evento.clip.error ?? null) : null,
        clipeResolvidoEm: corteFalhou ? new Date() : null,
      },
      // Reenvio não altera nada do evento, de propósito. O payload é congelado na
      // borda no momento do corte e nunca muda (outbox/event.py), então não há o que
      // atualizar -- e o que PARECE atualizável é justamente o que não pode ser: o
      // bloco `clip` do payload ainda diz "cortado com sucesso" muito depois de o
      // PATCH ter registrado o upload. Reescrevê-lo rebaixaria um clipe já disponível
      // para pendente, e a triagem passaria a esperar para sempre um upload que já
      // aconteceu. Quem manda no desfecho do clipe é o PATCH, sozinho.
      // Grava a chave primária sobre ela mesma: nada do evento muda, e é justamente esse
      // o ponto. Um `update: {}` seria a forma óbvia de dizer "não reescreva nada", mas o
      // Prisma só compila o upsert para o `INSERT ... ON CONFLICT DO UPDATE` nativo do
      // Postgres quando há o que atualizar -- com update vazio ele cai num caminho de
      // consultar-e-então-inserir, que não é atômico. Dois POST simultâneos do mesmo
      // evento (a borda reenviando sob link ruim, §5.4) faziam um dos dois estourar P2002
      // e virar 500. Não nascia evento duplicado, mas a idempotência que o §5.4 promete
      // deixava de valer no único caso em que ela importa.
      update: { eventId: evento.event_id },
    });

    // URL nova a cada POST, inclusive no reenvio: é assim que o agente renova uma URL
    // vencida (event-accepted.v1.json). Só faz sentido enquanto o clipe é esperado --
    // com os bytes já no R2 ou com a borda tendo desistido, não há upload a oferecer,
    // e o agente nessa situação nem olha o campo (outbox/sender.py, _encaminha_clipe).
    const emiteUrl = registro.clipeEstado === "pendente" && registro.clipeObjectKey !== null;
    const urlDeUpload = emiteUrl
      ? await app.armazenamento.urlDeUpload(registro.clipeObjectKey as string, app.uploadExpiraEmS)
      : null;

    const documento = {
      schema_version: 1,
      event_id: registro.eventId,
      clip_upload_url: urlDeUpload,
      clip_object_key: registro.clipeObjectKey,
      clip_upload_expires_in_s: urlDeUpload === null ? null : app.uploadExpiraEmS,
    };

    const validacaoResposta = validaRespostaEvento(documento);
    if (!validacaoResposta.valido) {
      // Defesa em profundidade, mesmo padrão de config.ts e register.ts: bug interno
      // vira 500, nunca um 2xx malformado. Aqui isso importa mais que nas outras
      // rotas -- um 200 sem clip_upload_url deixa o clipe preso na fila da loja.
      request.log.error(
        { erros: validacaoResposta.erros, eventId: registro.eventId },
        "resposta de POST /v1/events não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(existente ? 200 : 201).send(documento);
  });
}
