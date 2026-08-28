import type { FastifyInstance } from "fastify";
import { autenticaUsuario, lojasAdministradas } from "../../auth/usuario-token.js";
import { validaRespostaUsuarios } from "../../schemas/dashboard.js";
import { documentoDoUsuario, inclusaoDoUsuario } from "./apresentacao.js";

// GET /v1/usuarios: quem o autor pode administrar.
//
// Não é "os usuários do tenant". O recorte é o mesmo da escrita -- as lojas em que o
// autor é admin -- porque uma listagem mais larga do que o poder de edição é um
// vazamento sem contrapartida: numa rede, o admin da loja A enumeraria o quadro inteiro
// da loja B sem poder mexer em nada lá.
//
// Quem não administra loja nenhuma recebe lista vazia, e não 403. Um gerente que abre a
// tela de equipe está fazendo uma pergunta legítima ("quem mais tria aqui?"), e a
// resposta honesta é "nada que você administre", não um erro.

export default async function usuariosListarRoutes(app: FastifyInstance) {
  app.get("/v1/usuarios", async (request, reply) => {
    const autor = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const lojas = lojasAdministradas(autor);

    const usuarios =
      lojas.length === 0
        ? []
        : await app.prisma.usuario.findMany({
            where: {
              tenantId: autor.tenantId,
              lojas: { some: { lojaId: { in: lojas } } },
            },
            include: inclusaoDoUsuario,
            orderBy: { nome: "asc" },
          });

    const documento = { schema_version: 1, usuarios: usuarios.map(documentoDoUsuario) };

    const validacaoResposta = validaRespostaUsuarios(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, tenantId: autor.tenantId },
        "resposta de GET /v1/usuarios não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(200).send(documento);
  });
}
