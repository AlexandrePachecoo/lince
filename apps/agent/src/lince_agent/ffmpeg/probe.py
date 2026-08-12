"""`ffprobe` da câmera: o que ela realmente entrega, e não o que a configuração diz.

O item 1 do §10 ("pendências que precisam de dado real") é a taxa de frames que o
substream de fato entrega, base de todo o dimensionamento de decode. Este módulo é
como esse número sai da câmera.

Vale também como verificação de configuração: se a câmera entrega 1280x720 e a
configuração diz 640x480, o filtro `scale` vai reescalar em CPU a cada frame, em
silêncio, e o custo só aparece no benchmark.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from fractions import Fraction

from lince_agent.config import DecodeOptions
from lince_agent.ffmpeg.command import build_probe_command

log = logging.getLogger(__name__)


class ProbeError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class StreamInfo:
    codec: str
    width: int
    height: int
    avg_frame_rate: float
    """Taxa média medida na janela do probe."""
    r_frame_rate: float
    """Taxa nominal declarada pelo stream. Câmera CFTV costuma declarar uma coisa e
    entregar outra sob carga — a divergência entre as duas é informação."""

    def matches(self, decode: DecodeOptions) -> bool:
        return self.width == decode.width and self.height == decode.height


def _parse_rate(value: str | None) -> float:
    """`avg_frame_rate` vem como fração, ex.: "15/1". "0/0" significa desconhecido."""
    if not value:
        return 0.0
    try:
        fraction = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return 0.0
    return float(fraction)


def probe_stream(
    url: str,
    decode: DecodeOptions,
    *,
    ffprobe_bin: str = "ffprobe",
    timeout_s: float = 20.0,
) -> StreamInfo:
    argv = build_probe_command(url, decode, ffprobe_bin=ffprobe_bin)
    try:
        result = subprocess.run(  # noqa: S603 - argv montado por build_probe_command
            argv, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except subprocess.TimeoutExpired as error:
        raise ProbeError(f"ffprobe não respondeu em {timeout_s:.0f}s para {url}") from error
    except OSError as error:
        raise ProbeError(f"não consegui executar {ffprobe_bin}: {error}") from error

    if result.returncode != 0:
        raise ProbeError(f"ffprobe falhou em {url}: {result.stderr.strip()}")

    try:
        streams = json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError as error:
        raise ProbeError(f"saída do ffprobe não é JSON: {error}") from error
    if not streams:
        raise ProbeError(f"nenhum stream de vídeo em {url}")

    stream = streams[0]
    info = StreamInfo(
        codec=stream.get("codec_name", "desconhecido"),
        width=int(stream.get("width", 0)),
        height=int(stream.get("height", 0)),
        avg_frame_rate=_parse_rate(stream.get("avg_frame_rate")),
        r_frame_rate=_parse_rate(stream.get("r_frame_rate")),
    )
    if not info.matches(decode):
        log.warning(
            "câmera entrega %dx%d mas a configuração pede %dx%d: haverá reescala por frame, em CPU",
            info.width,
            info.height,
            decode.width,
            decode.height,
        )
    return info
