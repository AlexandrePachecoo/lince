import type { Prisma } from "@prisma/client";
import type { FastifyInstance } from "fastify";
import { RequisicaoInvalida } from "../../auth/errors.js";
import { autenticaUsuario, exigePapelNaLoja } from "../../auth/usuario-token.js";
import { validaRespostaFila } from "../../schemas/dashboard.js";
import { documentoDoEvento, inclusaoDaFila } from "./apresentacao.js";
import { codificaCursor, decodificaCursor } from "./cursor.js";

// GET /v1/events (§4.5): a fila de triagem, que é a primeira tela do dashboard.
//
// O padrão é `triagem=pendentes` porque a fila é uma lista de trabalho, não um
// histórico: o que o gerente abre no corredor é o que ainda não foi decidido. O
// histórico existe em `todas`, e é dele que sai a leitura de falso positivo por câmera
// (R-1) quando essa métrica for construída.
//
// Ordem: do mais recente para o mais antigo. Um alerta de agora ainda é acionável --
// dá para olhar a câmera, dá para falar com alguém; um de ontem virou estatística.

const LIMITE_PADRAO = 50;
// Casa com o maxItems de fila-triagem.v1.json: pedir mais devolveria um documento que a
// própria validação de resposta recusaria, e o cliente veria 500 no lugar de 400.
const LIMITE_MAXIMO = 200;

const FILTROS_TRIAGEM = ["pendentes", "triados", "todas"] as const;
type FiltroTriagem = (typeof FILTROS_TRIAGEM)[number];

function texto(query: Record<string, unknown>, nome: string): string | undefined {
  const valor = query[nome];
  if (valor === undefined) {
    return undefined;
  }
  // Parâmetro repetido vira array no Fastify. Recusar é melhor do que escolher um dos
  // dois: `?store_id=a&store_id=b` significa que o cliente está errado, e adivinhar
  // esconderia o erro dele numa lista silenciosamente filtrada pela loja errada.
  if (typeof valor !== "string") {
    throw new RequisicaoInvalida(`parâmetro ${nome} repetido ou inválido`);
  }
  return valor;
}

function instante(query: Record<string, unknown>, nome: string): Date | undefined {
  const valor = texto(query, nome);
  if (valor === undefined) {
    return undefined;
  }
  const data = new Date(valor);
  if (Number.isNaN(data.getTime())) {
    throw new RequisicaoInvalida(`parâmetro ${nome} não é um instante ISO-8601`);
  }
  return data;
}

export default async function eventsListarRoutes(app: FastifyInstance) {
  app.get("/v1/events", async (request, reply) => {
    const usuario = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const query = (request.query ?? {}) as Record<string, unknown>;

    // Quais lojas: a pedida, se o usuário a alcança, ou todas as dele. Nunca "todas do
    // tenant" -- numa rede, o gerente de uma loja não tem por que ver a fila da outra, e
    // é o vínculo que decide isso, não o tenant (NFR-6 é o piso, não o teto).
    const lojaPedida = texto(query, "store_id");
    let lojas: string[];
    if (lojaPedida !== undefined) {
      exigePapelNaLoja(usuario, lojaPedida);
      lojas = [lojaPedida];
    } else {
      lojas = [...usuario.lojas.keys()];
    }

    const filtroBruto = texto(query, "triagem") ?? "pendentes";
    if (!FILTROS_TRIAGEM.includes(filtroBruto as FiltroTriagem)) {
      throw new RequisicaoInvalida(`parâmetro triagem inválido: use ${FILTROS_TRIAGEM.join(", ")}`);
    }
    const filtro = filtroBruto as FiltroTriagem;

    const limiteBruto = texto(query, "limite");
    const limite = limiteBruto === undefined ? LIMITE_PADRAO : Number.parseInt(limiteBruto, 10);
    if (!Number.isInteger(limite) || limite < 1 || limite > LIMITE_MAXIMO) {
      throw new RequisicaoInvalida(`parâmetro limite fora de 1..${LIMITE_MAXIMO}`);
    }

    const cameraId = texto(query, "camera_id");
    const desde = instante(query, "desde");
    const ate = instante(query, "ate");
    const cursorBruto = texto(query, "cursor");
    const cursor = cursorBruto === undefined ? undefined : decodificaCursor(cursorBruto);

    const where: Prisma.EventoWhereInput = {
      // tenant no WHERE, sempre, mesmo com lojaId já resolvido a partir dos vínculos:
      // defesa em profundidade do NFR-6, igual a config.ts e clip.ts.
      tenantId: usuario.tenantId,
      lojaId: { in: lojas },
      ...(cameraId !== undefined ? { cameraId } : {}),
      ...(desde !== undefined || ate !== undefined
        ? {
            ocorridoEm: {
              ...(desde !== undefined ? { gte: desde } : {}),
              ...(ate !== undefined ? { lte: ate } : {}),
            },
          }
        : {}),
      // "Pendente" é ausência de triagem, não uma coluna de estado no evento. A triagem
      // é append-only e vive na tabela dela; uma coluna `triado` no evento seria um
      // segundo lugar dizendo a mesma coisa, e os dois divergiriam na primeira correção.
      ...(filtro === "pendentes" ? { triagens: { none: {} } } : {}),
      ...(filtro === "triados" ? { triagens: { some: {} } } : {}),
      ...(cursor !== undefined
        ? {
            OR: [
              { ocorridoEm: { lt: cursor.ocorridoEm } },
              { ocorridoEm: cursor.ocorridoEm, eventId: { lt: cursor.eventId } },
            ],
          }
        : {}),
    };

    // Um a mais do que o pedido: é assim que se sabe que há próxima página sem um
    // COUNT(*) sobre a fila inteira, que numa loja com meses de histórico custa caro
    // para responder uma pergunta de sim ou não.
    const linhas = await app.prisma.evento.findMany({
      where,
      orderBy: [{ ocorridoEm: "desc" }, { eventId: "desc" }],
      take: limite + 1,
      include: inclusaoDaFila,
    });

    const temMais = linhas.length > limite;
    const pagina = temMais ? linhas.slice(0, limite) : linhas;
    const ultimo = pagina[pagina.length - 1];

    const documento = {
      schema_version: 1,
      eventos: pagina.map(documentoDoEvento),
      proximo_cursor:
        temMais && ultimo !== undefined
          ? codificaCursor({ ocorridoEm: ultimo.ocorridoEm, eventId: ultimo.eventId })
          : null,
    };

    const validacaoResposta = validaRespostaFila(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, tenantId: usuario.tenantId },
        "resposta de GET /v1/events não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(200).send(documento);
  });
}
