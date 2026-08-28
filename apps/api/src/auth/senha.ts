import { randomBytes, scrypt, timingSafeEqual } from "node:crypto";

// Hash de senha humana. Deliberadamente **outra coisa** do hashToken de
// agent-token.ts, e a diferença não é preciosismo:
//
// - Token de agente são 256 bits de randomBytes. Não há dicionário que o alcance, e
//   SHA-256 puro basta -- o que se quer ali é só não guardar o segredo em claro.
// - Senha de gente é escolhida por gente. Um vazamento do banco com SHA-256 vira uma
//   lista de senhas em claro na mesma tarde, porque a GPU testa bilhões por segundo.
//
// scrypt do node:crypto, e não argon2/bcrypt, por dois motivos: é KDF de memória dura
// (a GPU perde a vantagem porque precisa de RAM por tentativa, não de núcleos), e vem
// na biblioteca padrão. As seis dependências da API custam auditoria; uma dependência
// nativa a mais para ganhar argon2id não paga o preço nesta fatia. Se pagar um dia, o
// formato abaixo já carrega o algoritmo no próprio hash e admite os dois convivendo.

// 128 * N * r bytes por tentativa = 32 MiB. maxmem é passado explícito porque o padrão
// do Node é exatamente 32 MiB e a conta inclui uma folga de 128 * p * r por cima --
// deixá-lo implícito faz o hash estourar "Invalid scrypt params" só em produção.
const N = 32768;
const R = 8;
const P = 1;
const MAXMEM = 64 * 1024 * 1024;
const TAMANHO_HASH = 32;
const TAMANHO_SAL = 16;

const ALGORITMO = "scrypt";
const SEPARADOR = "$";

function deriva(senha: string, sal: Buffer, n: number, r: number, p: number): Promise<Buffer> {
  return new Promise((resolve, rejeita) => {
    scrypt(
      senha.normalize("NFKC"),
      sal,
      TAMANHO_HASH,
      { N: n, r, p, maxmem: MAXMEM },
      (erro, chave) => {
        if (erro) {
          rejeita(erro);
          return;
        }
        resolve(chave);
      },
    );
  });
}

/**
 * Hash no formato `scrypt$N$r$p$sal$hash` (sal e hash em base64url).
 *
 * Os parâmetros viajam junto com o hash, e não numa constante do código: quando o
 * custo subir, as senhas antigas continuam verificáveis com o custo com que foram
 * criadas em vez de todo mundo ser trancado para fora numa implantação.
 */
export async function geraHashSenha(senha: string): Promise<string> {
  const sal = randomBytes(TAMANHO_SAL);
  const hash = await deriva(senha, sal, N, R, P);
  return [ALGORITMO, N, R, P, sal.toString("base64url"), hash.toString("base64url")].join(
    SEPARADOR,
  );
}

/**
 * Confere a senha contra o hash guardado. Devolve false para hash malformado em vez de
 * estourar: uma linha corrompida no banco tem que virar "senha não confere" para um
 * usuário, nunca 500 na rota de login para todo mundo.
 */
export async function confereSenha(senha: string, guardado: string): Promise<boolean> {
  const partes = guardado.split(SEPARADOR);
  if (partes.length !== 6 || partes[0] !== ALGORITMO) {
    return false;
  }
  const n = Number(partes[1]);
  const r = Number(partes[2]);
  const p = Number(partes[3]);
  if (!Number.isInteger(n) || !Number.isInteger(r) || !Number.isInteger(p)) {
    return false;
  }
  const sal = Buffer.from(partes[4] ?? "", "base64url");
  const esperado = Buffer.from(partes[5] ?? "", "base64url");
  if (sal.length === 0 || esperado.length === 0) {
    return false;
  }

  const obtido = await deriva(senha, sal, n, r, p);
  // timingSafeEqual exige o mesmo tamanho e estoura se diferirem -- o que já é um
  // "não confere", mas não pode virar exceção.
  if (obtido.length !== esperado.length) {
    return false;
  }
  return timingSafeEqual(obtido, esperado);
}

// Hash de descarte, com os mesmos parâmetros de um hash real. Serve ao login quando o
// e-mail não existe: sem ele, "e-mail desconhecido" responderia em microssegundos e
// "senha errada" em ~100 ms, e a diferença enumeraria os e-mails cadastrados com um
// cronômetro. Ver routes/auth/login.ts.
let hashDeDescarte: string | null = null;

export async function gastaTempoDeSenha(senha: string): Promise<void> {
  if (hashDeDescarte === null) {
    hashDeDescarte = await geraHashSenha(randomBytes(32).toString("hex"));
  }
  await confereSenha(senha, hashDeDescarte);
}
