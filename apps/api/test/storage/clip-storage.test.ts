import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { after, test } from "node:test";
import { ArmazenamentoClipes } from "../../src/storage/clip-storage.js";
import { configArmazenamentoTeste } from "../helpers/build-app.js";
import { apagaObjeto, urlCrua } from "../helpers/minio.js";

// MinIO real (infra/docker-compose.yml), não um stub de assinatura. O que importa aqui
// não é o formato da URL -- é o bucket aceitar. Uma assinatura impecável contra um
// escopo errado é indistinguível de uma correta até alguém tentar o PUT.

const armazenamento = new ArmazenamentoClipes(configArmazenamentoTeste());
const criadas: string[] = [];

function chaveNova(): string {
  const chave = `clipes/tenant-teste/2026/08/23/${randomUUID()}.mp4`;
  criadas.push(chave);
  return chave;
}

after(async () => {
  await Promise.all(criadas.map(apagaObjeto));
});

test("a URL de upload aceita o PUT do jeito que o agente faz", async () => {
  // Com Content-Type e Content-Length explícitos, como em outbox/http.py. Os dois
  // ficam FORA dos cabeçalhos assinados de propósito (só `host` entra): se entrassem,
  // qualquer divergência entre o que a API assinou e o que o agente manda viraria um
  // 403 do bucket, que se parece com credencial errada e manda depurar no lugar errado.
  const chave = chaveNova();
  const url = await armazenamento.urlDeUpload(chave, 900);
  const corpo = Buffer.from("bytes que fazem as vezes de um clipe");

  const resposta = await fetch(url, {
    method: "PUT",
    body: corpo,
    headers: { "Content-Type": "video/mp4", "Content-Length": String(corpo.byteLength) },
  });

  assert.equal(resposta.status, 200, await resposta.text());
});

test("a URL de leitura devolve exatamente os bytes que subiram", async () => {
  const chave = chaveNova();
  const corpo = Buffer.from("um clipe curto, mas inteiro");
  await fetch(await armazenamento.urlDeUpload(chave, 900), { method: "PUT", body: corpo });

  const leitura = await fetch(await armazenamento.urlDeLeitura(chave, 300));

  assert.equal(leitura.status, 200);
  assert.equal((await leitura.arrayBuffer()).byteLength, corpo.byteLength);
});

test("sem assinatura o bucket recusa (NFR-7)", async () => {
  // O clipe é dado pessoal (R-9). Um bucket que servisse o objeto a quem souber a
  // chave transformaria o event_id, que viaja em log e em payload, em credencial.
  const chave = chaveNova();
  const corpo = Buffer.from("clipe");
  await fetch(await armazenamento.urlDeUpload(chave, 900), { method: "PUT", body: corpo });

  const resposta = await fetch(urlCrua(chave));

  assert.equal(resposta.status, 403);
});

test("a validade pedida viaja na URL", async () => {
  // X-Amz-Expires é o que faz a URL vencer. Perdê-lo não dá erro nenhum na emissão --
  // a URL funciona, e funciona para sempre, que é justamente o que o NFR-7 proíbe.
  const url = new URL(await armazenamento.urlDeUpload(chaveNova(), 120));
  assert.equal(url.searchParams.get("X-Amz-Expires"), "120");
});

test("só `host` entra nos cabeçalhos assinados", async () => {
  // Ver a primeira asserção deste arquivo: é isto que deixa o agente mandar
  // Content-Type e Content-Length sem combinar valor nenhum com a API.
  const url = new URL(await armazenamento.urlDeUpload(chaveNova(), 900));
  assert.equal(url.searchParams.get("X-Amz-SignedHeaders"), "host");
});

test("a chave entra no caminho sem barra dobrada", async () => {
  // `${endpoint}/` + `/clipes/...` produziria `//clipes/...`, que não é erro de
  // assinatura: vira parte da chave, e o objeto pousa num caminho que ninguém procura.
  const comBarra = new ArmazenamentoClipes({
    ...configArmazenamentoTeste(),
    endpoint: `${configArmazenamentoTeste().endpoint}/`,
  });
  const url = new URL(await comBarra.urlDeUpload("clipes/a/b.mp4", 60));
  assert.ok(!url.pathname.includes("//"), url.pathname);
});

test("validade não positiva é recusada na emissão", async () => {
  // Uma URL com X-Amz-Expires=0 é aceita pelo assinador e recusada pelo bucket. O
  // erro apareceria como upload falhando na loja, e não como configuração errada
  // aqui -- que é onde ele está.
  await assert.rejects(() => armazenamento.urlDeUpload("clipes/a/b.mp4", 0));
});
