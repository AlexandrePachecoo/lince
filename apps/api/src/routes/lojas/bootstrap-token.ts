import type { FastifyInstance } from "fastify";
import { criaTokenBootstrap } from "../../auth/bootstrap-token.js";
import { autenticaUsuario, exigeAdminNaLoja } from "../../auth/usuario-token.js";
import { validaRespostaBootstrapToken } from "../../schemas/dashboard.js";
import { LojaDesconhecida } from "../usuarios/errors.js";

// POST /v1/lojas/{store_id}/bootstrap-token (§5.1): o "gerado no dashboard" do
// provisionamento. Devolve o token de uso único que o box troca por credencial no
// POST /v1/agents/register.
//
// Só admin da loja. O que sai daqui vira a credencial de um box, e um box puxa a
// configuração da loja -- com as URLs RTSP das câmeras e a senha delas dentro. Quem
// pode pôr um box na loja pode ver a loja inteira; é o mesmo degrau de quem cadastra
// gente nela, e por isso é o mesmo papel.
//
// É a credencial HUMANA que gera o token, e a rota não aceita a do agente: um box
// comprometido que pudesse emitir token de bootstrap fabricaria agentes novos para a
// própria loja, e desativá-lo deixaria de encerrar o incidente.

export default async function lojasBootstrapTokenRoutes(app: FastifyInstance) {
  app.post<{ Params: { storeId: string } }>(
    "/v1/lojas/:storeId/bootstrap-token",
    async (request, reply) => {
      const autor = await autenticaUsuario(
        app.prisma,
        request.headers.authorization,
        app.sessaoSegredo,
      );
      const { storeId } = request.params;

      // Fora do tenant é 404, dentro dele sem o papel é 403 -- nesta ordem. Quem está
      // autenticado já sabe que as lojas da rede existem, e "peça ao admin" é acionável;
      // confirmar que um id existe em OUTRA rede já é vazamento. O tenant no WHERE é o
      // NFR-6, e é o que faz as duas respostas saírem do banco e não do vínculo.
      const loja = await app.prisma.loja.findFirst({
        where: { id: storeId, tenantId: autor.tenantId },
        select: { id: true },
      });
      if (loja === null) {
        throw new LojaDesconhecida(storeId);
      }
      exigeAdminNaLoja(autor, loja.id, "gerar token de instalação do box");

      const { token, expiraEm } = await criaTokenBootstrap(app.prisma, loja.id, {
        usuarioId: autor.usuarioId,
        email: autor.email,
      });

      const documento = {
        schema_version: 1,
        store_id: loja.id,
        bootstrap_token: token,
        expires_at: expiraEm.toISOString(),
      };

      const validacao = validaRespostaBootstrapToken(documento);
      if (!validacao.valido) {
        // Sem `erros` no log: o Ajv devolve o valor recusado, e aqui o valor é o token.
        request.log.error({ lojaId: loja.id }, "resposta de bootstrap-token não bate com o schema");
        return reply.code(500).send();
      }

      // O corpo é um segredo que só existe nesta resposta. Proxy e navegador não podem
      // guardar uma cópia que ninguém sabe que existe.
      return reply.code(201).header("cache-control", "no-store").send(documento);
    },
  );
}
