import { describe, expect, test } from "vitest";
import { FILA_VAZIA, acumulaPagina, removeDaFila } from "../src/telas/paginacao.js";
import { evento, fila } from "./fixturas.js";

// A fila cresce por cima enquanto alguém a percorre — o agente não para de mandar evento
// porque o gerente abriu o app. É por isso que a API pagina por cursor e não por OFFSET
// (§4.5), e é por isso que juntar páginas no cliente tem caso de canto.

function comId(id: string) {
  return evento({ event_id: id });
}

describe("acumulação das páginas da fila", () => {
  test("a segunda página entra depois da primeira, na ordem", () => {
    // A ordem é do mais recente para o mais antigo, e ela é informação: um alerta de
    // agora ainda é acionável, um de ontem virou estatística. Embaralhar as páginas
    // desfaria isso sem erro nenhum.
    const primeira = acumulaPagina(FILA_VAZIA, fila([comId("a"), comId("b")], "cursor-1"));
    const segunda = acumulaPagina(primeira, fila([comId("c")], null));

    expect(segunda.eventos.map((e) => e.event_id)).toEqual(["a", "b", "c"]);
  });

  test("evento repetido entre páginas aparece uma vez só", () => {
    // O caso real: a fila recarrega do topo depois de uma decisão, e a página seguinte é
    // pedida com um cursor de antes. A mesma ocorrência duas vezes na tela faz o gerente
    // achar que houve dois furtos.
    const primeira = acumulaPagina(FILA_VAZIA, fila([comId("a"), comId("b")], "cursor-1"));
    const segunda = acumulaPagina(primeira, fila([comId("b"), comId("c")], null));

    expect(segunda.eventos.map((e) => e.event_id)).toEqual(["a", "b", "c"]);
  });

  test("cursor nulo encerra, e cursor novo continua", () => {
    // É o cursor, e não a contagem de itens, que diz se acabou: uma página cheia pode ser
    // a última, e uma página curta pode ter continuação.
    const meio = acumulaPagina(FILA_VAZIA, fila([comId("a")], "cursor-1"));
    expect(meio.proximoCursor).toBe("cursor-1");

    const fim = acumulaPagina(meio, fila([comId("b")], null));
    expect(fim.proximoCursor).toBeNull();
  });

  test("página vazia não apaga o que já está na tela", () => {
    // Uma página vazia com cursor nulo é o fim da fila, não uma fila vazia. Zerar aqui
    // faria a lista sumir na frente de quem estava lendo.
    const cheia = acumulaPagina(FILA_VAZIA, fila([comId("a"), comId("b")], "cursor-1"));
    const depois = acumulaPagina(cheia, fila([], null));

    expect(depois.eventos.map((e) => e.event_id)).toEqual(["a", "b"]);
  });

  test("acumular não muda o estado anterior", () => {
    // O React compara referência para decidir o que redesenhar: mutar o array anterior
    // faz a tela não atualizar, e o bug se parece com "a API não respondeu".
    const antes = acumulaPagina(FILA_VAZIA, fila([comId("a")], "cursor-1"));
    const depois = acumulaPagina(antes, fila([comId("b")], null));

    expect(antes.eventos.map((e) => e.event_id)).toEqual(["a"]);
    expect(depois.eventos).not.toBe(antes.eventos);
  });

  test("o evento decidido sai da fila na hora", () => {
    // A fila mostra o que **falta** decidir. Quem está triando trinta alertas precisa ver
    // a pilha diminuir; esperar a recarga faria o mesmo evento continuar ali, convidando
    // a uma segunda decisão sobre o que já foi decidido.
    const cheia = acumulaPagina(FILA_VAZIA, fila([comId("a"), comId("b")], "cursor-1"));
    const depois = removeDaFila(cheia, "a");

    expect(depois.eventos.map((e) => e.event_id)).toEqual(["b"]);
    expect(depois.proximoCursor).toBe("cursor-1");
  });
});
