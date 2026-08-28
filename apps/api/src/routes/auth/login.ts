import type { FastifyInstance } from "fastify";
import { comEmailNormalizado } from "../../auth/email.js";
import { CredenciaisInvalidas, RequisicaoInvalida, UsuarioInativo } from "../../auth/errors.js";
import { emiteToken } from "../../auth/jwt.js";
import { confereSenha, gastaTempoDeSenha } from "../../auth/senha.js";
import { validaRequisicaoLogin, validaRespostaSessao } from "../../schemas/dashboard.js";
import { documentoDoUsuario, inclusaoDoUsuario } from "../usuarios/apresentacao.js";

// POST /v1/auth/login (§4.5): e-mail e senha viram um token de sessão.
//
// É a primeira rota do sistema cujo cliente é um navegador e não o agente da borda. A
// diferença que mais importa está no que se responde ao erro: para o agente, o status
// escolhe entre reenviar e desistir (policy.py); aqui ele escolhe entre mandar para a
// tela de login e mostrar um recado. Por isso credencial errada e conta desativada são
// respostas diferentes -- entrar de novo resolve uma e não resolve a outra.

interface CorpoLogin {
  email: string;
  senha: string;
}

export default async function authLoginRoutes(app: FastifyInstance) {
  app.post("/v1/auth/login", async (request, reply) => {
    // Normaliza antes de validar, não depois: o contrato descreve o e-mail já
    // normalizado, e recusar com 400 quem digitou " Ana@Loja.local " no celular seria
    // culpar o gerente pelo teclado dele (auth/email.ts).
    const validacao = validaRequisicaoLogin(comEmailNormalizado(request.body));
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com auth-login.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const corpo = comEmailNormalizado(request.body) as CorpoLogin;
    const email = corpo.email;

    const usuario = await app.prisma.usuario.findUnique({
      where: { email },
      include: inclusaoDoUsuario,
    });

    if (!usuario) {
      // Gasta o mesmo tempo de um hash real antes de recusar. Sem isso, "e-mail
      // desconhecido" responde em microssegundos e "senha errada" em ~100 ms, e a
      // diferença enumera com um cronômetro quem tem conta no sistema.
      await gastaTempoDeSenha(corpo.senha);
      throw new CredenciaisInvalidas();
    }

    // A senha é conferida ANTES de olhar `ativo`, de propósito: na ordem inversa, quem
    // chutasse e-mails descobriria quais existem (e estão desativados) sem nunca acertar
    // uma senha.
    if (!(await confereSenha(corpo.senha, usuario.senhaHash))) {
      throw new CredenciaisInvalidas();
    }
    if (!usuario.ativo) {
      throw new UsuarioInativo();
    }

    const token = emiteToken(app.sessaoSegredo, {
      usuarioId: usuario.id,
      tenantId: usuario.tenantId,
      // Carimba a versão vigente. Trocar a senha ou desativar a conta invalida este
      // token na requisição seguinte, sem tabela de sessão (auth/usuario-token.ts).
      tokenVersao: usuario.tokenVersao,
      expiraEmS: app.sessaoExpiraEmS,
    });

    const documento = {
      schema_version: 1,
      token,
      expires_in_s: app.sessaoExpiraEmS,
      usuario: documentoDoUsuario(usuario),
    };

    const validacaoResposta = validaRespostaSessao(documento);
    if (!validacaoResposta.valido) {
      // Defesa em profundidade, mesmo padrão das rotas de agente: bug interno vira 500,
      // nunca um 200 malformado. Aqui um 200 sem `usuario.lojas` deixaria o PWA numa
      // fila vazia que parece "nenhum evento" em vez de erro.
      request.log.error(
        { erros: validacaoResposta.erros, usuarioId: usuario.id },
        "resposta de POST /v1/auth/login não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(200).send(documento);
  });
}
