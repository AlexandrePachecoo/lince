import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { hashToken } from "../../src/auth/agent-token.js";
import { TTL_HORAS } from "../../src/auth/bootstrap-token.js";
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

function tokenDe(usuario: UsuarioSemeado): string {
  return emiteToken(SEGREDO_TESTE, {
    usuarioId: usuario.usuarioId,
    tenantId: usuario.tenantId,
    tokenVersao: 0,
    expiraEmS: 3600,
  });
}

function gera(credencial: string, lojaId: string) {
  return app.inject({
    method: "POST",
    url: `/v1/lojas/${lojaId}/bootstrap-token`,
    headers: { authorization: `Bearer ${credencial}` },
  });
}

function registra(bootstrapToken: string) {
  return app.inject({
    method: "POST",
    url: "/v1/agents/register",
    payload: { schema_version: 1, bootstrap_token: bootstrapToken },
  });
}

async function lojaComAdmin() {
  const loja = await semeiaLoja();
  const admin = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "admin" } });
  return { loja, admin };
}

test("o token gerado pelo admin vira credencial de agente da mesma loja", async () => {
  // O ciclo inteiro do provisionamento (§5.1), pelas duas rotas de verdade. Se as duas
  // discordarem sobre o que é um token de bootstrap -- hash, prefixo, validade --, o
  // sintoma é o instalador na loja com um token que a tela acabou de mostrar e que a
  // nuvem recusa.
  const { loja, admin } = await lojaComAdmin();

  const resposta = await gera(tokenDe(admin), loja.lojaId);

  assert.equal(resposta.statusCode, 201);
  const corpo = resposta.json();
  assert.equal(corpo.store_id, loja.lojaId);

  const registro = await registra(corpo.bootstrap_token);
  assert.equal(registro.statusCode, 201);
  assert.equal(registro.json().store_id, loja.lojaId);
  assert.equal(registro.json().tenant_id, loja.tenantId);
});

test("a validade na resposta é a que está gravada, TTL_HORAS à frente", async () => {
  // Quem lê `expires_at` é uma pessoa decidindo se instala o box hoje ou amanhã. Uma
  // validade na tela diferente da do banco faz ela chegar à loja com um token vencido.
  const { loja, admin } = await lojaComAdmin();
  const antes = Date.now();

  const corpo = (await gera(tokenDe(admin), loja.lojaId)).json();

  const linha = await prismaTeste.tokenBootstrap.findUniqueOrThrow({
    where: { tokenHash: hashToken(corpo.bootstrap_token) },
  });
  assert.equal(corpo.expires_at, linha.expiraEm.toISOString());
  const ttlMs = TTL_HORAS * 60 * 60 * 1000;
  assert.ok(linha.expiraEm.getTime() >= antes + ttlMs);
  assert.ok(linha.expiraEm.getTime() <= Date.now() + ttlMs);
});

test("a linha do token guarda quem o gerou, e nunca o token em claro", async () => {
  // "Quem pôs um box nesta loja" só tem resposta se o autor ficar na linha. E o token é
  // credencial em potencial: em claro no banco, um dump de backup instalaria um box.
  const { loja, admin } = await lojaComAdmin();

  const corpo = (await gera(tokenDe(admin), loja.lojaId)).json();

  const linhas = await prismaTeste.tokenBootstrap.findMany({ where: { lojaId: loja.lojaId } });
  assert.equal(linhas.length, 1);
  assert.equal(linhas[0]?.criadoPorId, admin.usuarioId);
  assert.equal(linhas[0]?.criadoPorEmail, admin.email);
  assert.ok(!JSON.stringify(linhas).includes(corpo.bootstrap_token));
});

test("a resposta não pode ser guardada por proxy nem pelo navegador", async () => {
  // O corpo é um segredo que só existe nesta resposta; uma cópia em cache é uma
  // credencial de loja que ninguém sabe que existe.
  const { loja, admin } = await lojaComAdmin();

  const resposta = await gera(tokenDe(admin), loja.lojaId);

  assert.equal(resposta.headers["cache-control"], "no-store");
});

test("gerar outro token vence o anterior que ainda não foi usado", async () => {
  // Quem pede o segundo perdeu o primeiro. Se o primeiro continuasse valendo, seria uma
  // credencial de loja solta por até 24 h sem ninguém vigiando. E a recusa é "expirado",
  // não "não reconhecido": a primeira manda gerar outro, a segunda manda conferir a cópia.
  const { loja, admin } = await lojaComAdmin();
  const primeiro = (await gera(tokenDe(admin), loja.lojaId)).json().bootstrap_token;
  const segundo = (await gera(tokenDe(admin), loja.lojaId)).json().bootstrap_token;

  const comOPrimeiro = await registra(primeiro);
  const comOSegundo = await registra(segundo);

  assert.equal(comOPrimeiro.statusCode, 401);
  assert.match(comOPrimeiro.json().erro, /expirado/);
  assert.equal(comOSegundo.statusCode, 201);
});

test("gerar outro token não mexe no que já virou agente nem no de outra loja", async () => {
  // A linha usada é o registro de qual token virou qual agente; reescrevê-la apagaria a
  // história do provisionamento. E o token de outra loja é de outra instalação em curso.
  const { loja, admin } = await lojaComAdmin();
  const usado = (await gera(tokenDe(admin), loja.lojaId)).json().bootstrap_token;
  await registra(usado);
  const antes = await prismaTeste.tokenBootstrap.findUniqueOrThrow({
    where: { tokenHash: hashToken(usado) },
  });

  const vizinha = await semeiaLoja({ tenantId: loja.tenantId });
  const adminVizinho = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [vizinha.lojaId]: "admin" },
  });
  const daVizinha = (await gera(tokenDe(adminVizinho), vizinha.lojaId)).json().bootstrap_token;

  await gera(tokenDe(admin), loja.lojaId);

  const depois = await prismaTeste.tokenBootstrap.findUniqueOrThrow({
    where: { tokenHash: hashToken(usado) },
  });
  assert.equal(depois.expiraEm.getTime(), antes.expiraEm.getTime());
  assert.equal((await registra(daVizinha)).statusCode, 201);
});

test("gerente e operador da loja não geram token -> 403, e nada é gravado", async () => {
  // O token vira um box, e o box puxa a configuração com as senhas RTSP das câmeras. É
  // o degrau de quem administra a loja, não de quem tria a fila.
  const loja = await semeiaLoja();
  for (const papel of ["gerente", "operador"] as const) {
    const usuario = await semeiaUsuario({
      tenantId: loja.tenantId,
      lojas: { [loja.lojaId]: papel },
    });

    const resposta = await gera(tokenDe(usuario), loja.lojaId);

    assert.equal(resposta.statusCode, 403, papel);
  }
  assert.equal(await prismaTeste.tokenBootstrap.count(), 0);
});

test("admin de uma loja não gera token para outra loja da mesma rede -> 403", async () => {
  // `admin` é papel de loja, não de tenant: o admin da loja A não põe box na loja B.
  const { loja, admin } = await lojaComAdmin();
  const outra = await semeiaLoja({ tenantId: loja.tenantId });

  const resposta = await gera(tokenDe(admin), outra.lojaId);

  assert.equal(resposta.statusCode, 403);
  assert.equal(await prismaTeste.tokenBootstrap.count(), 0);
});

test("loja de outra rede responde igual a loja que não existe -> 404", async () => {
  // Dentro do tenant a recusa é 403; fora dele, 404. Um 403 aqui confirmaria a quem
  // está em outra rede que aquele id de loja existe.
  const { admin } = await lojaComAdmin();
  const alheia = await semeiaLoja();

  const daAlheia = await gera(tokenDe(admin), alheia.lojaId);
  const inexistente = await gera(tokenDe(admin), "loja-que-nao-existe");

  assert.equal(daAlheia.statusCode, 404);
  assert.equal(inexistente.statusCode, 404);
  assert.equal(await prismaTeste.tokenBootstrap.count(), 0);
});

test("a credencial do agente não gera token de bootstrap -> 401", async () => {
  // Um box comprometido que emitisse token fabricaria agentes novos para a própria loja,
  // e desativá-lo deixaria de encerrar o incidente. As duas credenciais leem o mesmo
  // cabeçalho e não se cruzam.
  const loja = await semeiaLoja();

  const resposta = await gera(loja.token, loja.lojaId);

  assert.equal(resposta.statusCode, 401);
  assert.equal(await prismaTeste.tokenBootstrap.count(), 0);
});

test("sem sessão -> 401", async () => {
  const loja = await semeiaLoja();

  const resposta = await app.inject({
    method: "POST",
    url: `/v1/lojas/${loja.lojaId}/bootstrap-token`,
  });

  assert.equal(resposta.statusCode, 401);
});
