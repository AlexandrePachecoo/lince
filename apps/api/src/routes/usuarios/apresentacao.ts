import type { Prisma } from "@prisma/client";

// Como uma linha de `usuario` vira o documento que o dashboard lê (usuario.v1.json).
// Existe por um motivo específico: é o ponto único por onde a senha **não** passa. Um
// `select` esquecido numa rota qualquer devolveria `senhaHash` no corpo, e a única
// defesa contra isso é nenhuma rota montar essa resposta por conta própria.

export const inclusaoDoUsuario = { lojas: true } satisfies Prisma.UsuarioInclude;

export type UsuarioComLojas = Prisma.UsuarioGetPayload<{ include: typeof inclusaoDoUsuario }>;

export interface UsuarioDocumento {
  id: string;
  tenant_id: string;
  email: string;
  nome: string;
  ativo: boolean;
  lojas: Array<{ store_id: string; papel: string }>;
}

export function documentoDoUsuario(usuario: UsuarioComLojas): UsuarioDocumento {
  return {
    id: usuario.id,
    tenant_id: usuario.tenantId,
    email: usuario.email,
    nome: usuario.nome,
    ativo: usuario.ativo,
    // Ordem estável: o dashboard renderiza a lista como veio, e uma ordem que muda a
    // cada requisição faz as caixas do formulário pularem de lugar entre um GET e outro.
    lojas: [...usuario.lojas]
      .sort((a, b) => a.lojaId.localeCompare(b.lojaId))
      .map((vinculo) => ({ store_id: vinculo.lojaId, papel: vinculo.papel })),
  };
}
