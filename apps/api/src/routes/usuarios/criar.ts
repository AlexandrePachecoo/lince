import type { FastifyInstance } from "fastify";
import { comEmailNormalizado } from "../../auth/email.js";
import { EmailJaCadastrado, RequisicaoInvalida } from "../../auth/errors.js";
import { geraHashSenha } from "../../auth/senha.js";
import { autenticaUsuario } from "../../auth/usuario-token.js";
import { validaRequisicaoUsuarioNovo, validaRespostaUsuario } from "../../schemas/dashboard.js";
import { documentoDoUsuario, inclusaoDoUsuario } from "./apresentacao.js";
import { type VinculoPedido, confereVinculos } from "./vinculos.js";

// POST /v1/usuarios (§6, USUARIO/USUARIO_LOJA): cadastra quem vai triar.
//
// O tenant do usuário novo é o de quem o criou, e não vem no corpo. Um tenant_id vindo
// do cliente seria a forma mais direta de furar o NFR-6 -- e não haveria como distinguir
// abuso de engano, porque o campo teria um uso legítimo aparente.
//
// Não existe auto-cadastro: o primeiro usuário de um tenant nasce pelo scripts/seed.ts
// (hoje) ou pelo provisionamento da rede (quando existir). É a mesma disciplina do
// agente, cuja credencial também não se cria sozinha (§5.1).

interface CorpoUsuarioNovo {
  email: string;
  nome: string;
  senha: string;
  lojas: VinculoPedido[];
}

export default async function usuariosCriarRoutes(app: FastifyInstance) {
  app.post("/v1/usuarios", async (request, reply) => {
    const autor = await autenticaUsuario(
      app.prisma,
      request.headers.authorization,
      app.sessaoSegredo,
    );

    // Mesma normalização do login, pela mesma função: se divergissem, existiria conta
    // criada com um endereço que nenhum login alcança.
    const validacao = validaRequisicaoUsuarioNovo(comEmailNormalizado(request.body));
    if (!validacao.valido) {
      throw new RequisicaoInvalida(
        `corpo não bate com usuario-novo.v1.json: ${JSON.stringify(validacao.erros)}`,
      );
    }
    const corpo = comEmailNormalizado(request.body) as CorpoUsuarioNovo;

    await confereVinculos(app.prisma, autor, corpo.lojas, "criar usuário");

    const email = corpo.email;
    const senhaHash = await geraHashSenha(corpo.senha);

    let criado: Awaited<ReturnType<typeof app.prisma.usuario.create>>;
    try {
      criado = await app.prisma.usuario.create({
        data: {
          tenantId: autor.tenantId,
          email,
          nome: corpo.nome,
          senhaHash,
          lojas: {
            create: corpo.lojas.map((v) => ({ lojaId: v.store_id, papel: v.papel })),
          },
        },
      });
    } catch (erro) {
      // A unicidade é do banco, não de um SELECT antes do INSERT: duas criações
      // simultâneas do mesmo e-mail passariam pelas duas verificações e uma das duas
      // falharia depois, de um jeito mais feio. P2002 é a violação de unique do Prisma.
      if (typeof erro === "object" && erro !== null && "code" in erro && erro.code === "P2002") {
        throw new EmailJaCadastrado();
      }
      throw erro;
    }

    const usuario = await app.prisma.usuario.findUniqueOrThrow({
      where: { id: criado.id },
      include: inclusaoDoUsuario,
    });
    const documento = { schema_version: 1, usuario: documentoDoUsuario(usuario) };

    const validacaoResposta = validaRespostaUsuario(documento);
    if (!validacaoResposta.valido) {
      request.log.error(
        { erros: validacaoResposta.erros, usuarioId: usuario.id },
        "resposta de POST /v1/usuarios não bate com o schema",
      );
      return reply.code(500).send();
    }

    return reply.code(201).send(documento);
  });
}
