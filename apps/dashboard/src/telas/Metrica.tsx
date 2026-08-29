import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { buscaMetrica } from "../api/cliente.js";
import type { CameraMetrica, MetricaFalsoPositivo } from "../api/tipos.js";

// Falso positivo por câmera (R-1). A pergunta que esta tela responde é única e
// operacional: **em qual câmera mexer**. Por isso é uma lista ordenada da pior para a
// melhor, e não um gráfico: o gerente abre isto na segunda de manhã para decidir onde
// gastar a hora que ele tem, e um gráfico exigiria um segundo passo para chegar no nome
// da câmera.
//
// O que a tela não faz: esconder a incerteza. Uma câmera sem triagem nenhuma tem zero
// falso positivo e apareceria no fim da lista, parecendo a melhor da loja — então os
// pendentes vão na linha, com destaque próprio.

const PERIODOS = [7, 30, 90] as const;

interface Props {
  token: string;
}

export function Metrica({ token }: Props) {
  const [dias, setDias] = useState<number>(7);
  const [dados, setDados] = useState<MetricaFalsoPositivo | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [carregando, setCarregando] = useState(true);

  const carrega = useCallback(async () => {
    setCarregando(true);
    setErro(null);
    try {
      setDados(await buscaMetrica(token, dias));
    } catch (causa) {
      setErro(causa instanceof Error ? causa.message : "não foi possível ler a métrica");
    } finally {
      setCarregando(false);
    }
  }, [token, dias]);

  useEffect(() => {
    void carrega();
  }, [carrega]);

  const acimaDoLimite = dados?.cameras.filter((c) => c.acima_do_limite).length ?? 0;

  return (
    <main className="tela">
      <header className="cabecalho">
        <div>
          <h1 className="cabecalho__titulo">Alertas falsos por câmera</h1>
          {dados !== null && (
            <p className="cabecalho__legenda">
              {dados.periodo.desde} a {dados.periodo.ate} · limite de {dados.limite_diario} por dia
            </p>
          )}
        </div>
        <Link className="botao botao--discreto" to="/fila">
          Fila
        </Link>
      </header>

      <div className="periodo">
        {PERIODOS.map((opcao) => (
          <button
            key={opcao}
            type="button"
            className={`botao botao--discreto${dias === opcao ? " botao--ativo" : ""}`}
            onClick={() => setDias(opcao)}
          >
            {opcao} dias
          </button>
        ))}
      </div>

      {erro !== null && (
        <p className="aviso aviso--erro" role="alert">
          {erro}
        </p>
      )}

      {carregando && <p className="aviso aviso--calmo">Carregando…</p>}

      {!carregando && dados !== null && dados.cameras.length === 0 && erro === null && (
        <p className="aviso aviso--calmo">
          Nenhum alerta de regra nos últimos {dados.periodo.dias} dias.
        </p>
      )}

      {!carregando && acimaDoLimite > 0 && (
        // O resumo existe para a tela ter uma resposta antes de ser lida linha a linha:
        // "quantas câmeras estão fora do requisito" é a pergunta que decide se vale
        // abrir a agenda hoje.
        // `<output>` em vez de `role="status"`: é o elemento semântico para um resultado
        // calculado, e o leitor de tela anuncia a mudança sem precisar do atributo.
        <output className="aviso aviso--alerta">
          {acimaDoLimite === 1
            ? "1 câmera passou do limite em algum dia."
            : `${acimaDoLimite} câmeras passaram do limite em algum dia.`}
        </output>
      )}

      <ul className="lista">
        {dados?.cameras.map((camera) => (
          <li key={`${camera.store_id} ${camera.camera_id}`}>
            <LinhaDaCamera camera={camera} limite={dados.limite_diario} dias={dados.periodo.dias} />
          </li>
        ))}
      </ul>
    </main>
  );
}

function LinhaDaCamera({
  camera,
  limite,
  dias,
}: {
  camera: CameraMetrica;
  limite: number;
  dias: number;
}) {
  return (
    <article className={`cartao${camera.acima_do_limite ? " cartao--alerta" : ""}`}>
      <div className="cartao__linha">
        <span className="cartao__camera">{camera.camera_id}</span>
        <span className="cartao__loja">{camera.store_id}</span>
      </div>

      <div className="cartao__linha cartao__linha--numeros">
        <Numero
          valor={camera.falso_positivo}
          rotulo={camera.falso_positivo === 1 ? "alerta falso" : "alertas falsos"}
          destaque={camera.acima_do_limite}
        />
        <Numero valor={camera.falso_positivo_por_dia} rotulo="por dia" />
        <Numero valor={camera.confirmado} rotulo="confirmados" />
        {/* Não somado ao falso positivo de propósito: é outro problema, com outra ação —
            inconclusivo alto é ângulo, luz ou posicionamento (R-3), não limiar. */}
        <Numero valor={camera.inconclusivo} rotulo="sem dar para ver" />
      </div>

      {camera.acima_do_limite && camera.pior_dia !== null && (
        // O que julga a NFR-2 é o pior dia, não a média — e é ele que a tela mostra por
        // extenso, porque é o dia que o gerente lembra de ter recebido a rajada.
        <p className="cartao__alerta">
          {camera.pior_dia.falso_positivo} num só dia ({camera.pior_dia.dia}) — o limite é {limite}.
        </p>
      )}

      {camera.pendentes > 0 && (
        // Sem isto o número mente. Zero falso positivo numa câmera que ninguém triou não
        // é uma câmera boa: é uma câmera desconhecida, e a diferença decide se vale
        // mexer nela ou triar a fila primeiro.
        <p className="cartao__ressalva">
          {camera.pendentes} de {camera.eventos} ainda sem triagem em {dias} dias — o número acima
          conta só o que foi decidido.
        </p>
      )}
    </article>
  );
}

function Numero({
  valor,
  rotulo,
  destaque = false,
}: {
  valor: number;
  rotulo: string;
  destaque?: boolean;
}) {
  return (
    <span className={`numero${destaque ? " numero--destaque" : ""}`}>
      <strong>{valor}</strong>
      <small>{rotulo}</small>
    </span>
  );
}
