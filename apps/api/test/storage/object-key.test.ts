import assert from "node:assert/strict";
import { test } from "node:test";
import { chaveDoClipe } from "../../src/storage/object-key.js";

const TENANT = "tenant-dev";
const EVENTO = "11111111-2222-3333-4444-555555555555";

test("a mesma entrada produz sempre a mesma chave", () => {
  // O POST é idempotente e o agente o reposta para renovar uma URL vencida (§5.4).
  // Chave nova a cada emissão órfãria os bytes que já subiram, e o evento passa a
  // apontar para um objeto que ninguém escreveu -- sem erro em lugar nenhum, só um
  // player quebrado na triagem semanas depois.
  const primeira = chaveDoClipe(TENANT, EVENTO, new Date("2026-08-23T14:05:00.000Z"));
  const segunda = chaveDoClipe(TENANT, EVENTO, new Date("2026-08-23T14:05:00.000Z"));
  assert.equal(primeira, segunda);
});

test("a data do prefixo sai de occurred_at, não do instante da chamada", () => {
  // occurred_at não muda no reenvio (event.v1.json); o relógio de quem chama muda. Um
  // evento das 23h59 reenviado às 00h02 do dia seguinte ganharia duas chaves se o
  // prefixo viesse do relógio -- a mesma falha da asserção acima, por um caminho que
  // só aparece uma vez por dia e é impossível de reproduzir de propósito.
  const ocorrido = new Date("2026-08-23T23:59:30.000Z");
  assert.equal(chaveDoClipe(TENANT, EVENTO, ocorrido), `clipes/${TENANT}/2026/08/23/${EVENTO}.mp4`);
});

test("a data do prefixo é UTC, não o fuso de quem roda a API", () => {
  // A borda manda instante UTC e a nuvem pode rodar em qualquer região. Se o prefixo
  // usasse o fuso local, a mesma loja produziria chaves em dias diferentes conforme
  // onde a API subiu -- e uma regra de ciclo de vida por prefixo (R-9) apagaria o dia
  // errado.
  const ocorrido = new Date("2026-08-24T02:30:00.000Z");
  assert.match(chaveDoClipe(TENANT, EVENTO, ocorrido), /^clipes\/tenant-dev\/2026\/08\/24\//);
});

test("a chave carrega tenant_id e event_id, e termina em .mp4", () => {
  // §4.4 exige os dois: o tenant para a regra de ciclo de vida e a auditoria poderem
  // trabalhar por prefixo, o event_id porque é ele que torna o caminho não adivinhável.
  const chave = chaveDoClipe(TENANT, EVENTO, new Date("2026-08-23T14:05:00.000Z"));
  assert.ok(chave.includes(TENANT));
  assert.ok(chave.includes(EVENTO));
  assert.ok(chave.endsWith(".mp4"));
  assert.ok(!chave.startsWith("/"), "chave com barra inicial vira '//' na URL do bucket");
});

test("mês e dia vêm com dois dígitos", () => {
  // `2026/8/3` e `2026/08/03` são prefixos diferentes: um mês sem zero à esquerda
  // partiria o bucket em dois esquemas de nomes que só se notam quando a listagem
  // por prefixo devolve metade dos clipes.
  assert.equal(
    chaveDoClipe(TENANT, EVENTO, new Date("2026-03-08T10:00:00.000Z")),
    `clipes/${TENANT}/2026/03/08/${EVENTO}.mp4`,
  );
});
