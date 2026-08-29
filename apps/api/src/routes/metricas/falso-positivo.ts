import type { FastifyInstance } from "fastify";
import { RequisicaoInvalida } from "../../auth/errors.js";
import { autenticaUsuario, exigePapelNaLoja } from "../../auth/usuario-token.js";
import { agrega } from "../../metricas/agrega.js";
import { fusoValido, janelaDeDias } from "../../metricas/dias.js";
import { contaPorCameraEDia } from "../../metricas/falso-positivo-query.js";
import { validaRespostaMetricaFalsoPositivo } from "../../schemas/dashboard.js";

// GET /v1/metricas/falso-positivo (R-1): a métrica número 1 do produto.
//
// É a leitura que justifica a triagem obrigatória. Tudo o que veio antes -- a borda que
// decide, o clipe, a fila, os dois toques -- existe para que este número exista e seja
// confiável; sem ele, "a taxa de falso positivo está aceitável" é opinião.
//
// A unidade é a **câmera**, porque a ação é por câmera: recalibra-se zona e limiar de uma
// de cada vez (§3.4). Uma média da loja esconde justamente a única que está errada, que é
// a que precisa ser encontrada.

const DIAS_PADRAO = 7;
// 90 dias é o que uma loja em piloto acumula num trimestre. O teto existe porque a
// consulta varre eventos por período: sem ele, um cliente pedindo 3650 dias faz a API
// varrer a tabela inteira, e o custo aparece como lentidão na fila de triagem.
const DIAS_MAXIMO = 90;

function texto(query: Record<string, unknown>, nome: string): string | undefined {
  const valor = query[nome];
  if (valor === undefined) {
    return undefined;
  }
  if (typeof valor !== "string") {
    throw new RequisicaoInvalida(`parâmetro ${nome} repetido ou inválido`);
  }
  return valor;
}

export default async function metricasFalsoPositivoRoutes(app: FastifyInstance) {
  app.get("/v1/metricas/falso-positivo", async (request, reply) => {
    const usuario = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const query = (request.query ?? {}) as Record<string, unknown>;

    // Mesma regra da fila: a loja pedida, se o usuário a alcança, ou todas as dele. É o
    // vínculo que decide, não o tenant (NFR-6 é o piso, não o teto).
    const lojaPedida = texto(query, "store_id");
    let lojas: string[];
    if (lojaPedida !== undefined) {
      exigePapelNaLoja(usuario, lojaPedida);
      lojas = [lojaPedida];
    } else {
      lojas = [...usuario.lojas.keys()];
    }

    const diasBruto = texto(query, "dias");
    const dias = diasBruto === undefined ? DIAS_PADRAO : Number.parseInt(diasBruto, 10);
    if (!Number.isInteger(dias) || dias < 1 || dias > DIAS_MAXIMO) {
      throw new RequisicaoInvalida(`parâmetro dias fora de 1..${DIAS_MAXIMO}`);
    }

    const registros = await app.prisma.loja.findMany({
      where: { id: { in: lojas }, tenantId: usuario.tenantId },
      select: { id: true, fusoHorario: true },
    });

    // O período tem um fuso só, porque a resposta declara um só. Lojas em fusos
    // diferentes não podem compartilhar uma janela de dias sem que "o dia" signifique
    // duas coisas ao mesmo tempo -- então a rota recusa, em vez de escolher um dos dois e
    // devolver um número que ninguém conseguiria auditar depois. Com uma loja por fuso o
    // caso nunca aparece; quando aparecer, é um 400 acionável e não um erro silencioso.
    const fusos = [...new Set(registros.map((loja) => loja.fusoHorario))];
    if (fusos.length > 1) {
      throw new RequisicaoInvalida(
        "as lojas estão em fusos horários diferentes: informe store_id para escolher uma",
      );
    }

    const fuso = fusos[0] ?? "UTC";
    if (!fusoValido(fuso)) {
      // Cadastro errado, não erro do cliente. Prosseguir devolveria dias deslocados sem
      // nenhum sinal, que é a falha que esta rota inteira existe para não cometer.
      request.log.error({ fuso, lojas }, "loja com fuso horário desconhecido");
      return reply.code(500).send();
    }

    const janela = janelaDeDias(dias, fuso, new Date());
    const linhas = await contaPorCameraEDia(app.prisma, {
      tenantId: usuario.tenantId,
      lojas: registros.map((loja) => loja.id),
      desde: janela.desde,
      ate: janela.ate,
    });

    const documento = agrega(linhas, janela, fuso);

    const validacaoResposta = validaRespostaMetricaFalsoPositivo(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, tenantId: usuario.tenantId },
        "resposta de GET /v1/metricas/falso-positivo não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(200).send(documento);
  });
}
