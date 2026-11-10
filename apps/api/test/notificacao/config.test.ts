import assert from "node:assert/strict";
import { test } from "node:test";
import webpush from "web-push";
import { carregaEnv } from "../../src/env.js";
import { ORIGENS_PUSH_PADRAO, endpointPermitido } from "../../src/notificacao/config.js";

// A lista de origens é o que impede a API de virar um disparador de requisições para
// onde o cliente quiser: a cada alerta ela faz um POST para o endpoint que o navegador
// mandou. E as chaves VAPID vão as três juntas, porque meio configurado é o caso que
// engana — a API aceitaria inscrição e nada chegaria a celular nenhum.

test("endpoint dos serviços de push dos navegadores é aceito", () => {
  for (const endpoint of [
    "https://fcm.googleapis.com/fcm/send/abc:def",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAA",
    "https://web.push.apple.com/QGx2",
    "https://wns2-par02p.notify.windows.com/w/?token=x",
  ]) {
    assert.equal(endpointPermitido(endpoint, ORIGENS_PUSH_PADRAO), true, endpoint);
  }
});

test("host que só começa igual não passa: a comparação é da origem, não do texto", () => {
  // Um `startsWith` aprovaria os dois primeiros, e o terceiro é o mesmo truque no curinga.
  for (const endpoint of [
    "https://fcm.googleapis.com.atacante.example/x",
    "https://fcm.googleapis.com@atacante.example/x",
    "https://notify.windows.com.atacante.example/x",
  ]) {
    assert.equal(endpointPermitido(endpoint, ORIGENS_PUSH_PADRAO), false, endpoint);
  }
});

test("protocolo, porta e endereço interno não passam", () => {
  for (const endpoint of [
    "http://fcm.googleapis.com/fcm/send/x",
    "https://fcm.googleapis.com:8443/fcm/send/x",
    "http://127.0.0.1:5432/",
    "http://169.254.169.254/latest/meta-data",
    "não é url",
  ]) {
    assert.equal(endpointPermitido(endpoint, ORIGENS_PUSH_PADRAO), false, endpoint);
  }
});

const BASE = {
  DATABASE_URL: "postgresql://x@localhost/x",
  S3_ENDPOINT: "http://localhost:9000",
  S3_REGION: "auto",
  S3_ACCESS_KEY_ID: "a",
  S3_SECRET_ACCESS_KEY: "b",
  S3_BUCKET: "c",
  AUTH_JWT_SECRET: "x".repeat(40),
};

test("sem nenhuma chave VAPID a API sobe com a notificação desligada", () => {
  assert.equal(carregaEnv({ ...BASE }).push, null);
});

test("chave VAPID pela metade derruba a subida, dizendo qual falta", () => {
  assert.throws(
    () => carregaEnv({ ...BASE, VAPID_PUBLIC_KEY: "x", VAPID_SUBJECT: "mailto:a@b.c" }),
    /VAPID_PRIVATE_KEY/,
  );
});

test("VAPID_SUBJECT que não é mailto: nem https: derruba a subida", () => {
  const chaves = webpush.generateVAPIDKeys();
  assert.throws(
    () =>
      carregaEnv({
        ...BASE,
        VAPID_PUBLIC_KEY: chaves.publicKey,
        VAPID_PRIVATE_KEY: chaves.privateKey,
        VAPID_SUBJECT: "suporte@lince.local",
      }),
    /VAPID_SUBJECT/,
  );
});

test("com as três, a notificação liga com os padrões de janela, teto e atraso", () => {
  const chaves = webpush.generateVAPIDKeys();
  const push = carregaEnv({
    ...BASE,
    VAPID_PUBLIC_KEY: chaves.publicKey,
    VAPID_PRIVATE_KEY: chaves.privateKey,
    VAPID_SUBJECT: "mailto:suporte@lince.local",
    ALERTA_TETO_DIARIO: "7",
  }).push;
  assert.ok(push);
  assert.equal(push.vapid.publica, chaves.publicKey);
  assert.equal(push.tetoDiarioPorLoja, 7);
  assert.equal(push.janelaAgrupamentoS, 120);
  assert.deepEqual(push.origensPermitidas, ORIGENS_PUSH_PADRAO);
});
