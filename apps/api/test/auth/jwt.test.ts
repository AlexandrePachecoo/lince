import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";
import {
  AssinaturaInvalida,
  TokenMalformado,
  TokenVencido,
  emiteToken,
  verificaToken,
} from "../../src/auth/jwt.js";

const SEGREDO = "segredo-de-teste-com-tamanho-suficiente";
const AGORA = Date.parse("2026-08-27T12:00:00.000Z");

function base(dados: object): string {
  return Buffer.from(JSON.stringify(dados), "utf-8").toString("base64url");
}

test("ida e volta preserva os claims", () => {
  const token = emiteToken(SEGREDO, {
    usuarioId: "u-1",
    tenantId: "t-1",
    tokenVersao: 3,
    expiraEmS: 3600,
    agoraMs: AGORA,
  });

  const claims = verificaToken(token, SEGREDO, AGORA);

  assert.equal(claims.sub, "u-1");
  assert.equal(claims.tid, "t-1");
  assert.equal(claims.ver, 3);
  assert.equal(claims.exp, claims.iat + 3600);
});

test("token assinado com outro segredo é recusado", () => {
  // O caso que importa não é o atacante: é o segredo trocado numa implantação sem
  // ninguém avisar. Aceitar aqui significaria aceitar token emitido por qualquer outra
  // instalação -- inclusive a de outro cliente rodando o mesmo código.
  const token = emiteToken("um-outro-segredo-igualmente-longo", {
    usuarioId: "u-1",
    tenantId: "t-1",
    tokenVersao: 0,
    expiraEmS: 3600,
    agoraMs: AGORA,
  });

  assert.throws(() => verificaToken(token, SEGREDO, AGORA), AssinaturaInvalida);
});

test("mexer no payload sem reassinar é recusado", () => {
  // O ataque óbvio: trocar `tid` para o tenant de outra rede e ler a fila de furtos
  // dela. É o NFR-6 valendo antes de qualquer consulta ao banco.
  const token = emiteToken(SEGREDO, {
    usuarioId: "u-1",
    tenantId: "tenant-a",
    tokenVersao: 0,
    expiraEmS: 3600,
    agoraMs: AGORA,
  });
  const [cabecalho, , assinatura] = token.split(".") as [string, string, string];
  const adulterado = [
    cabecalho,
    base({ sub: "u-1", tid: "tenant-b", ver: 0, iat: 1, exp: 9_999_999_999 }),
    assinatura,
  ].join(".");

  assert.throws(() => verificaToken(adulterado, SEGREDO, AGORA), AssinaturaInvalida);
});

test('alg "none" é recusado mesmo com a assinatura vazia', () => {
  // A família inteira de furos de JWT nasce de deixar o cabeçalho do token escolher o
  // algoritmo. Aqui o algoritmo é constante do código e o cabeçalho só é lido DEPOIS de
  // o HMAC fechar -- por isso este token nem chega à conferência de `alg`.
  const semAlg = [base({ alg: "none", typ: "JWT" }), base({ sub: "u-1" }), ""].join(".");

  assert.throws(() => verificaToken(semAlg, SEGREDO, AGORA), AssinaturaInvalida);
});

test("cabeçalho com alg diferente, ainda que assinado com o nosso segredo, é recusado", () => {
  // Assinado corretamente, e ainda assim fora do formato. É token de uma versão futura
  // ou de um emissor confuso; recusar é o lado certo de errar.
  const cabecalho = base({ alg: "HS512", typ: "JWT" });
  const claims = base({ sub: "u-1", tid: "t-1", ver: 0, iat: 1, exp: 9_999_999_999 });
  const corpo = `${cabecalho}.${claims}`;
  const assinatura = createHmac("sha256", SEGREDO).update(corpo).digest("base64url");

  assert.throws(() => verificaToken(`${corpo}.${assinatura}`, SEGREDO, AGORA), TokenMalformado);
});

test("token vencido é TokenVencido, não AssinaturaInvalida", () => {
  // A distinção vira 401 "sessão expirada" contra 401 "token não reconhecido". Os dois
  // mandam para a tela de login, mas só um deles é normal -- e confundi-los faz o log de
  // uma sessão que simplesmente venceu parecer tentativa de adulteração.
  const token = emiteToken(SEGREDO, {
    usuarioId: "u-1",
    tenantId: "t-1",
    tokenVersao: 0,
    expiraEmS: 60,
    agoraMs: AGORA,
  });

  assert.throws(() => verificaToken(token, SEGREDO, AGORA + 61_000), TokenVencido);
  // Sem sleep: quem anda é o relógio passado por parâmetro (CLAUDE.md).
  assert.doesNotThrow(() => verificaToken(token, SEGREDO, AGORA + 59_000));
});

test("lixo no lugar do token não estoura de forma não tratada", () => {
  for (const ruim of ["", "a.b", "a.b.c.d", "....", "x".repeat(5000)]) {
    assert.throws(
      () => verificaToken(ruim, SEGREDO, AGORA),
      (erro: unknown) => erro instanceof TokenMalformado || erro instanceof AssinaturaInvalida,
      `token: ${ruim.slice(0, 20)}`,
    );
  }
});
