import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { emiteToken } from "../../src/auth/jwt.js";
import { SEGREDO_TESTE, buildTestApp } from "../helpers/build-app.js";
import { ServicoPushFalso, configPushTeste } from "../helpers/servico-push.js";
import {
  type UsuarioSemeado,
  limpaBanco,
  prismaTeste,
  semeiaLoja,
  semeiaUsuario,
} from "../helpers/test-db.js";

// GET /v1/push/chave e POST/DELETE /v1/push/inscricoes: o aparelho que recebe alerta.
//
// O que se protege aqui: a inscrição é do usuário da sessão e de mais ninguém, o mesmo
// navegador entrando com outra pessoa passa a ser dela, e a API nunca aceita um endpoint
// que não seja de serviço de push — ela faz um POST para ele a cada alerta.

let app: FastifyInstance;
let semPush: FastifyInstance;
let servico: ServicoPushFalso;

before(async () => {
  servico = await ServicoPushFalso.sobe();
  app = await buildTestApp({ push: configPushTeste(servico.origem) });
  semPush = await buildTestApp();
});
after(async () => {
  await app.close();
  await semPush.close();
  await servico.fecha();
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

async function gerente(): Promise<UsuarioSemeado> {
  const loja = await semeiaLoja();
  return semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "gerente" } });
}

function inscreve(alvo: FastifyInstance, token: string, inscricao: unknown) {
  return alvo.inject({
    method: "POST",
    url: "/v1/push/inscricoes",
    headers: { authorization: `Bearer ${token}` },
    payload: { schema_version: 1, inscricao },
  });
}

test("a chave pública sai da API, para a que o dashboard usa ser a do servidor que envia", async () => {
  const token = tokenDe(await gerente());
  const resposta = await app.inject({
    method: "GET",
    url: "/v1/push/chave",
    headers: { authorization: `Bearer ${token}` },
  });
  assert.equal(resposta.statusCode, 200);
  assert.equal(resposta.json().chave_publica, app.configPush?.vapid.publica);
});

test("sem chaves VAPID a chave e a inscrição respondem 503, e não um erro genérico", async () => {
  // O dashboard mostra "notificação indisponível neste servidor" em vez de um botão que
  // parece funcionar e nunca entrega nada.
  const token = tokenDe(await gerente());
  const chave = await semPush.inject({
    method: "GET",
    url: "/v1/push/chave",
    headers: { authorization: `Bearer ${token}` },
  });
  assert.equal(chave.statusCode, 503);
  const inscricao = await inscreve(semPush, token, servico.inscricao("a"));
  assert.equal(inscricao.statusCode, 503);
});

test("sem sessão não há chave nem inscrição", async () => {
  const chave = await app.inject({ method: "GET", url: "/v1/push/chave" });
  assert.equal(chave.statusCode, 401);
  const inscricao = await app.inject({
    method: "POST",
    url: "/v1/push/inscricoes",
    payload: { schema_version: 1, inscricao: servico.inscricao("a") },
  });
  assert.equal(inscricao.statusCode, 401);
});

test("inscrição grava o aparelho preso ao usuário da sessão", async () => {
  const usuario = await gerente();
  const inscricao = servico.inscricao("celular");

  const resposta = await inscreve(app, tokenDe(usuario), inscricao);

  assert.equal(resposta.statusCode, 204, resposta.body);
  const linhas = await prismaTeste.inscricaoPush.findMany();
  assert.equal(linhas.length, 1);
  assert.equal(linhas[0]?.usuarioId, usuario.usuarioId);
  assert.equal(linhas[0]?.endpoint, inscricao.endpoint);
  assert.equal(linhas[0]?.p256dh, inscricao.keys.p256dh);
});

test("endpoint fora dos serviços de push é recusado e nada é gravado", async () => {
  // A API faria um POST para este endereço a cada alerta. Aqui, o metadata da nuvem.
  const inscricao = { ...servico.inscricao("x"), endpoint: "http://169.254.169.254/latest" };

  const resposta = await inscreve(app, tokenDe(await gerente()), inscricao);

  assert.equal(resposta.statusCode, 400);
  assert.equal(await prismaTeste.inscricaoPush.count(), 0);
});

test("corpo fora do contrato -> 400", async () => {
  const inscricao = servico.inscricao("x");
  const resposta = await inscreve(app, tokenDe(await gerente()), {
    endpoint: inscricao.endpoint,
    keys: { p256dh: inscricao.keys.p256dh },
  });
  assert.equal(resposta.statusCode, 400);
});

test("o mesmo navegador entrando com outra pessoa passa a ser dela", async () => {
  // O celular do turno: quem saiu não pode seguir recebendo os alertas de lá.
  const loja = await semeiaLoja();
  const ana = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "gerente" } });
  const bia = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "operador" },
  });
  const inscricao = servico.inscricao("turno");

  await inscreve(app, tokenDe(ana), inscricao);
  await inscreve(app, tokenDe(bia), inscricao);

  const linhas = await prismaTeste.inscricaoPush.findMany();
  assert.equal(linhas.length, 1);
  assert.equal(linhas[0]?.usuarioId, bia.usuarioId);
});

test("a remoção apaga só a inscrição do próprio usuário", async () => {
  const loja = await semeiaLoja();
  const ana = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "gerente" } });
  const bia = await semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "gerente" } });
  const daAna = servico.inscricao("ana");
  await inscreve(app, tokenDe(ana), daAna);

  const remove = (token: string, endpoint: string) =>
    app.inject({
      method: "DELETE",
      url: "/v1/push/inscricoes",
      headers: { authorization: `Bearer ${token}` },
      payload: { schema_version: 1, endpoint },
    });

  // A Bia pedindo para apagar o endpoint da Ana: 204 igual, e nada muda. Responder
  // diferente diria a ela que aquele endpoint existe para outra pessoa.
  assert.equal((await remove(tokenDe(bia), daAna.endpoint)).statusCode, 204);
  assert.equal(await prismaTeste.inscricaoPush.count(), 1);

  assert.equal((await remove(tokenDe(ana), daAna.endpoint)).statusCode, 204);
  assert.equal(await prismaTeste.inscricaoPush.count(), 0);
});
