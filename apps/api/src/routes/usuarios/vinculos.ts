import type { PapelUsuario, PrismaClient } from "@prisma/client";
import { RequisicaoInvalida } from "../../auth/errors.js";
import { type UsuarioAutenticado, exigeAdminNaLoja } from "../../auth/usuario-token.js";
import { LojaDesconhecida } from "./errors.js";

// Conferência compartilhada por POST /v1/usuarios e PATCH /v1/usuarios/{id}: quem chama
// pode mesmo mexer neste conjunto de lojas?
//
// A regra é uma só e vale para os dois: **admin em todas as lojas envolvidas**. Não há
// papel de tenant neste sistema (§6 define papel na loja), e inventar um só para
// administrar gente criaria um segundo eixo de permissão que ninguém mais usa -- o tipo
// de coisa que se confere errado no primeiro caso de canto.

export interface VinculoPedido {
  store_id: string;
  papel: PapelUsuario;
}

export async function confereVinculos(
  prisma: PrismaClient,
  autor: UsuarioAutenticado,
  vinculos: readonly VinculoPedido[],
  acao: string,
): Promise<void> {
  const lojas = vinculos.map((v) => v.store_id);

  // Repetido é engano de formulário, e sem esta conferência viraria violação de chave
  // primária composta lá embaixo -- 500 no lugar de um 400 que diz o que fazer.
  if (new Set(lojas).size !== lojas.length) {
    throw new RequisicaoInvalida("loja repetida na lista de vínculos");
  }

  for (const lojaId of lojas) {
    exigeAdminNaLoja(autor, lojaId, acao);
  }

  // O tenant entra no WHERE mesmo já tendo passado pela conferência de papel acima
  // (NFR-6): a lista de vínculos do autor vem do banco, e um cadastro inconsistente não
  // pode ser o caminho por onde um usuário nasce apontando para a loja de outra rede.
  const existentes = await prisma.loja.findMany({
    where: { id: { in: lojas }, tenantId: autor.tenantId },
    select: { id: true },
  });
  const conhecidas = new Set(existentes.map((loja) => loja.id));
  for (const lojaId of lojas) {
    if (!conhecidas.has(lojaId)) {
      throw new LojaDesconhecida(lojaId);
    }
  }
}
