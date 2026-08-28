import type { ClipeEstado, DecisaoTriagem, EventoOrigem, Prisma } from "@prisma/client";

// Como uma linha de `evento` vira o documento que o dashboard lê (fila-triagem.v1.json).
// Fica fora das rotas porque duas delas devolvem exatamente a mesma coisa: o GET da fila
// e o POST da triagem, que responde com o evento já atualizado para o PWA não precisar
// de uma segunda chamada. Duas montagens divergiriam no primeiro campo novo.

// O `include` que a consulta precisa fazer para esta montagem funcionar. Exportado para
// que a rota não possa pedir menos do que a apresentação lê -- se pedisse, o TypeScript
// reclamaria aqui e não em produção com um campo faltando.
export const inclusaoDaFila = {
  triagens: {
    // A vigente é a última, e "última" é por seq, não por criadoEm: duas decisões no
    // mesmo milissegundo empatariam e a vigente viraria cara ou coroa (schema.prisma).
    orderBy: { seq: "desc" },
    take: 1,
    include: { usuario: { select: { id: true, nome: true } } },
  },
  // Serve a um campo só, `triagem.revisada`: mais de uma decisão significa que alguém
  // corrigiu, e o dashboard tem que mostrar uma correção como correção.
  _count: { select: { triagens: true } },
} satisfies Prisma.EventoInclude;

export type EventoDaFila = Prisma.EventoGetPayload<{ include: typeof inclusaoDaFila }>;

interface TriagemDocumento {
  decisao: DecisaoTriagem;
  decidido_em: string;
  decidido_por: { id: string; nome: string };
  observacao: string | null;
  revisada: boolean;
}

export interface EventoDocumento {
  event_id: string;
  tenant_id: string;
  store_id: string;
  camera_id: string;
  occurred_at: string;
  reported_at: string;
  received_at: string;
  source: EventoOrigem;
  rule: { id: string; version: number } | null;
  versions: unknown;
  clip_state: ClipeEstado;
  clip_error: string | null;
  clip: unknown;
  triagem: TriagemDocumento | null;
}

export function documentoDoEvento(evento: EventoDaFila): EventoDocumento {
  const vigente = evento.triagens[0];

  return {
    event_id: evento.eventId,
    tenant_id: evento.tenantId,
    store_id: evento.lojaId,
    camera_id: evento.cameraId,
    occurred_at: evento.ocorridoEm.toISOString(),
    reported_at: evento.reportadoEm.toISOString(),
    received_at: evento.recebidoEm.toISOString(),
    source: evento.source,
    // Os dois juntos ou nenhum: `manual` não tem regra, e meia regra na tela seria pior
    // do que nenhuma para quem investiga uma câmera com falso positivo alto.
    rule:
      evento.regraId !== null && evento.regraVersao !== null
        ? { id: evento.regraId, version: evento.regraVersao }
        : null,
    versions: evento.versions ?? null,
    // Dois campos com nomes parecidos e significados diferentes, e é de propósito que
    // eles não se parecem mais do que isso: `clip_state` é o desfecho do **upload**
    // (os bytes chegaram ao bucket?) e `clip` é o bloco que o agente mandou no POST, o
    // desfecho do **corte** na borda. Confundi-los é a armadilha registrada no
    // CLAUDE.md, e ela custa uma triagem esperando para sempre um vídeo que já subiu.
    clip_state: evento.clipeEstado,
    clip_error: evento.clipeErro,
    clip: evento.clip ?? null,
    triagem:
      vigente === undefined
        ? null
        : {
            decisao: vigente.decisao,
            decidido_em: vigente.criadoEm.toISOString(),
            decidido_por: { id: vigente.usuario.id, nome: vigente.usuario.nome },
            observacao: vigente.observacao,
            revisada: evento._count.triagens > 1,
          },
  };
}
