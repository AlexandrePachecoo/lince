import { useEffect, useState } from "react";
import {
  type AmbienteNavegador,
  type EstadoAvisos,
  ambienteReal,
  ativaAvisos,
  desativaAvisos,
  estadoDosAvisos,
  sincronizaAvisos,
} from "../push/avisos.js";

// O liga-desliga dos avisos no celular, no cabeçalho da fila (§4.5).
//
// Fica na fila, e não numa tela de configurações: é ali que o gerente está quando percebe
// que só soube do furto porque abriu o app. Um botão escondido num menu é um aviso que
// nunca é ligado — e o produto volta a depender de alguém lembrar de olhar.

interface Props {
  token: string;
  /** Injetável para o teste; o padrão é o navegador de verdade. */
  ambiente?: AmbienteNavegador;
}

const DICA_IPHONE =
  "No iPhone, os avisos só chegam com o app na tela de início: toque em Compartilhar, " +
  "depois em Adicionar à Tela de Início, e abra o lince por lá.";

export function BotaoAvisos({ token, ambiente }: Props) {
  const [amb] = useState(() => ambiente ?? ambienteReal());
  const [estado, setEstado] = useState<EstadoAvisos | null>(null);
  const [ocupado, setOcupado] = useState(false);
  const [dica, setDica] = useState<string | null>(null);

  useEffect(() => {
    let vivo = true;
    void (async () => {
      const atual = await estadoDosAvisos(amb);
      if (vivo) {
        setEstado(atual);
      }
      // O aparelho já inscrito passa a ser de quem entrou agora (ver sincronizaAvisos).
      // Falhar aqui não é motivo para tela de erro: o aviso continua indo para alguém, e
      // a próxima abertura tenta de novo.
      await sincronizaAvisos(token, amb).catch(() => undefined);
    })();
    return () => {
      vivo = false;
    };
  }, [token, amb]);

  async function alterna() {
    if (estado === "so_na_tela_inicial") {
      setDica(DICA_IPHONE);
      return;
    }
    setOcupado(true);
    setDica(null);
    try {
      if (estado === "ativo") {
        await desativaAvisos(token, amb);
        setEstado("inativo");
      } else {
        const novo = await ativaAvisos(token, amb);
        setEstado(novo);
        if (novo === "negado") {
          setDica(
            "Avisos bloqueados. Libere as notificações do lince nas configurações do navegador.",
          );
        }
      }
    } catch {
      setDica("Não foi possível alterar os avisos agora. Tente de novo.");
    } finally {
      setOcupado(false);
    }
  }

  if (estado === null || estado === "sem_suporte") {
    return null;
  }

  if (estado === "indisponivel") {
    return <span className="cabecalho__nota">Avisos indisponíveis</span>;
  }

  const rotulo =
    estado === "ativo"
      ? "Avisos ligados"
      : estado === "negado"
        ? "Avisos bloqueados"
        : "Ativar avisos";

  return (
    <>
      <button
        className={estado === "ativo" ? "botao botao--discreto" : "botao"}
        type="button"
        onClick={alterna}
        disabled={ocupado || estado === "negado"}
        aria-pressed={estado === "ativo"}
        title={estado === "ativo" ? "Toque para desligar os avisos neste aparelho" : undefined}
      >
        {rotulo}
      </button>
      {dica !== null && <output className="aviso aviso--calmo cabecalho__dica">{dica}</output>}
    </>
  );
}
