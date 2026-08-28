import type { FastifyInstance } from "fastify";
import { RequisicaoInvalida, SemPermissao } from "../../auth/errors.js";
import { geraHashSenha } from "../../auth/senha.js";
import { autenticaUsuario } from "../../auth/usuario-token.js";
import {
  validaRequisicaoUsuarioAlteracao,
  validaRespostaUsuario,
} from "../../schemas/dashboard.js";
import { documentoDoUsuario, inclusaoDoUsuario } from "./apresentacao.js";
import { UsuarioDesconhecido } from "./errors.js";
import { type VinculoPedido, confereVinculos } from "./vinculos.js";

// PATCH /v1/usuarios/{id}: nome, senha, ativo e vínculos.
//
// `lojas`, quando vem, **substitui** a lista inteira. É o formulário de edição do
// dashboard, com uma caixa por loja: um PATCH que só somasse deixaria o admin sem como
// tirar acesso de ninguém, e "tirar acesso" é justamente a operação urgente.
//
// Não há DELETE de usuário nesta fatia, e não é esquecimento: quem saiu da empresa vira
// `ativo: false`. Apagar removeria o autor de triagens que continuam valendo, e o banco
// recusa isso de propósito (schema.prisma, Triagem.usuarioId sem onDelete).

interface CorpoAlteracao {
  nome?: string;
  ativo?: boolean;
  senha?: string;
  lojas?: VinculoPedido[];
}

export default async function usuariosAlterarRoutes(app: FastifyInstance) {
  app.patch("/v1/usuarios/:usuarioId", async (request, reply) => {
    const autor = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );
    const { usuarioId } = request.params as { usuarioId: string };

    const validacao = validaRequisicaoUsuarioAlteracao(request.body);
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com usuario-alteracao.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const corpo = request.body as CorpoAlteracao;

    // Tenant no WHERE (NFR-6). Usuário de outro tenant responde o mesmo 404 de usuário
    // inexistente: confirmar que um e-mail tem conta em outra rede já é vazamento.
    const alvo = await app.prisma.usuario.findFirst({
      where: { id: usuarioId, tenantId: autor.tenantId },
      include: inclusaoDoUsuario,
    });
    if (!alvo) {
      throw new UsuarioDesconhecido();
    }

    // Admin nas lojas que o alvo tem **hoje**, e não só nas que vai passar a ter. Sem
    // isso, um admin da loja A renomearia ou desativaria alguém que também trabalha na
    // loja B -- efeito numa loja que ele não administra, a partir de uma tela em que a
    // loja B nem aparece.
    await confereVinculos(
      app.prisma,
      autor,
      alvo.lojas.map((v) => ({ store_id: v.lojaId, papel: v.papel })),
      "alterar usuário",
    );
    if (corpo.lojas !== undefined) {
      await confereVinculos(app.prisma, autor, corpo.lojas, "alterar vínculos");
    }

    // Trava contra o tiro no pé mais fácil de dar: desativar a própria conta enquanto se
    // administra a loja. Não cobre todo caso de tranca-se-fora (dá para rebaixar o
    // último admin de uma loja), e cobrir aquilo exigiria uma regra de "sempre ao menos
    // um admin por loja" que ainda não tem dono no produto.
    if (corpo.ativo === false && alvo.id === autor.usuarioId) {
      throw new SemPermissao("desativar a própria conta");
    }

    // Trocar a senha derruba as sessões abertas daquele usuário (Usuario.tokenVersao).
    // É o que se espera de uma senha trocada porque vazou: se as sessões antigas
    // sobrevivessem, trocá-la não resolveria o problema que motivou a troca.
    const senhaHash = corpo.senha === undefined ? undefined : await geraHashSenha(corpo.senha);

    await app.prisma.$transaction(async (tx) => {
      await tx.usuario.update({
        where: { id: alvo.id },
        data: {
          ...(corpo.nome !== undefined ? { nome: corpo.nome } : {}),
          ...(corpo.ativo !== undefined ? { ativo: corpo.ativo } : {}),
          ...(senhaHash !== undefined ? { senhaHash, tokenVersao: { increment: 1 } } : {}),
        },
      });
      if (corpo.lojas !== undefined) {
        // Apaga e recria dentro da mesma transação: um instante com o usuário sem
        // vínculo nenhum não pode ser observável por uma requisição em curso, senão a
        // fila dele voltaria vazia no meio de uma edição de cadastro.
        await tx.usuarioLoja.deleteMany({ where: { usuarioId: alvo.id } });
        await tx.usuarioLoja.createMany({
          data: corpo.lojas.map((v) => ({
            usuarioId: alvo.id,
            lojaId: v.store_id,
            papel: v.papel,
          })),
        });
      }
    });

    const atualizado = await app.prisma.usuario.findUniqueOrThrow({
      where: { id: alvo.id },
      include: inclusaoDoUsuario,
    });
    const documento = { schema_version: 1, usuario: documentoDoUsuario(atualizado) };

    const validacaoResposta = validaRespostaUsuario(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, usuarioId: alvo.id },
        "resposta de PATCH /v1/usuarios/{id} não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(200).send(documento);
  });
}
