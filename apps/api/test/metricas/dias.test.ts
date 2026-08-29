import assert from "node:assert/strict";
import { test } from "node:test";
import { diaLocal, fusoValido, inicioDoDiaUtc, janelaDeDias } from "../../src/metricas/dias.js";

// O corte do dia decide a que dia um alerta pertence, e a NFR-2 é um teto **diário**.
// Errar aqui não estoura nada: a métrica sai um pouco deslocada, e uma câmera que passou
// do limite numa noite aparece dentro dele nos dois dias vizinhos. É o erro para o lado
// que esconde o problema (R-1), e por isso esta aritmética tem teste próprio.

const SP = "America/Sao_Paulo";

test("a noite de uma loja brasileira não é partida em dois dias", () => {
  // O caso que motiva a coluna de fuso inteira. 22h de 29/08 em São Paulo é 01h de 30/08
  // em UTC: contando pelo dia UTC, o movimento da noite de sexta cairia metade no sábado.
  const vinteEDuasHoras = new Date("2026-08-30T01:00:00.000Z");

  assert.equal(diaLocal(vinteEDuasHoras, SP), "2026-08-29");
  assert.equal(diaLocal(vinteEDuasHoras, "UTC"), "2026-08-30", "e é isso que o UTC diria");
});

test("a meia-noite da loja é 03:00 UTC, e não 00:00", () => {
  // São Paulo é UTC-3 desde o fim do horário de verão. A janela da consulta é montada com
  // este instante; errá-lo desloca o período inteiro em três horas.
  assert.equal(inicioDoDiaUtc("2026-08-29", SP).toISOString(), "2026-08-29T03:00:00.000Z");
  assert.equal(inicioDoDiaUtc("2026-08-29", "UTC").toISOString(), "2026-08-29T00:00:00.000Z");
});

test("ida e volta: o início do dia local pertence ao dia local", () => {
  // A propriedade que sustenta a consulta. Se ela falhar em algum fuso, a primeira hora
  // de cada dia cai no dia anterior e a métrica erra em silêncio.
  for (const fuso of [SP, "UTC", "America/Manaus", "Europe/Lisbon", "Asia/Tokyo"]) {
    for (const dia of ["2026-01-01", "2026-06-15", "2026-08-29", "2026-12-31"]) {
      assert.equal(diaLocal(inicioDoDiaUtc(dia, fuso), fuso), dia, `${fuso} ${dia}`);
    }
  }
});

test("o último milissegundo do dia ainda está dentro da janela", () => {
  // `ate` é exclusivo e aponta para a meia-noite seguinte. Um limite inclusivo escrito
  // como 23:59:59.999 deixaria de fora o evento gravado no milissegundo final -- ele
  // sumiria da métrica sem deixar rastro, que é a pior forma de errar aqui.
  const janela = janelaDeDias(1, SP, new Date("2026-08-29T15:00:00.000Z"));
  const ultimoInstante = new Date(janela.ate.getTime() - 1);

  assert.equal(diaLocal(ultimoInstante, SP), "2026-08-29");
  assert.ok(ultimoInstante >= janela.desde && ultimoInstante < janela.ate);
});

test("sete dias são sete dias, e terminam hoje", () => {
  const janela = janelaDeDias(7, SP, new Date("2026-08-29T15:00:00.000Z"));

  assert.equal(janela.diaFinal, "2026-08-29", "hoje entra, ainda que incompleto");
  assert.equal(janela.diaInicial, "2026-08-23");
  assert.equal(janela.dias, 7);
  // Sete dias de 24 h em São Paulo, que não tem mais horário de verão.
  assert.equal(janela.ate.getTime() - janela.desde.getTime(), 7 * 86_400_000);
});

test("um dia é só o dia de hoje", () => {
  const janela = janelaDeDias(1, SP, new Date("2026-08-29T15:00:00.000Z"));

  assert.equal(janela.diaInicial, "2026-08-29");
  assert.equal(janela.diaFinal, "2026-08-29");
  assert.equal(janela.ate.getTime() - janela.desde.getTime(), 86_400_000);
});

test("a virada do mês e do ano andam pelo calendário, não por aritmética", () => {
  const virada = janelaDeDias(3, SP, new Date("2026-01-02T15:00:00.000Z"));

  assert.equal(virada.diaFinal, "2026-01-02");
  assert.equal(virada.diaInicial, "2025-12-31");
});

test("num fuso com horário de verão o período continua tendo o número certo de dias", () => {
  // O Brasil não tem mais horário de verão, mas a decisão foi política e pode voltar --
  // e o código não pode depender disso. Num dia de transição o dia local tem 23 ou 25 h,
  // então andar em múltiplos de 24 h a partir da meia-noite cai no dia vizinho e o
  // período sai com um dia a mais ou a menos, sem erro nenhum.
  //
  // Santiago volta do horário de verão em 05/04/2026 (o dia tem 25 h).
  const fuso = "America/Santiago";
  const janela = janelaDeDias(7, fuso, new Date("2026-04-08T15:00:00.000Z"));

  assert.equal(janela.diaFinal, "2026-04-08");
  assert.equal(janela.diaInicial, "2026-04-02", "sete dias no calendário, não 7×24 h");
  assert.equal(diaLocal(janela.desde, fuso), "2026-04-02");
  assert.equal(diaLocal(new Date(janela.ate.getTime() - 1), fuso), "2026-04-08");
  // E a janela em horas **não** é 7×24: é 169 h, porque um dos dias teve 25.
  assert.notEqual(janela.ate.getTime() - janela.desde.getTime(), 7 * 86_400_000);
});

test("fuso desconhecido é reconhecido como desconhecido", () => {
  // O valor vem do cadastro. Prosseguir com um fuso inválido faria o Postgres estourar
  // dentro da consulta, longe de onde o erro nasceu -- ou, pior, devolver dia deslocado.
  assert.equal(fusoValido(SP), true);
  assert.equal(fusoValido("UTC"), true);
  assert.equal(fusoValido("America/Nao_Existe"), false);
  assert.equal(fusoValido(""), false);
});
