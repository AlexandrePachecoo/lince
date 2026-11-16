import { useState } from "react";
import { Link } from "react-router-dom";
import { geraTokenDeInstalacao } from "../api/cliente.js";
import type { TokenDeInstalacao } from "../api/tipos.js";
import { instanteCurto } from "./tempo.js";

// A instalação do box (§5.1): o admin gera aqui o token de uso único que o box troca
// por credencial na primeira subida. É o passo que antes era um INSERT no banco.
//
// O token só existe na memória desta tela. Não vai para `localStorage` com a sessão,
// nem para a URL: ele vira a credencial de um box, e um celular que passa de mão em mão
// no turno não pode guardar isso. O preço é honesto e vai escrito na tela — saiu daqui
// sem copiar, gera outro.
//
// O que se mostra é a linha do `.env`, e não o token solto: é para lá que ele vai
// (docs/box.md), e copiar a linha inteira tira do instalador a chance de errar o nome
// da variável — que não daria erro de digitação, daria um box que não se registra.

/** Nome da variável que o agente lê. O par está em infra/box/docker-compose.yml. */
export const VARIAVEL_DO_BOX = "LINCE_BOOTSTRAP_TOKEN";

interface Props {
  token: string;
  /** Lojas em que quem está logado é admin. Vazia é "esta pessoa não instala box". */
  lojas: readonly string[];
}

export function Box({ token, lojas }: Props) {
  const [gerado, setGerado] = useState<TokenDeInstalacao | null>(null);
  const [gerando, setGerando] = useState<string | null>(null);
  const [erro, setErro] = useState<string | null>(null);
  const [copia, setCopia] = useState<"nao" | "copiado" | "falhou">("nao");

  async function gera(lojaId: string) {
    setGerando(lojaId);
    setErro(null);
    setCopia("nao");
    // O anterior some da tela antes de o novo chegar: a API já o venceu, e mostrá-lo
    // por mais um instante é convidar alguém a copiar um token morto.
    setGerado(null);
    try {
      setGerado(await geraTokenDeInstalacao(token, lojaId));
    } catch (causa) {
      setErro(causa instanceof Error ? causa.message : "não foi possível gerar o token");
    } finally {
      setGerando(null);
    }
  }

  async function copiaLinha(linha: string) {
    // `navigator.clipboard` só existe em origem segura e pode ser recusado pelo
    // navegador. Falhar em silêncio deixaria o instalador colando o que já estava na
    // área de transferência — por isso a falha é dita, e a linha fica selecionável.
    try {
      await navigator.clipboard.writeText(linha);
      setCopia("copiado");
    } catch {
      setCopia("falhou");
    }
  }

  const linha = gerado === null ? null : `${VARIAVEL_DO_BOX}=${gerado.bootstrap_token}`;

  return (
    <main className="tela">
      <header className="cabecalho">
        <div>
          <h1 className="cabecalho__titulo">Instalar box</h1>
          <p className="cabecalho__legenda">Token de instalação, de uso único</p>
        </div>
        <Link className="botao botao--discreto" to="/fila">
          Fila
        </Link>
      </header>

      {lojas.length === 0 && (
        <p className="aviso aviso--calmo">
          Só o administrador da loja gera o token de instalação do box.
        </p>
      )}

      {erro !== null && (
        <p className="aviso aviso--erro" role="alert">
          {erro}
        </p>
      )}

      <ul className="lista">
        {lojas.map((lojaId) => (
          <li key={lojaId} className="cartao">
            <div className="cartao__linha">
              <span className="cartao__camera">{lojaId}</span>
              <button
                className="botao"
                type="button"
                disabled={gerando !== null}
                onClick={() => void gera(lojaId)}
              >
                {gerando === lojaId ? "Gerando…" : "Gerar token"}
              </button>
            </div>
          </li>
        ))}
      </ul>

      {gerado !== null && linha !== null && (
        <section className="segredo" aria-label="Token de instalação">
          <p>
            Token da loja <strong>{gerado.store_id}</strong>. Cole esta linha no <code>.env</code>{" "}
            do box:
          </p>
          <code className="segredo__valor" data-testid="linha-do-env">
            {linha}
          </code>
          <button className="botao" type="button" onClick={() => void copiaLinha(linha)}>
            {copia === "copiado" ? "Copiado" : "Copiar"}
          </button>
          {copia === "falhou" && (
            <p className="aviso aviso--erro" role="alert">
              O navegador não deixou copiar. Selecione a linha acima e copie à mão.
            </p>
          )}
          <p className="aviso aviso--alerta">
            Aparece só agora: saindo desta tela, é preciso gerar outro. Vale até{" "}
            {instanteCurto(gerado.expires_at)} e serve para um box só. Gerar outro token para esta
            loja cancela este.
          </p>
        </section>
      )}
    </main>
  );
}
