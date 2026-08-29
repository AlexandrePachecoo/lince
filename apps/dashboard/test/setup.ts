import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";

// jsdom não implementa a reprodução de mídia: `play()` não existe no HTMLMediaElement
// dele. Sem isto, montar a tela do evento estoura antes de qualquer asserção -- e o que
// se quer testar é justamente que o vídeo é montado para tocar sozinho (NFR-9).
beforeEach(() => {
  if (typeof HTMLMediaElement !== "undefined" && !HTMLMediaElement.prototype.play) {
    HTMLMediaElement.prototype.play = () => Promise.resolve();
  }
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});
