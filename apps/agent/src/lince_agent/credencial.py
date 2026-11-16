"""A credencial do agente, no disco do box (§5.1).

O `POST /v1/agents/register` devolve a credencial de longa duração **uma vez**. A nuvem
guarda só o hash e não tem rota que a devolva de novo, e o token de bootstrap que a
comprou é de uso único. Este arquivo é, portanto, o único lugar do mundo onde ela
existe — e é isso que decide tudo o que está aqui:

1. **Escrita atômica e com `fsync`.** Um box que perde energia no meio da gravação não
   pode acordar com meio JSON: a credencial estaria perdida e o token, queimado, e o
   conserto seria alguém gerar outro token e voltar à loja. O `config_cache` faz só o
   `os.replace` porque o que ele guarda a nuvem devolve de novo; aqui não devolve.
2. **Confere que dá para gravar antes de registrar** (`pode_gravar`). Descobrir que o
   volume está só-leitura *depois* de trocar o token é a mesma perda, por outro caminho.
3. **`0600` desde a criação.** O arquivo nasce pelo `os.open` já com o modo, e não por
   `write_text` seguido de `chmod`: entre os dois ele existiria legível pelo mundo.
4. **O token não aparece em `repr` nem em log.** `Credencial` é o tipo que mais
   facilmente cai num `log.info("%s", ...)` de depuração.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

FORMATO = 1
"""Versão do envelope em disco. Um arquivo de formato desconhecido é tratado como
ausente — com log, porque aqui "ausente" custa um token de bootstrap novo."""

_CAMPOS = ("agent_id", "tenant_id", "store_id", "token")


@dataclass(frozen=True, slots=True)
class Credencial:
    """O que o registro devolve: quem este box é, e o segredo que prova isso."""

    agent_id: str
    tenant_id: str
    store_id: str
    token: str = field(repr=False)


class CofreDeCredencial:
    """Um arquivo JSON `0600` no volume de estado do box."""

    def __init__(self, caminho: Path) -> None:
        self._caminho = caminho

    @property
    def caminho(self) -> Path:
        return self._caminho

    def le(self) -> Credencial | None:
        """A credencial guardada, ou `None`. **Nunca levanta.**

        Arquivo ilegível ou corrompido vira `None` com log de erro: quem chama cai no
        token de bootstrap, se houver, ou recusa subir dizendo o que fazer. Levantar
        aqui trocaria essa mensagem por um traceback.
        """
        try:
            bruto = self._caminho.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as erro:
            log.error("credencial ilegível em %s: %s", self._caminho, erro)
            return None

        try:
            envelope = json.loads(bruto)
        except json.JSONDecodeError:
            # Sem o texto do erro: o `JSONDecodeError` cita o trecho que não leu, e o
            # trecho de um arquivo de credencial é a credencial.
            log.error("credencial corrompida em %s", self._caminho)
            return None

        if not isinstance(envelope, dict) or envelope.get("formato") != FORMATO:
            log.error("credencial em %s não é do formato %d", self._caminho, FORMATO)
            return None
        valores = [envelope.get(campo) for campo in _CAMPOS]
        if not all(isinstance(valor, str) and valor for valor in valores):
            log.error("credencial em %s está incompleta", self._caminho)
            return None
        return Credencial(*valores)

    def pode_gravar(self) -> bool:
        """Se uma gravação daria certo agora — conferido **antes** de gastar o token.

        Escreve e apaga um arquivo de verdade no diretório, em vez de olhar permissão:
        volume só-leitura, disco cheio e pai que não é diretório não aparecem em
        `os.access`, e o root do container (§3.8) passa em qualquer bit de modo.
        """
        sonda = self._caminho.with_name(self._caminho.name + ".sonda")
        try:
            self._caminho.parent.mkdir(parents=True, exist_ok=True)
            sonda.write_bytes(b"lince")
            sonda.unlink()
        except OSError as erro:
            log.error("não dá para gravar a credencial em %s: %s", self._caminho, erro)
            with contextlib.suppress(OSError):
                sonda.unlink(missing_ok=True)
            return False
        return True

    def grava(self, credencial: Credencial) -> bool:
        """Substitui a credencial guardada. Devolve se conseguiu.

        `False` e não exceção, como no `config_cache`: quem chama acabou de se
        registrar e tem a credencial na memória. Seguir operando com ela é melhor do
        que parar — mas o log é de erro, porque o próximo reinício não a encontra.
        """
        envelope = {"formato": FORMATO, **{c: getattr(credencial, c) for c in _CAMPOS}}
        dados = json.dumps(envelope, ensure_ascii=False, indent=2).encode("utf-8")
        temporario = self._caminho.with_name(self._caminho.name + ".novo")
        try:
            self._caminho.parent.mkdir(parents=True, exist_ok=True)
            # Um temporário de uma tentativa anterior pode ter outro modo; apagar
            # primeiro garante que o `O_EXCL` crie o arquivo com `0600`.
            temporario.unlink(missing_ok=True)
            descritor = os.open(temporario, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descritor, "wb") as arquivo:
                arquivo.write(dados)
                arquivo.flush()
                # Sem isto o `replace` pode chegar ao disco antes do conteúdo, e uma
                # queda de energia deixa o nome certo apontando para um arquivo vazio.
                os.fsync(arquivo.fileno())
            os.replace(temporario, self._caminho)
            _sincroniza_diretorio(self._caminho.parent)
        except OSError as erro:
            log.error("não consegui gravar a credencial em %s: %s", self._caminho, erro)
            with contextlib.suppress(OSError):
                temporario.unlink(missing_ok=True)
            return False
        return True


def _sincroniza_diretorio(diretorio: Path) -> None:
    """`fsync` do diretório: é nele que mora o `rename`. Melhor esforço — nem todo
    sistema de arquivos deixa abrir diretório, e o conteúdo já está no disco."""
    with contextlib.suppress(OSError):
        descritor = os.open(diretorio, os.O_RDONLY)
        try:
            os.fsync(descritor)
        finally:
            os.close(descritor)
