import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { ErroHttp, buscaClipeUrl, buscaEvento, decide } from "../api/cliente.js";
import type { Decisao, Evento as EventoDoContrato } from "../api/tipos.js";
import { apresentacaoDoClipe, valeReconsultar } from "./estado-clipe.js";
import { instanteCurto } from "./tempo.js";

// A tela onde a decisão acontece — o segundo dos dois toques da NFR-9.
//
// O primeiro toque trouxe até aqui, e a tela abre com o vídeo **já rodando**: o detalhe e
// a URL do clipe são pedidos em paralelo, porque encadeá-los somaria dois round-trips
// antes do primeiro frame. O segundo toque é um dos três botões do rodapé.
//
// Nada aqui espera o vídeo para liberar a decisão. Um upload que não vem deixaria o
// evento sem triagem para sempre, e evento sem triagem é dívida operacional visível
// (§4.5), não um estado silencioso.

const DECISOES: Array<{ valor: Decisao; rotulo: string; classe: string }> = [
  { valor: "confirmado", rotulo: "Confirmar", classe: "botao--confirmar" },
  { valor: "falso_positivo", rotulo: "Falso positivo", classe: "botao--descartar" },
  // Mesmo tamanho e mesmo peso visual dos outros dois, de propósito: escondido num menu,
  // o triador chuta `falso_positivo` no clipe em que não dá para ver nada — e envenena
  // exatamente a métrica que o R-1 manda vigiar.
  { valor: "inconclusivo", rotulo: "Não dá para ver", classe: "botao--inconclusivo" },
];

/** De quanto em quanto tempo se volta a perguntar por um clipe que ainda está subindo. */
const RECONSULTA_MS = 5_000;

interface Props {
  token: string;
  /** Chamado depois de uma decisão gravada, para a fila recarregar sem o que saiu dela. */
  aoDecidir: () => void;
}

export function Evento({ token, aoDecidir }: Props) {
  const { eventId = "" } = useParams();
  const navega = useNavigate();

  const [evento, setEvento] = useState<EventoDoContrato | null>(null);
  const [urlClipe, setUrlClipe] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [gravando, setGravando] = useState(false);

  const carrega = useCallback(async () => {
    setErro(null);
    // Em paralelo, e com o clipe tolerando falha: 409 aqui é "o vídeo ainda não chegou"
    // (ou não vem), que é informação, não erro. Deixar o `Promise.all` estourar por causa
    // dele esconderia o evento inteiro por falta de vídeo.
    const [detalhe, clipe] = await Promise.all([
      buscaEvento(token, eventId),
      buscaClipeUrl(token, eventId).catch(() => null),
    ]);
    setEvento(detalhe.evento);
    setUrlClipe(clipe?.url ?? null);
  }, [token, eventId]);

  useEffect(() => {
    carrega().catch((causa: unknown) => {
      if (causa instanceof ErroHttp && causa.status === 404) {
        setErro("Este evento não existe, ou não é de uma loja que você acessa.");
      } else if (causa instanceof ErroHttp && causa.status === 403) {
        setErro("Você não tem acesso à loja deste evento. Peça ao admin da loja.");
      } else {
        setErro(causa instanceof Error ? causa.message : "não foi possível abrir o evento");
      }
    });
  }, [carrega]);

  // Enquanto os bytes ainda podem chegar, volta a perguntar. Para assim que o clipe
  // resolve para um lado ou para o outro -- `indisponivel` não muda mais, e insistir
  // seria bater na API para sempre.
  useEffect(() => {
    if (evento === null || !valeReconsultar(evento)) {
      return;
    }
    const timer = setInterval(() => {
      carrega().catch(() => {
        /* falha de reconsulta não derruba a tela: o que está nela continua válido */
      });
    }, RECONSULTA_MS);
    return () => clearInterval(timer);
  }, [evento, carrega]);

  async function grava(decisao: Decisao) {
    setGravando(true);
    setErro(null);
    try {
      const resposta = await decide(token, eventId, decisao);
      setEvento(resposta.evento);
      aoDecidir();
      navega("/fila");
    } catch (causa) {
      setErro(causa instanceof Error ? causa.message : "não foi possível gravar a decisão");
      setGravando(false);
    }
  }

  if (erro !== null && evento === null) {
    return (
      <main className="tela">
        <p className="aviso aviso--erro" role="alert">
          {erro}
        </p>
        <Link className="botao botao--discreto" to="/fila">
          Voltar à fila
        </Link>
      </main>
    );
  }

  if (evento === null) {
    return (
      <main className="tela">
        <p className="aviso aviso--calmo">Carregando…</p>
      </main>
    );
  }

  const clipe = apresentacaoDoClipe(evento);

  return (
    <main className="tela tela--evento">
      <header className="cabecalho">
        <Link className="botao botao--discreto" to="/fila">
          ← Fila
        </Link>
        <span className="cabecalho__camera">{evento.camera_id}</span>
      </header>

      <section className="palco">
        {clipe.tipo === "player" && urlClipe !== null && (
          // `muted` não é escolha estética: sem ele o navegador recusa o autoplay, o
          // vídeo fica parado e o primeiro toque vira dois -- a NFR-9 inteira. O clipe
          // não tem áudio de qualquer forma.
          <video
            data-testid="clipe"
            className="palco__video"
            src={urlClipe}
            autoPlay
            muted
            playsInline
            controls
            loop
          >
            <track kind="captions" label="sem legendas" />
          </video>
        )}
        {clipe.tipo === "player" && urlClipe === null && (
          <p className="aviso aviso--calmo">Preparando o vídeo…</p>
        )}
        {clipe.tipo === "subindo" && (
          <p className="aviso aviso--calmo">
            O vídeo ainda está subindo da loja. Dá para decidir sem ele.
          </p>
        )}
        {clipe.tipo === "sem_video" && (
          <p className="aviso aviso--ausente">
            Não vai haver vídeo para este evento: {clipe.motivo}
          </p>
        )}
      </section>

      <section className="ficha">
        <dl>
          <div>
            <dt>Quando</dt>
            <dd>{instanteCurto(evento.occurred_at)}</dd>
          </div>
          <div>
            <dt>Loja</dt>
            <dd>{evento.store_id}</dd>
          </div>
          <div>
            <dt>Origem</dt>
            <dd>
              {evento.source === "manual"
                ? "teste de instalação"
                : (evento.rule?.id ?? "regra da câmera")}
            </dd>
          </div>
        </dl>

        {evento.triagem !== null && (
          <p className="aviso aviso--calmo">
            {evento.triagem.revisada ? "Decisão corrigida" : "Já decidido"} por{" "}
            {evento.triagem.decidido_por.nome}: <strong>{rotuloDe(evento.triagem.decisao)}</strong>.
            Decidir de novo grava uma correção.
          </p>
        )}
      </section>

      {erro !== null && (
        <p className="aviso aviso--erro" role="alert">
          {erro}
        </p>
      )}

      {/* Fixos no rodapé: é onde o polegar alcança com o celular numa mão, e é o segundo
          toque. Os três têm o mesmo tamanho — o alvo não muda de lugar entre um evento e
          o seguinte, que é o que permite decidir sem reler a tela. */}
      <footer className="decisao">
        {DECISOES.map(({ valor, rotulo, classe }) => (
          <button
            key={valor}
            type="button"
            className={`botao botao--decisao ${classe}`}
            disabled={gravando}
            onClick={() => void grava(valor)}
          >
            {rotulo}
          </button>
        ))}
      </footer>
    </main>
  );
}

function rotuloDe(decisao: Decisao): string {
  return DECISOES.find((d) => d.valor === decisao)?.rotulo ?? decisao;
}
