import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, beforeEach, test } from "node:test";
import {
  SemAcessoALoja,
  SemPermissao,
  SessaoExpirada,
  TokenAusente,
  TokenInvalido,
  UsuarioInativo,
} from "../../src/auth/errors.js";
import { emiteToken } from "../../src/auth/jwt.js";
import {
  type UsuarioAutenticado,
  autenticaUsuario,
  exigeAdminNaLoja,
  exigePapelNaLoja,
  lojasAdministradas,
} from "../../src/auth/usuario-token.js";
import { SEGREDO_TESTE } from "../helpers/build-app.js";
import { limpaBanco, prismaTeste, semeiaLoja, semeiaUsuario } from "../helpers/test-db.js";

beforeEach(async () => {
  await limpaBanco();
});
after(async () => {
  await prismaTeste.$disconnect();
});

function token(usuarioId: string, tenantId: string, tokenVersao = 0): string {
  return emiteToken(SEGREDO_TESTE, { usuarioId, tenantId, tokenVersao, expiraEmS: 3600 });
}

function autentica(header: string | undefined): Promise<UsuarioAutenticado> {
  return autenticaUsuario(prismaTeste, header, SEGREDO_TESTE);
}

test("cabeçalho ausente ou fora do formato Bearer -> TokenAusente", async () => {
  for (const header of [undefined, "", "Basic abc", "Bearer", "Bearer   "]) {
    await assert.rejects(() => autentica(header), TokenAusente, `header: ${String(header)}`);
  }
});

test("token bem assinado para usuário que não existe -> TokenInvalido", async () => {
  // Acontece de verdade: o usuário foi apagado do banco (restauração parcial, limpeza
  // manual) e o token dele continua no celular de alguém, válido pela assinatura. Sem a
  // leitura do cadastro, ele autorizaria com um sub que não existe mais.
  await assert.rejects(
    () => autentica(`Bearer ${token(randomUUID(), "tenant-qualquer")}`),
    TokenInvalido,
  );
});

test("tenant do token diferente do cadastro -> TokenInvalido", async () => {
  // Não deveria acontecer -- usuário não muda de tenant hoje -- e é por isso que passar
  // batido seria pior: o token continuaria autorizando leitura no tenant antigo, e o
  // NFR-6 dependeria de uma coisa que "não acontece".
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });

  await assert.rejects(
    () => autentica(`Bearer ${token(usuario.usuarioId, "outro-tenant")}`),
    TokenInvalido,
  );
});

test("papel e lojas vêm do cadastro, não do token", async () => {
  // O JWT poupa a tabela de sessão, não o SELECT. Se as lojas viajassem congeladas no
  // token, tirar um gerente de uma loja só valeria quando a sessão dele vencesse -- e
  // "tirar acesso" é justamente a operação urgente.
  const loja = await semeiaLoja();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const dele = `Bearer ${token(usuario.usuarioId, usuario.tenantId)}`;

  const antes = await autentica(dele);
  assert.equal(antes.lojas.get(loja.lojaId), "gerente");
  assert.equal(antes.lojas.get(outra.lojaId), undefined);

  await prismaTeste.usuarioLoja.create({
    data: { usuarioId: usuario.usuarioId, lojaId: outra.lojaId, papel: "admin" },
  });

  const depois = await autentica(dele);
  assert.equal(depois.lojas.get(outra.lojaId), "admin", "o mesmo token, o cadastro novo");
});

test("versão de token antiga vira SessaoExpirada, não TokenInvalido", async () => {
  // A distinção existe para quem depura: "caiu sozinho" é sessão derrubada por troca de
  // senha, e não token adulterado. Confundi-los faz um evento normal parecer ataque no
  // log -- e o inverso, que é pior.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  await prismaTeste.usuario.update({
    where: { id: usuario.usuarioId },
    data: { tokenVersao: 2 },
  });

  await assert.rejects(
    () => autentica(`Bearer ${token(usuario.usuarioId, usuario.tenantId, 1)}`),
    SessaoExpirada,
  );
});

test("usuário desativado -> UsuarioInativo (403), não 401", async () => {
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
    ativo: false,
  });

  await assert.rejects(
    () => autentica(`Bearer ${token(usuario.usuarioId, usuario.tenantId)}`),
    UsuarioInativo,
  );
});

test("as três conferências de papel", async () => {
  const usuario: UsuarioAutenticado = {
    usuarioId: "u",
    tenantId: "t",
    email: "u@loja.local",
    nome: "U",
    lojas: new Map([
      ["loja-a", "admin"],
      ["loja-b", "gerente"],
    ]),
  };

  assert.equal(exigePapelNaLoja(usuario, "loja-b"), "gerente");
  assert.throws(() => exigePapelNaLoja(usuario, "loja-c"), SemAcessoALoja);
  assert.doesNotThrow(() => exigeAdminNaLoja(usuario, "loja-a", "teste"));
  // Alcançar a loja não é administrá-la: sem esta separação, todo gerente criaria uma
  // conta admin para si mesmo.
  assert.throws(() => exigeAdminNaLoja(usuario, "loja-b", "teste"), SemPermissao);
  assert.deepEqual(lojasAdministradas(usuario), ["loja-a"]);
});
