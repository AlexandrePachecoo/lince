import assert from "node:assert/strict";
import { test } from "node:test";
import { confereSenha, gastaTempoDeSenha, geraHashSenha } from "../../src/auth/senha.js";

// scrypt é lento de propósito (~100 ms por chamada). Poucos casos, cada um pagando o
// custo uma vez só -- é o mesmo motivo pelo qual semeiaUsuario existe no test-db.ts.

test("a senha certa confere e a errada não", async () => {
  const hash = await geraHashSenha("uma senha de gerente");

  assert.equal(await confereSenha("uma senha de gerente", hash), true);
  assert.equal(await confereSenha("uma senha de gerentE", hash), false);
  assert.equal(await confereSenha("", hash), false);
});

test("o mesmo texto gera hashes diferentes (sal por usuário)", async () => {
  // Sem sal por usuário, dois gerentes com a mesma senha teriam o mesmo hash no banco:
  // quem vazasse a tabela quebraria a senha uma vez e entraria nas duas contas -- e
  // ainda saberia, só de olhar, quem repetiu senha com quem.
  const a = await geraHashSenha("senha repetida");
  const b = await geraHashSenha("senha repetida");

  assert.notEqual(a, b, "hashes iguais significam sal fixo ou ausente");
  assert.equal(await confereSenha("senha repetida", a), true);
  assert.equal(await confereSenha("senha repetida", b), true);
});

test("o hash carrega os parâmetros com que foi criado", async () => {
  // Quando o custo do KDF subir, as senhas antigas precisam continuar verificáveis com o
  // custo antigo. Se os parâmetros viessem de uma constante do código, a implantação que
  // sobe o custo tranca todo mundo para fora de uma vez -- e o sintoma é "ninguém
  // consegue entrar", sem erro em lugar nenhum.
  const hash = await geraHashSenha("qualquer");
  const partes = hash.split("$");

  assert.equal(partes[0], "scrypt");
  assert.equal(partes.length, 6, "algoritmo, N, r, p, sal e hash");
  assert.ok(Number.parseInt(partes[1] ?? "0", 10) >= 16384, "N baixo demais não é KDF");
});

test("hash malformado no banco vira 'não confere', não exceção", async () => {
  // Uma linha corrompida (migração manual, restauração parcial) tem que negar o login
  // daquele usuário. Se estourasse, viraria 500 na rota de login -- que é o caminho de
  // todo mundo, e a loja inteira ficaria de fora por causa de uma linha.
  for (const ruim of ["", "scrypt$", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$YQ$YQ", "lixo"]) {
    assert.equal(await confereSenha("qualquer", ruim), false, `hash: ${ruim}`);
  }
});

test("gastaTempoDeSenha não estoura e não aceita nada", async () => {
  // Serve ao login quando o e-mail não existe. Se lançasse, a rota devolveria 500 em vez
  // de 401 -- e a diferença de resposta entre e-mail conhecido e desconhecido voltaria a
  // existir, que é exatamente o que esta função existe para apagar.
  await gastaTempoDeSenha("qualquer coisa");
  await gastaTempoDeSenha("");
});
