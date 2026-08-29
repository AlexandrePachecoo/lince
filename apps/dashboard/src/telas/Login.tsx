import { type FormEvent, useState } from "react";
import { ErroDeRede, ErroHttp, login } from "../api/cliente.js";
import { guardaSessao } from "../auth/sessao.js";
import type { SessaoGuardada } from "../auth/sessao.js";

interface Props {
  aoEntrar: (sessao: SessaoGuardada) => void;
}

export function Login({ aoEntrar }: Props) {
  const [email, setEmail] = useState("");
  const [senha, setSenha] = useState("");
  const [erro, setErro] = useState<string | null>(null);
  const [enviando, setEnviando] = useState(false);

  async function envia(evento: FormEvent) {
    evento.preventDefault();
    setErro(null);
    setEnviando(true);
    try {
      aoEntrar(guardaSessao(await login(email, senha)));
    } catch (causa) {
      // Credencial errada nunca diz **qual** metade errou: distinguir "não existe esse
      // e-mail" de "a senha está errada" entrega a lista de quem trabalha na rede a quem
      // só tem a tela de login. A API já responde igual nos dois casos; a tela não pode
      // desfazer isso sendo prestativa.
      if (causa instanceof ErroHttp) {
        setErro(causa.status === 401 ? "E-mail ou senha incorretos." : causa.message);
      } else if (causa instanceof ErroDeRede) {
        setErro("Sem conexão com a nuvem. Confira a internet e tente de novo.");
      } else {
        throw causa;
      }
    } finally {
      setEnviando(false);
    }
  }

  return (
    <main className="tela tela--centrada">
      <h1 className="marca">lince</h1>
      <p className="marca__legenda">Triagem de possíveis ocorrências</p>

      <form className="formulario" onSubmit={envia}>
        <label className="campo">
          <span>E-mail</span>
          <input
            type="email"
            name="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            // O teclado do celular capitaliza a primeira letra e o autocompletar deixa
            // espaço no fim. A API normaliza antes de validar (auth/email.ts), mas
            // desligar aqui evita que o gerente veja o próprio e-mail errado na tela.
            autoCapitalize="none"
            autoCorrect="off"
            autoComplete="username"
            required
          />
        </label>

        <label className="campo">
          <span>Senha</span>
          <input
            type="password"
            name="senha"
            value={senha}
            onChange={(e) => setSenha(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>

        {erro !== null && (
          <p className="aviso aviso--erro" role="alert">
            {erro}
          </p>
        )}

        <button className="botao botao--principal" type="submit" disabled={enviando}>
          {enviando ? "Entrando…" : "Entrar"}
        </button>
      </form>
    </main>
  );
}
