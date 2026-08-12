"""Leitura do pipe de detecção.

`-f rawvideo` não tem framing: o pipe é uma sequência contínua de bytes de pixel,
sem cabeçalho, sem separador. O leitor fatia em blocos de exatamente
`width × height × bytes_por_pixel`, e é por isso que a resolução é forçada no
filtro em vez de descoberta — errar o tamanho não dá erro, embaralha tudo.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass

import numpy as np

from lince_agent.config import PixelFormat

_READ_CHUNK = 1 << 16
"""64 KB: o tamanho do buffer de pipe do kernel. Pedir mais não acelera."""


@dataclass(frozen=True, slots=True)
class Frame:
    """Um frame decodificado, já amostrado na taxa do estágio 2."""

    data: bytes
    width: int
    height: int
    pixel_format: PixelFormat
    sequence: int
    received_at: float
    """`time.monotonic()` na chegada. É o timestamp da borda que o §3.1 exige —
    o relógio da câmera nunca é usado."""

    def as_array(self) -> np.ndarray:
        """Vista NumPy sobre os bytes, sem cópia dos dados.

        BGR24 sai como `(altura, largura, 3)`, pronto para o detector. NV12 e
        YUV420P saem no layout planar `(altura * 3 // 2, largura)`, que é o que
        `cv2.cvtColor` espera receber.
        """
        buffer = np.frombuffer(self.data, dtype=np.uint8)
        if self.pixel_format is PixelFormat.BGR24:
            return buffer.reshape(self.height, self.width, 3)
        return buffer.reshape(self.height * 3 // 2, self.width)


def read_exact(fd: int, size: int) -> bytes | None:
    """Lê exatamente `size` bytes. Devolve `None` no EOF.

    Um pipe entrega o que tiver disponível, não o que foi pedido: um frame de
    921.600 bytes chega em ~14 leituras de 64 KB. EOF no meio de um frame descarta
    o parcial — meio frame não é frame.
    """
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = os.read(fd, min(remaining, _READ_CHUNK))
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def iter_raw_frames(fd: int, frame_bytes: int) -> Iterator[bytes]:
    """Rende frames crus até o EOF do pipe."""
    while True:
        frame = read_exact(fd, frame_bytes)
        if frame is None:
            return
        yield frame
