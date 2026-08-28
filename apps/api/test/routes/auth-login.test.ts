import assert from "node:assert/strict";
import { after, before, beforeEach, test } from "node:test";
import type { FastifyInstance } from "fastify";
import { buildTestApp } from "../helpers/build-app.js";
import {
  SENHA_PADRAO_TESTE,
  type UsuarioSemeado,
  limpaBanco,
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

function login(corpo: unknown) {
  return app.inject({ method: "POST", url: "/v1/auth/login", payload: corpo as object });
}

async function gerenteDeUmaLoja(): Promise<UsuarioSemeado> {
  const loja = await semeiaLoja();
  return semeiaUsuario({ tenantId: loja.tenantId, lojas: { [loja.lojaId]: "gerente" } });
}

test("credenciais certas -> 200 com token e as lojas do usuário", async () => {
  // As lojas vêm no login, e não numa segunda chamada, porque a primeira tela do PWA é a
  // fila de triagem: o gerente abre o app em pé no corredor e não pode olhar um spinner.
  const usuario = await gerenteDeUmaLoja();

  const resposta = await login({ email: usuario.email, senha: usuario.senha });

  assert.equal(resposta.statusCode, 200);
  const corpo = resposta.json();
  assert.ok(corpo.token, "sem token o dashboard não sai da tela de login");
  assert.equal(corpo.expires_in_s, 43200);
  assert.equal(corpo.usuario.email, usuario.email);
  assert.equal(corpo.usuario.lojas.length, 1);
});

test("nenhuma resposta de login carrega senha ou hash", async () => {
  // O hash não é público. Vazado, vira alvo de força bruta offline -- sem rede, sem
  // limite de tentativa e sem ninguém percebendo.
  const usuario = await gerenteDeUmaLoja();

  const resposta = await login({ email: usuario.email, senha: usuario.senha });

  const cru = resposta.rawPayload.toString("utf-8");
  assert.ok(!cru.includes("senhaHash"), "hash de senha no corpo da resposta");
  assert.ok(!cru.includes("scrypt$"), "hash de senha no corpo da resposta");
  assert.ok(!cru.includes(usuario.senha), "senha em claro no corpo da resposta");
});

test("senha errada e e-mail inexistente respondem o mesmo 401", async () => {
  // Duas mensagens diferentes entregariam a lista de quem tem conta a quem chutar
  // endereços -- e o dashboard não faria nada de diferente com a distinção.
  const usuario = await gerenteDeUmaLoja();

  const senhaErrada = await login({ email: usuario.email, senha: "não é a senha" });
  const semConta = await login({ email: "ninguem@lugar-nenhum.local", senha: SENHA_PADRAO_TESTE });

  assert.equal(senhaErrada.statusCode, 401);
  assert.equal(semConta.statusCode, 401);
  assert.deepEqual(senhaErrada.json(), semConta.json());
});

test("e-mail em maiúsculas entra na mesma conta", async () => {
  // O teclado do celular capitaliza a primeira letra sozinho. Sem normalizar, o gerente
  // digita o próprio e-mail e leva "senha inválida" -- e vai procurar o problema na
  // senha, que está certa.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
    email: "ana@loja.local",
  });

  const resposta = await login({ email: "  Ana@Loja.Local  ", senha: usuario.senha });

  assert.equal(resposta.statusCode, 200);
});

test("usuário desativado -> 403, e não 401", async () => {
  // Entrar de novo resolve um 401 e não resolve isto. Quem saiu da empresa vira
  // ativo=false (não é apagado, senão a triagem dele perde o autor), e a tela precisa
  // dizer "conta desativada" em vez de mandar tentar a senha outra vez.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
    ativo: false,
  });

  const resposta = await login({ email: usuario.email, senha: usuario.senha });

  assert.equal(resposta.statusCode, 403);
});

test("corpo fora do schema -> 400", async () => {
  const semSenha = await login({ email: "alguem@loja.local" });
  const emailQualquer = await login({ email: "não é e-mail", senha: "12345678" });

  assert.equal(semSenha.statusCode, 400);
  assert.equal(emailQualquer.statusCode, 400);
});

test("token de agente não abre rota de dashboard, e vice-versa", async () => {
  // As duas credenciais viajam no mesmo cabeçalho Bearer e não se cruzam em lugar
  // nenhum. Importa porque o token do agente vive num box no estoque de um mercado: se
  // ele abrisse a fila, o clipe de qualquer evento estaria a um roubo de caixa de
  // distância.
  const loja = await semeiaLoja();
  const usuario = await semeiaUsuario({
    tenantId: loja.tenantId,
    lojas: { [loja.lojaId]: "gerente" },
  });
  const sessao = (await login({ email: usuario.email, senha: usuario.senha })).json();

  const agenteNaFila = await app.inject({
    method: "GET",
    url: "/v1/events",
    headers: { authorization: `Bearer ${loja.token}` },
  });
  const usuarioNoHeartbeat = await app.inject({
    method: "POST",
    url: "/v1/agents/heartbeat",
    headers: { authorization: `Bearer ${sessao.token}` },
    payload: {
      schema_version: 1,
      agent_version: "0.0.0",
      model_version: null,
      config_version: null,
      queue: { depth: 0, oldest_age_s: 0, bytes: 0 },
      cameras: [],
      uptime_s: 1,
      restarts: 0,
      clock_skew_s: 0,
    },
  });

  assert.equal(agenteNaFila.statusCode, 401, "token de agente não pode ler a fila");
  assert.equal(usuarioNoHeartbeat.statusCode, 401, "token de usuário não pode ser agente");
});
