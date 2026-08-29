import { useCallback, useEffect, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { type SessaoGuardada, leSessao, limpaSessao } from "./auth/sessao.js";
import { Evento } from "./telas/Evento.js";
import { Fila } from "./telas/Fila.js";
import { Login } from "./telas/Login.js";
import { Metrica } from "./telas/Metrica.js";

// O esqueleto: sessão, rotas e a queda para o login.
//
// A rota do evento é `/eventos/:eventId` e não um estado dentro da fila, de propósito: é
// isso que faz um F5 no meio da triagem voltar ao mesmo evento em vez de ao topo da lista
// — e é o que dá endereço à notificação quando ela existir (§4.5). É o par, no cliente,
// do GET /v1/events/{event_id} que a API passou a ter.

export function App() {
  const [sessao, setSessao] = useState<SessaoGuardada | null>(() => leSessao());
  // Sobe a cada decisão gravada: a fila é a lista do que **falta** decidir, então o que
  // acabou de sair dela não pode continuar na tela.
  const [versaoDaFila, setVersaoDaFila] = useState(0);

  const navega = useNavigate();
  const local = useLocation();

  const sai = useCallback(() => {
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
