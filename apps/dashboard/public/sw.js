// Service worker do dashboard: recebe o Web Push e abre o evento no toque (§4.5).
//
// JavaScript puro, fora do bundle do Vite, de propósito: o service worker precisa morar
// na raiz do site (`/sw.js`) para que o escopo dele seja o app inteiro, e um arquivo com
// hash no nome muda de endereço a cada build — o navegador trataria cada versão como um
// worker novo e as inscrições antigas ficariam órfãs. `test/sw.test.ts` carrega este
// mesmo arquivo num contexto isolado e exercita os eventos.
//
// Não guarda nada em cache. Um app de triagem servido do cache mostraria uma fila velha
// com a cara de fila atual, e o gerente decidiria sobre o que já foi decidido.

// O que um toque pode abrir: só caminhos do próprio app, no formato que a API manda
// (notificacao.v1.json). Qualquer outra coisa vira a fila — um conteúdo adulterado no
// caminho não pode transformar a notificação num link para fora.
const CAMINHO_PERMITIDO = /^\/(eventos\/[A-Za-z0-9-]+|fila)$/;

const PADRAO = {
  titulo: "lince: novo alerta",
  corpo: "Abra para ver a fila de triagem.",
  url: "/fila",
  tag: "lince",
};

/**
 * Lê o conteúdo do push, e cai no padrão em vez de falhar. O navegador exige uma
 * notificação visível para cada push (`userVisibleOnly`), e um push sem notificação faz o
 * Chrome mostrar a dele, genérica — ou, repetido, revogar a inscrição.
 */
function leConteudo(dados) {
  let bruto;
  try {
    bruto = dados ? dados.json() : null;
  } catch {
    bruto = null;
  }
  if (!bruto || typeof bruto !== "object") {
    return PADRAO;
  }
  return {
    titulo: typeof bruto.titulo === "string" && bruto.titulo ? bruto.titulo : PADRAO.titulo,
    corpo: typeof bruto.corpo === "string" ? bruto.corpo : PADRAO.corpo,
    url:
      typeof bruto.url === "string" && CAMINHO_PERMITIDO.test(bruto.url) ? bruto.url : PADRAO.url,
    tag: typeof bruto.tag === "string" && bruto.tag ? bruto.tag : PADRAO.tag,
  };
}

/** Uma janela do app já aberta vai para o evento; sem nenhuma, abre uma. */
async function abre(url) {
  const janelas = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const doApp = janelas.find((janela) => new URL(janela.url).origin === self.location.origin);
  if (doApp) {
    await doApp.focus();
    // `navigate` só existe em janela controlada por este worker. Sem ele, focar já
    // devolve o gerente ao app, e a fila mostra o evento no topo.
    if (typeof doApp.navigate === "function") {
      return doApp.navigate(url);
    }
    return doApp;
  }
  return self.clients.openWindow(url);
}

self.addEventListener("install", () => {
  // A versão nova assume na hora. Um worker velho esperando todas as abas fecharem
  // seguiria mostrando notificação no formato antigo — num celular, aba nenhuma fecha.
  self.skipWaiting();
});

self.addEventListener("activate", (evento) => {
  evento.waitUntil(self.clients.claim());
});

self.addEventListener("push", (evento) => {
  const conteudo = leConteudo(evento.data);
  evento.waitUntil(
    self.registration.showNotification(conteudo.titulo, {
      body: conteudo.corpo,
      // Mesma câmera substitui a anterior na bandeja; `renotify` faz a substituta tocar
      // de novo — sem ele, o segundo furto da mesma câmera chegaria em silêncio.
      tag: conteudo.tag,
      renotify: true,
      icon: "/icone-192.png",
      badge: "/icone-192.png",
      data: { url: conteudo.url },
    }),
  );
});

self.addEventListener("notificationclick", (evento) => {
  evento.notification.close();
  const url = evento.notification.data?.url;
  evento.waitUntil(abre(typeof url === "string" && CAMINHO_PERMITIDO.test(url) ? url : "/fila"));
});
