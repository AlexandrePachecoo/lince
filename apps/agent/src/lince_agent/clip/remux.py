"""Remux do clipe: fMP4 concatenado em RAM → MP4 progressivo em disco (§3.5).

O clipe é o **único** arquivo de vídeo que o sistema escreve (NFR-3), e ele nasce
já no destino final: os bytes selecionados estão em RAM e vão para o ffmpeg pela
stdin. Um arquivo bruto intermediário seria vídeo em disco que ninguém pediu.

Ver `build_clip_command` para o porquê de cada opção — em especial o porquê de
**não** haver `-ss` nem `-t` aqui.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from pathlib import Path
from typing import Protocol

from lince_agent.ffmpeg.command import build_clip_command

log = logging.getLogger(__name__)


class RemuxError(Exception):
    """O corte falhou. O evento sobe sem clipe, marcado `clip_failed` (§3.5)."""


class Remuxer(Protocol):
    """O que o `ClipRecorder` precisa de um remuxer.

    Existe para que a orquestração — espera do pós-roll, prazo, sessão perdida,
    fila de pedidos — seja testável sem depender de ffmpeg, e sobretudo para que o
    caminho de falha seja exercitável: um ffmpeg que falha sob demanda é difícil de
    produzir, um dublê que levanta `RemuxError` é uma linha.
    """

    def __call__(self, data: bytes, destination: Path, *, timeout_s: float) -> None: ...


def remux_to_mp4(
    data: bytes,
    destination: Path,
    *,
    timeout_s: float = 30.0,
    ffmpeg_bin: str = "ffmpeg",
) -> None:
    """Escreve `data` (init + fragmentos concatenados) como MP4 em `destination`.

    Nunca deixa arquivo parcial: um MP4 truncado no diretório de pendentes subiria
    para a nuvem como se fosse clipe, e a falha só apareceria quando um humano
    tentasse assistir.
    """
    if not data:
        raise RemuxError("não há bytes para remuxar (buffer vazio)")

    destination.parent.mkdir(parents=True, exist_ok=True)
    argv = build_clip_command(str(destination), ffmpeg_bin=ffmpeg_bin)

    try:
        resultado = subprocess.run(  # noqa: S603
            argv,
            input=data,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        _descarta(destination)
        # Um ffmpeg pendurado não pode segurar os clipes das outras câmeras: o
        # recorder é uma thread só, e o §3.7 tem 15 s de orçamento total.
        raise RemuxError(f"remux passou de {timeout_s}s e foi encerrado") from error
    except OSError as error:
        _descarta(destination)
        raise RemuxError(f"falha ao executar o ffmpeg: {error}") from error

    if resultado.returncode != 0:
        _descarta(destination)
        detalhe = resultado.stderr.decode("utf-8", errors="replace").strip()
        raise RemuxError(f"ffmpeg saiu com código {resultado.returncode}: {detalhe}")

    if not destination.exists() or destination.stat().st_size == 0:
        _descarta(destination)
        raise RemuxError("ffmpeg saiu com sucesso mas não produziu arquivo")


def _descarta(destination: Path) -> None:
    with contextlib.suppress(OSError):
        destination.unlink(missing_ok=True)
