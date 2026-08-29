import type { Sessao, Usuario } from "../api/tipos.js";

// A sessão do gerente, do lado do navegador.
//
// Fica em `localStorage` e não em memória por uma razão de uso, não de conforto: o
// celular no bolso descarrega a aba, e voltar ao app no meio de um turno não pode pedir
// e-mail e senha de novo, em pé, num corredor. Não há refresh token nesta versão (§4.5),
// então o que se guarda é o token e a hora em que ele deixa de valer.
//
// O token é credencial: fica só aqui e no cabeçalho. Nunca em URL, nunca em log.

const CHAVE = "lince.sessao";

export interface SessaoGuardada {
  token: string;
  /** Epoch em ms. Guardado absoluto, e não como `expires_in_s`: um "faltam 12 h" salvo
   *  no disco vira uma sessão eterna, porque nada desconta o tempo que o app passou
   *  fechado. */
  expiraEm: number;
  usuario: Usuario;
}

export function guardaSessao(resposta: Sessao, agora: number = Date.now()): SessaoGuardada {
  const sessao: SessaoGuardada = {
    token: resposta.token,
    expiraEm: agora + resposta.expires_in_s * 1000,
    usuario: resposta.usuario,
  };
  localStorage.setItem(CHAVE, JSON.stringify(sessao));
  return sessao;
}

export function leSessao(agora: number = Date.now()): SessaoGuardada | null {
  const cru = localStorage.getItem(CHAVE);
  if (cru === null) {
    return null;
  }

  let sessao: SessaoGuardada;
  try {
    sessao = JSON.parse(cru) as SessaoGuardada;
  } catch {
    // Conteúdo corrompido (versão antiga do app, storage editado à mão). Descartar é o
    // certo: insistir num JSON quebrado deixaria o app numa tela de erro da qual só se
    // sai limpando o navegador, e o gerente não vai fazer isso no corredor.
    limpaSessao();
    return null;
  }

  if (typeof sessao?.token !== "string" || typeof sessao?.expiraEm !== "number") {
    limpaSessao();
    return null;
  }

  // Vencida é o mesmo que ausente. Mandar o token vencido para a API só trocaria esta
  // decisão por um 401 mais tarde -- e, no meio de uma triagem, um round-trip a mais é a
  // diferença entre a tela abrir e a tela piscar.
  if (sessao.expiraEm <= agora) {
    limpaSessao();
    return null;
  }

  return sessao;
}

export function limpaSessao(): void {
  localStorage.removeItem(CHAVE);
}
