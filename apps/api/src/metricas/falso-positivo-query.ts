import { Prisma, type PrismaClient } from "@prisma/client";

// A agregação do R-1: falso positivo por câmera e por dia.
//
// SQL cru, e não `groupBy` do Prisma, por duas razões que o client não alcança: a
// decisão **vigente** de um evento é a última por `seq` (um LATERAL por evento), e o dia
// é o dia civil **da loja**, o que exige conversão de fuso dentro do banco. Fazer
// qualquer uma das duas em JavaScript significaria trazer todos os eventos do período
// para a memória da API só para contá-los.

/** O teto da NFR-2: 3 alertas falsos por câmera por dia. */
export const LIMITE_DIARIO = 3;

export interface LinhaPorDia {
  lojaId: string;
  cameraId: string;
  /** Data civil no fuso da loja, `YYYY-MM-DD`. */
  dia: string;
  confirmado: number;
  falsoPositivo: number;
  inconclusivo: number;
  pendentes: number;
}

export interface JanelaConsulta {
  tenantId: string;
  lojas: string[];
  /** Instante UTC do início do primeiro dia local, inclusivo. */
  desde: Date;
  /** Instante UTC do fim do último dia local, exclusivo. */
  ate: Date;
}

/**
 * Contagem por câmera e por dia local, já com a decisão vigente resolvida.
 *
 * Devolve só os dias em que houve evento; os dias vazios são preenchidos por quem
 * agrega, porque o denominador da média é o período pedido e não os dias com movimento.
 */
export async function contaPorCameraEDia(
  prisma: PrismaClient,
  janela: JanelaConsulta,
): Promise<LinhaPorDia[]> {
  if (janela.lojas.length === 0) {
    return [];
  }

  const linhas = await prisma.$queryRaw<
    Array<{
      loja_id: string;
      camera_id: string;
      dia: string;
      confirmado: bigint;
      falso_positivo: bigint;
      inconclusivo: bigint;
      pendentes: bigint;
    }>
  >`
    SELECT
      e.loja_id,
      e.camera_id,
      -- Os DOIS "AT TIME ZONE" são necessários, e não é redundância. A coluna é
      -- 'timestamp without time zone' guardando UTC (o Prisma grava assim): o primeiro
      -- **rotula** esse valor como UTC, o segundo **converte** para o fuso da loja.
      -- Sem o primeiro, o Postgres interpretaria o instante no fuso do servidor -- que
      -- muda de máquina para máquina -- e o dia sairia deslocado sem erro nenhum.
      to_char(
        (e.ocorrido_em AT TIME ZONE 'UTC' AT TIME ZONE l.fuso_horario)::date,
        'YYYY-MM-DD'
      ) AS dia,
      COUNT(*) FILTER (WHERE v.decisao = 'confirmado')::bigint      AS confirmado,
      COUNT(*) FILTER (WHERE v.decisao = 'falso_positivo')::bigint  AS falso_positivo,
      COUNT(*) FILTER (WHERE v.decisao = 'inconclusivo')::bigint    AS inconclusivo,
      COUNT(*) FILTER (WHERE v.decisao IS NULL)::bigint             AS pendentes
    FROM evento e
    JOIN loja l ON l.id = e.loja_id
    -- A decisão vigente é a última por seq, nunca por criado_em: duas decisões no mesmo
    -- milissegundo empatariam e "vigente" viraria cara ou coroa (schema.prisma).
    LEFT JOIN LATERAL (
      SELECT t.decisao
      FROM triagem t
      WHERE t.event_id = e.event_id
      ORDER BY t.seq DESC
      LIMIT 1
    ) v ON TRUE
    WHERE e.tenant_id = ${janela.tenantId}
      AND e.loja_id IN (${Prisma.join(janela.lojas)})
      -- O andaime de gatilho da instalação fica fora da métrica inteira: um instalador
      -- testando 40 vezes numa tarde afundaria a estatística da câmera que ele estava
      -- justamente calibrando (R-1).
      AND e.source = 'rule'
      AND e.ocorrido_em >= ${janela.desde}
      AND e.ocorrido_em < ${janela.ate}
    GROUP BY e.loja_id, e.camera_id, dia
  `;

  // COUNT devolve bigint, que o JSON não serializa. A conversão acontece aqui, na
  // fronteira, e não na rota: um bigint escapando daqui estoura no JSON.stringify com
  // "Do not know how to serialize a BigInt", longe da consulta que o produziu.
  return linhas.map((linha) => ({
    lojaId: linha.loja_id,
    cameraId: linha.camera_id,
    dia: linha.dia,
    confirmado: Number(linha.confirmado),
    falsoPositivo: Number(linha.falso_positivo),
    inconclusivo: Number(linha.inconclusivo),
    pendentes: Number(linha.pendentes),
  }));
}
