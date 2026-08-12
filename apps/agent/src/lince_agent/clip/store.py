"""Diretório de clipes pendentes de upload (§3.6).

O clipe é o **único** arquivo de vídeo que o sistema escreve, e ele é temporário:
existe entre o corte e a confirmação do upload, e some depois (NFR-3). Não há
gravação contínua em lugar nenhum.

Enquanto o estágio 6 (fila local e envio) não existe, este teto de disco é a única
coisa que impede o box da loja de encher sozinho — por isso ele entra agora, e não
junto com o uploader.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_EVENT_ID_VALIDO = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
"""O `event_id` vira nome de arquivo, então ele é entrada não confiável até prova
em contrário. Um id com `/` ou `..` escreveria fora do diretório de clipes; barrar
aqui é mais barato que auditar todo caminho que produz um id."""

SUFIXO = ".mp4"


class ClipStore:
    """Onde os clipes esperam o upload, com teto de disco."""

    def __init__(self, directory: Path, *, max_bytes: int = 2 * 1024**3) -> None:
        if max_bytes <= 0:
            raise ValueError(f"max_bytes deve ser positivo, recebi {max_bytes}")
        self._directory = Path(directory)
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._directory.mkdir(parents=True, exist_ok=True)

    @property
    def directory(self) -> Path:
        return self._directory

    def path_for(self, event_id: str) -> Path:
        if not _EVENT_ID_VALIDO.match(event_id):
            raise ValueError(f"event_id inválido para virar nome de arquivo: {event_id!r}")
        return self._directory / f"{event_id}{SUFIXO}"

    def register(self, path: Path) -> None:
        """Contabiliza um clipe recém-escrito e aplica o teto de disco.

        Ao estourar, o **clipe mais antigo** é apagado — nunca o evento. O §3.6 é
        explícito: "clipes são descartados antes dos eventos", porque o metadado é
        o que destrava o alerta e o vídeo é o que ocupa espaço.
        """
        with self._lock:
            if not path.exists():
                raise FileNotFoundError(f"clipe não existe: {path}")
            self._aplica_teto()

    def discard(self, event_id: str) -> bool:
        """Apaga o clipe, se existir. Devolve se havia algo para apagar.

        Chamado pelo estágio 6 depois do upload confirmado — é o que faz o vídeo
        não ficar em disco (NFR-3) — e também depois de uma falha de corte, para não
        deixar arquivo parcial.
        """
        caminho = self.path_for(event_id)
        with self._lock:
            if not caminho.exists():
                return False
            with contextlib.suppress(OSError):
                caminho.unlink()
            return True

    def usage_bytes(self) -> int:
        with self._lock:
            return sum(caminho.stat().st_size for caminho in self._clipes())

    def _clipes(self) -> list[Path]:
        return [caminho for caminho in self._directory.glob(f"*{SUFIXO}") if caminho.is_file()]

    def _aplica_teto(self) -> None:
        clipes = sorted(self._clipes(), key=lambda caminho: caminho.stat().st_mtime)
        total = sum(caminho.stat().st_size for caminho in clipes)

        # Sobra sempre ao menos um: um clipe sozinho maior que o teto significa teto
        # mal dimensionado, e apagá-lo deixaria o evento sem vídeo sem resolver nada.
        while total > self._max_bytes and len(clipes) > 1:
            mais_antigo = clipes.pop(0)
            tamanho = mais_antigo.stat().st_size
            with contextlib.suppress(OSError):
                mais_antigo.unlink()
            total -= tamanho
            log.warning(
                "teto de disco de clipes atingido: %s descartado (%d bytes)",
                mais_antigo.name,
                tamanho,
            )
