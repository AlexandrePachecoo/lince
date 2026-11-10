import type { AlertaDecisao, EventoOrigem, PrismaClient } from "@prisma/client";
import type { ConfigPush } from "./config.js";

// Se um evento novo vira notificação (§4.6, R-12). Fica separado do envio porque é a
// parte que decide quantas vezes o celular do gerente toca por dia — e é esse número,
// mais que a taxa de acerto, que faz ele desligar a notificação (R-1).
//
// A decisão é gravada (tabela `alerta`) porque a do próximo evento depende dela: a
// janela procura o último notificado da câmera, e o teto conta os notificados do dia.

export interface EventoParaAlerta {
  eventId: string;
  tenantId: string;
  lojaId: string;
  cameraId: string;
  source: EventoOrigem;
  ocorridoEm: Date;
}

export interface LojaDoAlerta {
  nome: string;
  fusoHorario: string;
}

export interface Decisao {
  decisao: AlertaDecisao;
  /**
   * Este é o primeiro evento do dia a bater no teto. É quando o aparelho recebe um aviso
   * de que os próximos vão só para a fila — uma vez, não a cada evento barrado, senão o
   * aviso do teto seria a nova enxurrada.
   */
  primeiroTetoDoDia: boolean;
  loja: LojaDoAlerta;
}

type ParametrosDecisao = Pick<
  ConfigPush,
  "janelaAgrupamentoS" | "tetoDiarioPorLoja" | "atrasoMaximoS"
>;

export async function decideAlerta(
  prisma: PrismaClient,
  evento: EventoParaAlerta,
  agora: Date,
  parametros: ParametrosDecisao,
): Promise<Decisao> {
  return prisma.$transaction(async (tx) => {
    // Uma decisão por vez por loja. Dois eventos da mesma câmera chegando juntos — a
    // borda drenando a fila depois de o link voltar faz exatamente isso — leriam os dois
    // "nenhum notificado na janela" e notificariam os dois. O teto é por loja, e por isso
    // a trava também é: travar por câmera deixaria duas câmeras furarem o teto juntas.
    await tx.$queryRaw`SELECT pg_advisory_xact_lock(hashtext(${evento.lojaId}))::text`;

    const loja = await tx.loja.findUniqueOrThrow({
      where: { id: evento.lojaId },
      select: { nome: true, fusoHorario: true },
    });

    let decisao: AlertaDecisao;
    let primeiroTetoDoDia = false;

    if (agora.getTime() - evento.ocorridoEm.getTime() > parametros.atrasoMaximoS * 1000) {
      decisao = "atrasado";
    } else {
      const inicioDaJanela = new Date(agora.getTime() - parametros.janelaAgrupamentoS * 1000);
      const recente = await tx.alerta.findFirst({
        where: {
          lojaId: evento.lojaId,
          cameraId: evento.cameraId,
          source: evento.source,
          decisao: "notificado",
          criadoEm: { gte: inicioDaJanela },
        },
        select: { eventId: true },
      });
      if (recente) {
        decisao = "agrupado";
      } else {
        const hoje = await contaNoDiaDaLoja(tx, evento.lojaId, loja.fusoHorario, agora);
        if (hoje.notificados >= parametros.tetoDiarioPorLoja) {
          decisao = "teto";
          primeiroTetoDoDia = hoje.teto === 0;
        } else {
          decisao = "notificado";
        }
      }
    }

    await tx.alerta.create({
      data: {
        eventId: evento.eventId,
        tenantId: evento.tenantId,
        lojaId: evento.lojaId,
        cameraId: evento.cameraId,
        source: evento.source,
        decisao,
        criadoEm: agora,
      },
    });

    return { decisao, primeiroTetoDoDia, loja };
  });
}

type Transacao = Parameters<Parameters<PrismaClient["$transaction"]>[0]>[0];

/**
 * Notificados e barrados pelo teto no dia de hoje **da loja**. O dia é o do fuso dela,
 * como na métrica do R-1: em UTC o teto zeraria às 21h de Brasília, no meio do
 * movimento, e a noite ganharia um segundo teto inteiro.
 *
 * `criado_em` é `timestamp without time zone` gravado em UTC, e por isso leva os DOIS
 * `AT TIME ZONE` (armadilha registrada no CLAUDE.md): o primeiro rotula a coluna como
 * UTC, o segundo converte para o fuso da loja. Com um só, o Postgres interpretaria o
 * instante no fuso do servidor.
 */
async function contaNoDiaDaLoja(
  tx: Transacao,
  lojaId: string,
  fuso: string,
  agora: Date,
): Promise<{ notificados: number; teto: number }> {
  const linhas = await tx.$queryRaw<{ notificados: bigint; teto: bigint }[]>`
    SELECT
      count(*) FILTER (WHERE decisao = 'notificado') AS notificados,
      count(*) FILTER (WHERE decisao = 'teto') AS teto
    FROM alerta
    WHERE loja_id = ${lojaId}
      AND decisao IN ('notificado', 'teto')
      AND (criado_em AT TIME ZONE 'UTC' AT TIME ZONE ${fuso})::date
          = (${agora}::timestamptz AT TIME ZONE ${fuso})::date
  `;
  const linha = linhas[0];
  return { notificados: Number(linha?.notificados ?? 0), teto: Number(linha?.teto ?? 0) };
}
