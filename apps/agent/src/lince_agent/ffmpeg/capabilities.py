"""Descoberta do que este ffmpeg e esta máquina conseguem fazer.

O §3.9 exige que o mesmo artefato rode no box da loja (com GPU NVIDIA) e na máquina
de desenvolvimento (sem GPU nenhuma), sem `if cloud` e sem imagem diferente. A
escolha de decode é, portanto, em tempo de execução — e o §3.1 pede fallback
explícito para CPU.
"""

from __future__ import annotations

import functools
import logging
import subprocess

from lince_agent.config import HwAccel

log = logging.getLogger(__name__)

_TIMEOUT_S = 15.0

_REQUIRED_DECODERS: dict[HwAccel, tuple[str, ...]] = {
    HwAccel.CUDA: ("h264_cuvid",),
    HwAccel.CUDA_GPU_FILTER: ("h264_cuvid",),
}


def _run(argv: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - argv fixo, sem shell
        argv, capture_output=True, text=True, timeout=_TIMEOUT_S, check=False
    )
    return result.stdout + result.stderr


@functools.cache
def available_hwaccels(ffmpeg_bin: str = "ffmpeg") -> frozenset[str]:
    """Nomes listados por `ffmpeg -hwaccels`.

    Só diz o que o **build** suporta, não o que a máquina tem: um ffmpeg compilado
    com CUDA lista `cuda` mesmo sem GPU nem driver instalados. Por isso existe o
    `hwaccel_works` abaixo.
    """
    try:
        output = _run([ffmpeg_bin, "-hide_banner", "-hwaccels"])
    except (OSError, subprocess.SubprocessError):
        log.exception("não consegui listar os hwaccels de %s", ffmpeg_bin)
        return frozenset()
    lines = [line.strip() for line in output.splitlines()]
    return frozenset(line for line in lines if line and not line.endswith(":"))


@functools.cache
def available_decoders(ffmpeg_bin: str = "ffmpeg") -> frozenset[str]:
    try:
        output = _run([ffmpeg_bin, "-hide_banner", "-decoders"])
    except (OSError, subprocess.SubprocessError):
        log.exception("não consegui listar os decoders de %s", ffmpeg_bin)
        return frozenset()
    names: set[str] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        flags, name = parts[0], parts[1]
        # As linhas úteis são " V..... h264_cuvid   Nvidia CUVID...". O cabeçalho
        # não tem campo de flags, mas a **legenda** tem — " V..... = Video" passaria
        # por um teste só de flags e registraria um decoder chamado "=". Daí exigir
        # também que o nome seja um identificador.
        if len(flags) == 6 and flags[0] in "VAS" and name.replace("_", "").isalnum():
            names.add(name)
    return frozenset(names)


@functools.cache
def hwaccel_works(hwaccel: HwAccel, ffmpeg_bin: str = "ffmpeg") -> bool:
    """Decodifica um clipe minúsculo de verdade para saber se a aceleração funciona.

    A lista do `-hwaccels` não basta: ela reflete o build, não o driver. Este teste
    gera cinco frames 64x64 em H.264 em memória e tenta decodificá-los com a
    aceleração pedida. É a diferença entre "o ffmpeg sabe falar CUDA" e "esta
    máquina tem GPU e driver funcionando".
    """
    if hwaccel is HwAccel.NONE:
        return True
    if hwaccel.value.split("-")[0] not in available_hwaccels(ffmpeg_bin):
        return False
    decoders = available_decoders(ffmpeg_bin)
    if not all(name in decoders for name in _REQUIRED_DECODERS.get(hwaccel, ())):
        return False

    clip = _synthetic_h264(ffmpeg_bin)
    if clip is None:
        # Build sem libx264: não dá para montar o teste. Cai para o que a lista diz
        # e deixa a falha real aparecer no supervisor.
        log.warning("sem libx264 para o teste de %s; confiando na lista de hwaccels", hwaccel)
        return True

    argv = [ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-hwaccel", "cuda"]
    if hwaccel is HwAccel.CUDA_GPU_FILTER:
        argv += ["-hwaccel_output_format", "cuda"]
    argv += ["-f", "h264", "-i", "pipe:0", "-frames:v", "5", "-f", "null", "-"]

    try:
        result = subprocess.run(  # noqa: S603 - argv fixo, sem shell
            argv, input=clip, capture_output=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("teste de %s falhou", hwaccel)
        return False

    if result.returncode != 0:
        log.info(
            "%s indisponível nesta máquina: %s",
            hwaccel,
            result.stderr.decode("utf-8", errors="replace").strip(),
        )
        return False
    return True


@functools.cache
def _synthetic_h264(ffmpeg_bin: str) -> bytes | None:
    argv = [
        *(ffmpeg_bin, "-hide_banner", "-loglevel", "error"),
        *("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=5"),
        *("-frames:v", "5", "-c:v", "libx264", "-preset", "ultrafast"),
        *("-pix_fmt", "yuv420p", "-f", "h264", "pipe:1"),
    ]
    try:
        result = subprocess.run(  # noqa: S603 - argv fixo, sem shell
            argv, capture_output=True, timeout=_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None


def select_hwaccel(preferred: HwAccel, *, ffmpeg_bin: str = "ffmpeg") -> HwAccel:
    """Confirma a aceleração pedida ou cai para CPU, registrando o motivo.

    Cair para CPU é degradação, não erro: o §3.2 aceita rodar em taxa reduzida
    enquanto a GPU não volta, e um agente que se recusa a subir por falta de driver
    deixa a loja sem detecção nenhuma.
    """
    if preferred is HwAccel.NONE:
        return HwAccel.NONE
    if hwaccel_works(preferred, ffmpeg_bin):
        return preferred
    log.warning("%s indisponível; decode vai para a CPU em taxa reduzida", preferred)
    return HwAccel.NONE
