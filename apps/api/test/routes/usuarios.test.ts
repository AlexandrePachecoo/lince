import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { emiteToken } from "../../src/auth/jwt.js";
import { SEGREDO_TESTE, buildTestApp } from "../helpers/build-app.js";
import {
  type UsuarioSemeado,
  limpaBanco,
  prismaTeste,
  semeiaLoja,
  semeiaUsuario,
} from "../helpers/test-db.js";

let app: FastifyInstance;

before(async () => {
  app = await buildTestApp();
});
after(async () => {
  await app.close();
});
beforeEach(async () => {
  await limpaBanco();
});

const SENHA_NOVA = "uma senha longa o bastante";

function tokenDe(usuario: UsuarioSemeado): string {
  return emiteToken(SEGREDO_TESTE, {
    usuarioId: usuario.usuarioId,
    tenantId: usuario.tenantId,
    tokenVersao: 0,
    expiraEmS: 3600,
  });
}

function cria(token: string, corpo: unknown) {
  return app.inject({
    method: "POST",
    url: "/v1/usuarios",
    headers: { authorization: `Bearer ${token}` },
    payload: corpo as object,
  });
}

function lista(token: string) {
  return app.inject({
    method: "GET",
    url: "/v1/usuarios",
    headers: { authorization: `Bearer ${token}` },
  });
}

function altera(token: string, usuarioId: string, corpo: unknown) {
  return app.inject({
    method: "PATCH",
    url: `/v1/usuarios/${usuarioId}`,
    headers: { authorization: `Bearer ${token}` },
    payload: corpo as object,
  });
}

test("admin da loja cria usuário -> 201, no tenant de quem criou", async () => {
  // O tenant não vem no corpo, de propósito: um tenant_id vindo do cliente seria a forma
  // mais direta de furar o NFR-6, e teria um uso legítimo aparente que esconderia o abuso.
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });

  const resposta = await cria(tokenDe(admin), {
    email: "Novo.Gerente@Loja.Local ",
    nome: "Novo gerente",
    senha: SENHA_NOVA,
    lojas: [{ store_id: loja.lojaId, papel: "gerente" }],
  });

  assert.equal(resposta.statusCode, 201);
  const usuario = resposta.json().usuario;
  assert.equal(usuario.tenant_id, loja.tenantId);
  // Normalizado no cadastro pela mesma função do login: se divergissem, existiria conta
  // criada com um endereço que nenhum login alcança.
  assert.equal(usuario.email, "novo.gerente@loja.local");
  assert.deepEqual(usuario.lojas, [{ store_id: loja.lojaId, papel: "gerente" }]);
});

test("nenhuma resposta de usuário carrega senha ou hash", async () => {
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });

  const criado = await cria(tokenDe(admin), {
    email: "alguem@loja.local",
    nome: "Alguém",
    senha: SENHA_NOVA,
    lojas: [{ store_id: loja.lojaId, papel: "operador" }],
  });
  const listado = await lista(tokenDe(admin));

  for (const resposta of [criado, listado]) {
    const cru = resposta.rawPayload.toString("utf-8");
    assert.ok(!cru.includes("scrypt$"), "hash de senha na resposta");
    assert.ok(!cru.includes("senhaHash"), "hash de senha na resposta");
    assert.ok(!cru.includes(SENHA_NOVA), "senha em claro na resposta");
  }
});

test("o usuário criado entra de verdade com a senha dada", async () => {
  // Fecha o ciclo: cadastro e login pela mesma normalização e pela mesma rotina de hash.
  // Uma divergência aqui só apareceria como "criei a conta e ela não entra", já em campo.
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });
  await cria(tokenDe(admin), {
    email: "gerente.novo@loja.local",
    nome: "Gerente novo",
    senha: SENHA_NOVA,
    lojas: [{ store_id: loja.lojaId, papel: "gerente" }],
  });

  const login = await app.inject({
    method: "POST",
    url: "/v1/auth/login",
    payload: { email: "gerente.novo@loja.local", senha: SENHA_NOVA },
  });

  assert.equal(login.statusCode, 200);
});

test("gerente não cria usuário -> 403", async () => {
  // Administrar gente é papel de loja, e é `admin`. Sem isso, qualquer gerente criaria
  // uma conta admin para si mesmo e a hierarquia da rede seria decorativa.
  const loja = await semeiaLoja();
  const gerente = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });

  const resposta = await cria(tokenDe(gerente), {
    email: "outro@loja.local",
    nome: "Outro",
    senha: SENHA_NOVA,
    lojas: [{ store_id: loja.lojaId, papel: "admin" }],
  });

  assert.equal(resposta.statusCode, 403);
  assert.equal(await prismaTeste.usuario.count(), 1, "nada pode ter sido criado");
});

test("admin de uma loja não dá acesso a outra que não administra -> 403", async () => {
  // O caso da rede com várias lojas: o admin da loja A não pode criar alguém com acesso
  // à loja B. Se pudesse, bastaria administrar a menor loja da rede para alcançar todas.
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja({ tenantId: lojaA.tenantId });
  const admin = await semeiaUsuario({
    tenantId: lojaA.tenantId,
    lojas: { [lojaA.lojaId]: "admin" },
  });

  const resposta = await cria(tokenDe(admin), {
    email: "espalhado@loja.local",
    nome: "Espalhado",
    senha: SENHA_NOVA,
    lojas: [
      { store_id: lojaA.lojaId, papel: "gerente" },
      { store_id: lojaB.lojaId, papel: "gerente" },
    ],
  });

  assert.equal(resposta.statusCode, 403);
  assert.equal(await prismaTeste.usuarioLoja.count({ where: { lojaId: lojaB.lojaId } }), 0);
});

test("e-mail repetido -> 409, e a unicidade é do banco", async () => {
  // A conferência não é um SELECT antes do INSERT: duas criações simultâneas passariam
  // pelas duas verificações e uma quebraria depois, de um jeito mais feio.
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });
  const corpo = {
    email: "repetido@loja.local",
    nome: "Repetido",
    senha: SENHA_NOVA,
    lojas: [{ store_id: loja.lojaId, papel: "operador" }],
  };

  assert.equal((await cria(tokenDe(admin), corpo)).statusCode, 201);
  assert.equal((await cria(tokenDe(admin), corpo)).statusCode, 409);
});

test("corpo fora do schema -> 400", async () => {
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });
  const token = tokenDe(admin);

  // Senha curta: comprimento é o único requisito que aumenta o trabalho de quem ataca
  // sem aumentar o de quem lembra.
  const curta = await cria(token, {
    email: "a@b.local",
    nome: "A",
    senha: "curta",
    lojas: [{ store_id: loja.lojaId, papel: "operador" }],
  });
  // Sem loja nenhuma: usuário que não enxerga evento é sempre engano de formulário.
  const semLoja = await cria(token, {
    email: "a@b.local",
    nome: "A",
    senha: SENHA_NOVA,
    lojas: [],
  });
  // Loja repetida viraria violação de chave primária composta lá embaixo -- 500 no lugar
  // de um 400 que diz o que consertar.
  const repetida = await cria(token, {
    email: "a@b.local",
    nome: "A",
    senha: SENHA_NOVA,
    lojas: [
      { store_id: loja.lojaId, papel: "gerente" },
      { store_id: loja.lojaId, papel: "admin" },
    ],
  });

  assert.equal(curta.statusCode, 400);
  assert.equal(semLoja.statusCode, 400);
  assert.equal(repetida.statusCode, 400);
});

test("a lista é o que se administra, não o quadro do tenant inteiro", async () => {
  // Listagem mais larga que o poder de edição é vazamento sem contrapartida: o admin da
  // loja A enumeraria o quadro da loja B sem poder mexer em nada lá.
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja({ tenantId: lojaA.tenantId });
  const admin = await semeiaUsuario({
    tenantId: lojaA.tenantId,
    lojas: { [lojaA.lojaId]: "admin" },
  });
  const daLojaA = await semeiaUsuario({
    tenantId: lojaA.tenantId,
    lojas: { [lojaA.lojaId]: "operador" },
  });
  await semeiaUsuario({ tenantId: lojaA.tenantId, lojas: { [lojaB.lojaId]: "gerente" } });

  const corpo = (await lista(tokenDe(admin))).json();

  assert.deepEqual(
    corpo.usuarios.map((u: { id: string }) => u.id).sort(),
    [admin.usuarioId, daLojaA.usuarioId].sort(),
  );
});

test("quem não administra loja nenhuma recebe lista vazia, não 403", async () => {
  // Um gerente que abre a tela de equipe faz uma pergunta legítima ("quem mais tria
  // aqui?"). A resposta honesta é "nada que você administre", não um erro.
  const loja = await semeiaLoja();
  const gerente = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });

  const resposta = await lista(tokenDe(gerente));

  assert.equal(resposta.statusCode, 200);
  assert.deepEqual(resposta.json().usuarios, []);
});

test("PATCH de lojas substitui a lista inteira", async () => {
  // É o formulário de edição, com uma caixa por loja. Um PATCH que só somasse deixaria o
  // admin sem como tirar acesso -- e tirar acesso é a operação urgente.
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja({ tenantId: lojaA.tenantId });
  const admin = await semeiaUsuario({
    tenantId: lojaA.tenantId,
    lojas: { [lojaA.lojaId]: "admin", [lojaB.lojaId]: "admin" },
  });
  const alvo = await semeiaUsuario({
    tenantId: lojaA.tenantId,
    lojas: { [lojaA.lojaId]: "gerente", [lojaB.lojaId]: "gerente" },
  });

  const resposta = await altera(tokenDe(admin), alvo.usuarioId, {
    lojas: [{ store_id: lojaA.lojaId, papel: "operador" }],
  });

  assert.equal(resposta.statusCode, 200);
  assert.deepEqual(resposta.json().usuario.lojas, [{ store_id: lojaA.lojaId, papel: "operador" }]);
});

test("trocar a senha derruba as sessões abertas daquele usuário", async () => {
  // É o que se espera de uma senha trocada porque vazou. Se as sessões antigas
  // sobrevivessem por 12 h, trocá-la não resolveria o problema que motivou a troca.
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });
  const alvo = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const sessaoDoAlvo = tokenDe(alvo);
  assert.equal((await lista(sessaoDoAlvo)).statusCode, 200, "a sessão valia antes");

  await altera(tokenDe(admin), alvo.usuarioId, { senha: "outra senha bem longa" });

  assert.equal((await lista(sessaoDoAlvo)).statusCode, 401);
});

test("não dá para desativar a própria conta -> 403", async () => {
  // O tiro no pé mais fácil de dar: o admin se desativa e ninguém mais administra a
  // loja. Não cobre todo caso de tranca-se-fora, cobre o que acontece de verdade.
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });

  const resposta = await altera(tokenDe(admin), admin.usuarioId, { ativo: false });

  assert.equal(resposta.statusCode, 403);
});

test("admin só de uma das lojas do alvo não mexe nele -> 403", async () => {
  // Renomear ou desativar alguém que também trabalha na loja B é efeito numa loja que o
  // autor não administra, a partir de uma tela em que a loja B nem aparece.
  const lojaA = await semeiaLoja();
  const lojaB = await semeiaLoja({ tenantId: lojaA.tenantId });
  const admin = await semeiaUsuario({
    tenantId: lojaA.tenantId,
    lojas: { [lojaA.lojaId]: "admin" },
  });
  const alvo = await semeiaUsuario({
    tenantId: lojaA.tenantId,
    lojas: { [lojaA.lojaId]: "gerente", [lojaB.lojaId]: "gerente" },
  });

  const resposta = await altera(tokenDe(admin), alvo.usuarioId, { ativo: false });

  assert.equal(resposta.statusCode, 403);
  const depois = await prismaTeste.usuario.findUniqueOrThrow({ where: { id: alvo.usuarioId } });
  assert.equal(depois.ativo, true);
});

test("usuário de outro tenant -> 404 (NFR-6)", async () => {
  // 404 e não 403: confirmar que aquele id tem conta em outra rede de mercados já é
  // vazamento.
  const minha = await semeiaLoja();
  const alheia = await semeiaLoja();
  const admin = await semeiaUsuario({
    tenantId: minha.tenantId,
    lojas: { [minha.lojaId]: "admin" },
  });
  const deOutraRede = await semeiaUsuario({
    tenantId: alheia.tenantId,
    lojas: { [alheia.lojaId]: "gerente" },
  });

  const existente = await altera(tokenDe(admin), deOutraRede.usuarioId, { nome: "Renomeado" });
  const inexistente = await altera(tokenDe(admin), randomUUID(), { nome: "Renomeado" });

  assert.equal(existente.statusCode, 404);
  assert.equal(inexistente.statusCode, 404);
});
