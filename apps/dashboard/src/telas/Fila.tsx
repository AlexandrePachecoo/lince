import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { buscaFila } from "../api/cliente.js";
import type { Evento } from "../api/tipos.js";
import { type EstadoDaFila, FILA_VAZIA, acumulaPagina } from "./paginacao.js";
import { instanteCurto, quandoFoi } from "./tempo.js";

// A primeira tela do app (§4.5): o que **falta** decidir, do mais recente para o mais
// antigo. Um alerta de agora ainda é acionável -- dá para olhar a câmera, dá para falar
// com alguém; um de ontem virou estatística.
//
// Cada linha inteira é o alvo do toque, e não um botão dentro dela: é o primeiro dos dois
// toques da NFR-9, dado com o polegar, com o celular numa mão. Um alvo pequeno aqui custa
// o toque errado, e o toque errado custa uma triagem para corrigir.

const POR_PAGINA = 25;

interface Props {
  token: string;
  nome: string;
  aoSair: () => void;
  /** Sobe quando um evento é decidido, para a fila recarregar sem o que saiu dela. */
  versao?: number;
}

export function Fila({ token, nome, aoSair, versao = 0 }: Props) {
  const [estado, setEstado] = useState<EstadoDaFila>(FILA_VAZIA);
  const [carregando, setCarregando] = useState(true);
  const [erro, setErro] = useState<string | null>(null);

  const carregaPrimeira = useCallback(async () => {
    setCarregando(true);
    setErro(null);
    try {
      const pagina = await buscaFila(token, { limite: POR_PAGINA });
      setEstado(acumulaPagina(FILA_VAZIA, pagina));
    } catch (causa) {
      setErro(causa instanceof Error ? causa.message : "não foi possível carregar a fila");
    } finally {
      setCarregando(false);
    }
  }, [token]);

  useEffect(() => {
    void carregaPrimeira();
  }, [carregaPrimeira]);

  // `versao` muda quando uma decisão é gravada: a fila é a lista do que falta, então o
  // que acabou de ser decidido não pode continuar nela.
  useEffect(() => {
    if (versao > 0) {
      void carregaPrimeira();
    }
  }, [versao, carregaPrimeira]);

  async function carregaMais() {
    if (estado.proximoCursor === null || carregando) {
      return;
    }
    setCarregando(true);
    try {
      const pagina = await buscaFila(token, {
        limite: POR_PAGINA,
        cursor: estado.proximoCursor,
      });
      setEstado((anterior) => acumulaPagina(anterior, pagina));
    } catch (causa) {
      setErro(causa instanceof Error ? causa.message : "não foi possível carregar mais");
    } finally {
      setCarregando(false);
    }
  }

  return (
    <main className="tela">
      <header className="cabecalho">
        <div>
          <h1 className="cabecalho__titulo">Fila de triagem</h1>
          <p className="cabecalho__legenda">{nome}</p>
        </div>
        <button className="botao botao--discreto" type="button" onClick={aoSair}>
          Sair
        </button>
      </header>

      {erro !== null && (
        <p className="aviso aviso--erro" role="alert">
          {erro}
        </p>
      )}

      {!carregando && estado.eventos.length === 0 && erro === null && (
        <p className="aviso aviso--calmo">Nada para decidir agora.</p>
      )}

      <ul className="lista">
        {estado.eventos.map((evento) => (
          <li key={evento.event_id}>
            <Link className="cartao" to={`/eventos/${encodeURIComponent(evento.event_id)}`}>
              <ItemDaFila evento={evento} />
            </Link>
          </li>
        ))}
      </ul>

      {carregando && <p className="aviso aviso--calmo">Carregando…</p>}

      {estado.proximoCursor !== null && !carregando && (
        <button className="botao botao--discreto" type="button" onClick={carregaMais}>
          Carregar mais
        </button>
      )}
    </main>
  );
}

function ItemDaFila({ evento }: { evento: Evento }) {
  return (
    <>
      <div className="cartao__linha">
        <span className="cartao__camera">{evento.camera_id}</span>
        <span className="cartao__quando" title={instanteCurto(evento.occurred_at)}>
          {quandoFoi(evento.occurred_at)}
        </span>
      </div>

      <div className="cartao__linha cartao__linha--etiquetas">
        {/* O andaime de gatilho da instalação vai marcado: 40 testes numa tarde não podem
            se misturar a alerta de verdade na estatística da câmera (R-1). */}
        {evento.source === "manual" && (
          <span className="etiqueta etiqueta--teste">teste de instalação</span>
        )}
        {evento.clip_state === "pendente" && (
          <span className="etiqueta etiqueta--espera">vídeo subindo</span>
        )}
        {evento.clip_state === "indisponivel" && (
          <span className="etiqueta etiqueta--ausente">sem vídeo</span>
        )}
        <span className="cartao__loja">{evento.store_id}</span>
      </div>
    </>
  );
}
