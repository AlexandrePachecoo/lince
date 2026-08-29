import { beforeEach, describe, expect, test } from "vitest";
import { guardaSessao, leSessao, limpaSessao } from "../src/auth/sessao.js";
import { sessao } from "./fixturas.js";

// A sessão vive em localStorage porque o celular no bolso descarrega a aba, e voltar ao
// app no meio de um turno não pode pedir e-mail e senha de novo, em pé, num corredor.
// O que se guarda é credencial, e o cuidado é com validade: não há refresh nesta versão.

beforeEach(() => {
  localStorage.clear();
});

describe("sessão guardada no navegador", () => {
  test("a validade é guardada absoluta, não relativa", () => {
    // `expires_in_s` é "faltam 12 h" no instante da resposta. Guardado como está, ele
    // vira uma sessão eterna: nada desconta o tempo que o app passou fechado, e o token
    // vencido continuaria sendo mandado para sempre.
    const agora = 1_700_000_000_000;
    const guardada = guardaSessao(sessao({ expires_in_s: 3600 }), agora);

    expect(guardada.expiraEm).toBe(agora + 3_600_000);
  });

  test("sessão vencida é o mesmo que sessão ausente", () => {
    // Mandar o token vencido só trocaria esta decisão por um 401 mais tarde -- e, no meio
    // de uma triagem, um round-trip a mais é a diferença entre a tela abrir e piscar.
    const agora = 1_700_000_000_000;
    guardaSessao(sessao({ expires_in_s: 60 }), agora);

    expect(leSessao(agora + 59_000)).not.toBeNull();
    expect(leSessao(agora + 61_000)).toBeNull();
  });

  test("sessão vencida também é apagada, não só ignorada", () => {
    // Deixar o token vencido no storage é guardar credencial que não serve para nada --
    // e credencial parada é credencial que vaza numa captura de tela de suporte.
    const agora = 1_700_000_000_000;
    guardaSessao(sessao({ expires_in_s: 60 }), agora);
    leSessao(agora + 61_000);

    expect(localStorage.getItem("lince.sessao")).toBeNull();
  });

  test("conteúdo corrompido não trava o app", () => {
    // Versão antiga do app, storage editado à mão. Insistir num JSON quebrado deixaria a
    // tela de erro da qual só se sai limpando o navegador -- o gerente não vai fazer isso
    // no corredor; ele desinstala o app.
    localStorage.setItem("lince.sessao", "{isto não é json");

    expect(leSessao()).toBeNull();
    expect(localStorage.getItem("lince.sessao")).toBeNull();
  });

  test("JSON válido sem os campos que importam também cai fora", () => {
    // `{}` sobrevive ao JSON.parse e passaria adiante uma sessão sem token, que só
    // falharia depois, como 401 numa tela qualquer.
    localStorage.setItem("lince.sessao", JSON.stringify({ usuario: { nome: "Ana" } }));

    expect(leSessao()).toBeNull();
  });

  test("sair apaga a credencial", () => {
    guardaSessao(sessao());
    limpaSessao();

    expect(leSessao()).toBeNull();
  });
});
