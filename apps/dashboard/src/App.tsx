import { useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { type SessaoGuardada, leSessao, limpaSessao } from "./auth/sessao.js";
import { ambienteReal, desativaAvisos } from "./push/avisos.js";
import { Evento } from "./telas/Evento.js";
import { Fila } from "./telas/Fila.js";
import { Login } from "./telas/Login.js";
import { Metrica } from "./telas/Metrica.js";

// O esqueleto: sessão, rotas e a queda para o login.
//
// A rota do evento é `/eventos/:eventId` e não um estado dentro da fila, de propósito: é
// isso que faz um F5 no meio da triagem voltar ao mesmo evento em vez de ao topo da lista
// — e é o que dá endereço à notificação (§4.5): o toque nela abre `/eventos/:eventId`. É o par, no cliente,
// do GET /v1/events/{event_id} que a API passou a ter.

export function App() {
  const [sessao, setSessao] = useState<SessaoGuardada | null>(() => leSessao());
  // Sobe a cada decisão gravada: a fila é a lista do que **falta** decidir, então o que
  // acabou de sair dela não pode continuar na tela.
  const [versaoDaFila, setVersaoDaFila] = useState(0);

  const navega = useNavigate();
  const local = useLocation();

  const sai = useCallback(() => {
    // O aparelho sai da lista de quem recebe aviso junto com a sessão: o celular que passa
    // de mão em mão não pode seguir tocando com os alertas de quem saiu (R-9). Sem
    // esperar: sair não pode ficar preso a uma rede ruim, e a desinscrição no navegador
    // acontece mesmo se a API não responder (push/avisos.ts).
    const token = leSessao()?.token;
    if (token !== undefined) {
      void desativaAvisos(token, ambienteReal()).catch(() => undefined);
    }
    limpaSessao();
    setSessao(null);
    navega("/login");
  }, [navega]);

  // Uma sessão que vence com o app aberto (12 h é um turno) tem que cair sozinha, senão a
  // próxima ação do gerente é um 401 no meio de uma decisão.
  useEffect(() => {
    if (sessao === null) {
      return;
    }
    const timer = setTimeout(sai, Math.max(0, sessao.expiraEm - Date.now()));
    return () => clearTimeout(timer);
  }, [sessao, sai]);

  if (sessao === null) {
    return (
      <Routes>
        <Route
          path="/login"
          element={
            <Login
              aoEntrar={(nova) => {
                setSessao(nova);
                navega("/fila");
              }}
            />
          }
        />
        {/* Guarda o endereço pedido: quem tocou na notificação e caiu no login volta para
            o evento, e não para o topo da fila. */}
        <Route path="*" element={<Navigate to="/login" replace state={{ de: local.pathname }} />} />
      </Routes>
    );
  }

  return (
    <Routes>
      <Route
        path="/fila"
        element={
          <Fila
            token={sessao.token}
            nome={sessao.usuario.nome}
            aoSair={sai}
            versao={versaoDaFila}
          />
        }
      />
      <Route
        path="/eventos/:eventId"
        element={<Evento token={sessao.token} aoDecidir={() => setVersaoDaFila((v) => v + 1)} />}
      />
      <Route path="/metrica" element={<Metrica token={sessao.token} />} />
      <Route path="*" element={<Navigate to="/fila" replace />} />
    </Routes>
  );
}
