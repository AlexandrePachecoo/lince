import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App.js";
import "./estilo.css";

const raiz = document.getElementById("raiz");
if (raiz === null) {
  throw new Error("elemento #raiz não existe no index.html");
}

createRoot(raiz).render(
  <StrictMode>
    <BrowserRouter>
      <App />
    </BrowserRouter>
  </StrictMode>,
);
