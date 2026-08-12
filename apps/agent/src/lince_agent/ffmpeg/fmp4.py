"""Parser incremental de MP4 fragmentado, para o pipe do buffer.

O `pipe` do buffer carrega um stream fMP4 contínuo. MP4 é uma sequência de *boxes*,
cada um com 4 bytes de tamanho (big-endian) e 4 bytes de tipo, e é isso que dá
framing ao pipe — ao contrário do pipe de detecção, que depende de o tamanho do
frame ser conhecido de antemão.

O stream tem duas partes:

- `ftyp` + `moov` no começo: o **init segment**. Guarde para sempre — nenhum
  fragmento é reproduzível sem ele.
- Depois, pares `moof` + `mdat`: cada par é **um fragmento**, começando num
  keyframe por causa do `+frag_keyframe`. É a unidade do buffer circular do §3.5:
  descartar o fragmento mais antigo nunca quebra os que ficaram.

`init_segment + fragmentos concatenados` é um MP4 válido e reproduzível. Essa
propriedade é a razão de o estágio 5 poder cortar com `-c copy` sem recodificar
nada.
"""

from __future__ import annotations

import itertools
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

_HEADER_SIZE: Final = 8
_LARGE_HEADER_SIZE: Final = 16

_sessoes = itertools.count(1)
"""Contador de execuções do ffmpeg no processo.

Cada `Fmp4Parser` é uma execução, e o estágio 5 precisa distinguir uma da outra:
depois de uma reconexão chega um `init` novo, com timeline própria, e fragmentos
das duas execuções são inconcatenáveis. Não dá para inferir isso da ordem de
chegada — `on_init_segment` é chamado inline na thread leitora enquanto o
fragmento atravessa a fila até a thread despachante, então o fragmento velho pode
chegar depois do init novo (§3.5)."""
MAX_BUFFERED_BYTES: Final = 32 * 1024 * 1024
"""Teto de segurança do buffer interno. Um fragmento de substream é da ordem de
centenas de KB; chegar a 32 MB sem fechar um box significa stream corrompido, e
crescer sem limite trocaria um erro por um OOM no box da loja."""


class Fmp4ParseError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class InitSegment:
    """`ftyp` + `moov`. Prefixo obrigatório de qualquer clipe montado a partir dos
    fragmentos."""

    data: bytes
    timescale: int
    """Unidades por segundo do timeline de mídia. Com
    `-use_wallclock_as_timestamps 1`, esse timeline é o relógio da borda."""

    session_id: int = 0
    """Qual execução do ffmpeg produziu este init. Ver `_sessoes`."""


@dataclass(frozen=True, slots=True)
class Fragment:
    """Um par `moof` + `mdat`, decodificável isoladamente."""

    data: bytes
    base_media_decode_time: int
    """Instante de início do fragmento, do `tfdt`, em unidades de `timescale`."""

    timescale: int
    received_at: float
    """`time.monotonic()` na chegada. O `base_media_decode_time` é a referência
    precisa; este campo existe para o watchdog, que só precisa saber se algo
    chegou."""

    session_id: int = 0
    """Qual execução do ffmpeg produziu este fragmento. Concatenar fragmentos de
    sessões diferentes produz arquivo que não abre. Ver `_sessoes`."""

    @property
    def start_seconds(self) -> float:
        return self.base_media_decode_time / self.timescale

    def __len__(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class Box:
    """Um box localizado dentro de um buffer maior, sem cópia."""

    type: str
    start: int
    """Início do box, incluindo o cabeçalho — é o que se repassa adiante."""
    body_start: int
    end: int


def iter_boxes(data: bytes, start: int = 0, end: int | None = None) -> Iterator[Box]:
    """Percorre os boxes completos em `data[start:end]`.

    Para silenciosamente no primeiro box incompleto: num pipe, "incompleto"
    significa "o resto ainda não chegou", não erro.
    """
    end = len(data) if end is None else end
    offset = start
    while offset + _HEADER_SIZE <= end:
        (size,) = struct.unpack_from(">I", data, offset)
        box_type = data[offset + 4 : offset + 8].decode("ascii", errors="replace")
        header = _HEADER_SIZE
        if size == 1:
            if offset + _LARGE_HEADER_SIZE > end:
                return
            (size,) = struct.unpack_from(">Q", data, offset + 8)
            header = _LARGE_HEADER_SIZE
        elif size == 0:
            # "até o fim do stream": legítimo no último box de um arquivo, mas num
            # pipe ao vivo não existe fim, então isto é corrupção.
            raise Fmp4ParseError(f"box '{box_type}' com tamanho 0 em stream ao vivo")
        if size < header:
            raise Fmp4ParseError(f"box '{box_type}' com tamanho {size} menor que o cabeçalho")
        if offset + size > end:
            return
        yield Box(type=box_type, start=offset, body_start=offset + header, end=offset + size)
        offset += size


def find_box(
    data: bytes, path: tuple[str, ...], start: int = 0, end: int | None = None
) -> tuple[int, int] | None:
    """Desce por um caminho de boxes aninhados, ex.: `("moov", "trak", "mdia", "mdhd")`.

    Devolve `(início, fim)` do **corpo** do box mais interno.
    """
    if not path:
        return None
    head, *rest = path
    for box in iter_boxes(data, start, end):
        if box.type != head:
            continue
        if not rest:
            return box.body_start, box.end
        found = find_box(data, tuple(rest), box.body_start, box.end)
        if found is not None:
            return found
    return None


def parse_timescale(init_data: bytes) -> int:
    """Timescale do `mdhd` da trilha de vídeo."""
    located = find_box(init_data, ("moov", "trak", "mdia", "mdhd"))
    if located is None:
        raise Fmp4ParseError("mdhd não encontrado no init segment")
    start, end = located
    version = init_data[start]
    # v0: creation(4) modification(4) timescale(4); v1 usa 8 bytes nos dois primeiros.
    offset = start + 4 + (16 if version == 1 else 8)
    if offset + 4 > end:
        raise Fmp4ParseError("mdhd truncado")
    (timescale,) = struct.unpack_from(">I", init_data, offset)
    if timescale == 0:
        raise Fmp4ParseError("timescale zero no mdhd")
    return timescale


def parse_base_media_decode_time(moof_data: bytes) -> int:
    """`baseMediaDecodeTime` do `tfdt`, o instante de início do fragmento."""
    located = find_box(moof_data, ("moof", "traf", "tfdt"))
    if located is None:
        raise Fmp4ParseError("tfdt não encontrado no moof")
    start, end = located
    version = moof_data[start]
    offset = start + 4
    if version == 1:
        if offset + 8 > end:
            raise Fmp4ParseError("tfdt truncado")
        return struct.unpack_from(">Q", moof_data, offset)[0]
    if offset + 4 > end:
        raise Fmp4ParseError("tfdt truncado")
    return struct.unpack_from(">I", moof_data, offset)[0]


class Fmp4Parser:
    """Alimente com os bytes que chegam do pipe; receba init segment e fragmentos.

    Feito para ser alimentado por uma thread leitora que não sabe nada de MP4: os
    chunks do pipe não respeitam fronteira de box nenhuma, e o parser guarda o
    resto entre chamadas.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._init_boxes = bytearray()
        self._init: InitSegment | None = None
        self._pending_moof: bytes | None = None
        self._session_id = next(_sessoes)

    @property
    def session_id(self) -> int:
        return self._session_id

    @property
    def init_segment(self) -> InitSegment | None:
        return self._init

    def feed(self, chunk: bytes, *, received_at: float) -> list[InitSegment | Fragment]:
        self._buffer += chunk
        if len(self._buffer) > MAX_BUFFERED_BYTES:
            raise Fmp4ParseError(
                f"buffer fMP4 passou de {MAX_BUFFERED_BYTES} bytes sem fechar um box"
            )

        emitted: list[InitSegment | Fragment] = []
        consumed = 0
        for box in iter_boxes(self._buffer):
            item = self._consume_box(
                box.type, bytes(self._buffer[box.start : box.end]), received_at
            )
            consumed = box.end
            if item is not None:
                emitted.append(item)

        del self._buffer[:consumed]
        return emitted

    def _consume_box(
        self, box_type: str, raw: bytes, received_at: float
    ) -> InitSegment | Fragment | None:
        if box_type == "moof":
            if self._init is None:
                self._init = self._finalize_init()
                self._pending_moof = raw
                return self._init
            self._pending_moof = raw
            return None

        if box_type == "mdat" and self._pending_moof is not None:
            moof = self._pending_moof
            self._pending_moof = None
            assert self._init is not None
            return Fragment(
                data=moof + raw,
                base_media_decode_time=parse_base_media_decode_time(moof),
                timescale=self._init.timescale,
                received_at=received_at,
                session_id=self._session_id,
            )

        if self._init is None:
            self._init_boxes += raw
        # Boxes fora do padrão depois do init (`free`, `skip`) são descartados: não
        # carregam mídia e só atrapalhariam a concatenação.
        return None

    def _finalize_init(self) -> InitSegment:
        if not self._init_boxes:
            raise Fmp4ParseError("moof antes de qualquer ftyp/moov")
        data = bytes(self._init_boxes)
        self._init_boxes.clear()
        return InitSegment(data=data, timescale=parse_timescale(data), session_id=self._session_id)
