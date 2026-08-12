"""Ciclo de vida de um processo ffmpeg e das threads que drenam seus pipes.

Um processo por câmera, três pipes: frames decodificados, fragmentos fMP4 e stderr.
Os três precisam ser drenados **o tempo todo e independentemente** — ver o docstring
de `queues.py` para o porquê. Daí o desenho:

    ffmpeg ──pipe:1──> thread leitora ──> fila (drop oldest) ──> thread despachante ──> on_frame
           ──pipe:N──> thread leitora ──> fila (drop oldest) ──> thread despachante ──> on_fragment
           ──stderr──> thread leitora ─────────────────────────────────────────────> on_log

Separar leitura de despacho é o que permite que `on_frame` seja o YOLO, que leva
centenas de milissegundos, sem que isso alcance o ffmpeg.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from lince_agent.config import DecodeOptions
from lince_agent.ffmpeg.command import build_ingest_command
from lince_agent.ffmpeg.fmp4 import Fmp4ParseError, Fmp4Parser, Fragment, InitSegment
from lince_agent.ffmpeg.queues import DropOldestQueue
from lince_agent.ffmpeg.rawframe import Frame, read_exact

log = logging.getLogger(__name__)

_STDERR_ENCODING = "utf-8"
_JOIN_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class IngestCallbacks:
    """Pontos de encaixe dos estágios seguintes.

    `on_fragment` é a fronteira com o estágio 5: o buffer circular de 30 s pluga
    aqui sem tocar em nada desta camada.
    """

    on_frame: Callable[[Frame], None] | None = None
    on_init_segment: Callable[[InitSegment], None] | None = None
    on_fragment: Callable[[Fragment], None] | None = None
    on_log: Callable[[str], None] | None = None


@dataclass(frozen=True, slots=True)
class IngestStats:
    """Instantâneo consistente. Alimenta o heartbeat do §5.3."""

    frames: int = 0
    frames_dropped: int = 0
    fragments: int = 0
    fragments_dropped: int = 0
    fragment_bytes: int = 0
    last_frame_at: float | None = None
    """`time.monotonic()` do último frame. É o relógio do watchdog: stream travado
    sem fechar a conexão só se detecta pela ausência de frame novo."""
    started_at: float = 0.0
    frames_per_second: float = 0.0
    """Taxa medida numa janela recente, não desde a subida.

    A média de vida inteira serviria para nada: ela é diluída pelos segundos de
    conexão e, pior, esconde uma câmera que degradou depois de dez minutos boa —
    exatamente o que o §5.3 quer detectar com `decode_fps`."""

    @property
    def uptime_s(self) -> float:
        return max(0.0, time.monotonic() - self.started_at) if self.started_at else 0.0


_RATE_WINDOW = 32
"""Frames considerados no cálculo da taxa: ~10 s a 3 fps."""


@dataclass(slots=True)
class _MutableStats:
    frames: int = 0
    fragments: int = 0
    fragment_bytes: int = 0
    last_frame_at: float | None = None
    started_at: float = 0.0
    recent_frame_times: deque[float] = field(default_factory=lambda: deque(maxlen=_RATE_WINDOW))
    lock: threading.Lock = field(default_factory=threading.Lock)

    def rate(self) -> float:
        times = self.recent_frame_times
        if len(times) < 2:
            return 0.0
        elapsed = times[-1] - times[0]
        return (len(times) - 1) / elapsed if elapsed > 0 else 0.0


class IngestProcess(Protocol):
    """O que o supervisor precisa de um processo de ingestão.

    Existe para que a máquina de estados do supervisor (backoff, watchdog,
    transições de saúde) seja testável com um dublê e um relógio falso, em
    milissegundos e sem rede. Testar essa lógica só através de ffmpeg de verdade
    obrigaria a esperar segundos por transição e tornaria impossível reproduzir os
    casos raros — câmera que conecta e nunca entrega frame, por exemplo.
    """

    @property
    def command(self) -> list[str]: ...

    @property
    def returncode(self) -> int | None: ...

    def start(self) -> None: ...

    def stop(self, grace_s: float = ...) -> int | None: ...

    def is_running(self) -> bool: ...

    def stats(self) -> IngestStats: ...


class FfmpegIngest:
    """Um processo ffmpeg de ingestão, com os pipes já drenados.

    Não reconecta e não decide nada sobre falhas: quem faz isso é o
    `ingest.supervisor`. Aqui só existe "está rodando" e "morreu".
    """

    def __init__(
        self,
        url: str,
        decode: DecodeOptions,
        callbacks: IngestCallbacks | None = None,
        *,
        ffmpeg_bin: str = "ffmpeg",
        frame_queue_size: int = 8,
        fragment_queue_size: int = 64,
    ) -> None:
        self._url = url
        self._decode = decode
        self._callbacks = callbacks or IngestCallbacks()
        self._ffmpeg_bin = ffmpeg_bin

        # 8 frames a 3 fps são ~2,6 s de folga para o detector. Mais que isso só
        # atrasaria a detecção com frames velhos: o §3.2 prefere descartar o antigo
        # a processar tarde.
        self._frames: DropOldestQueue[Frame] = DropOldestQueue(frame_queue_size)
        # Fragmentos são raros (1 por GOP) e pequenos; a fila é generosa porque
        # descartar aqui abre buraco no buffer do estágio 5. Ainda assim ela é
        # limitada: travar o ffmpeg seria pior que perder um fragmento.
        self._fragments: DropOldestQueue[Fragment] = DropOldestQueue(fragment_queue_size)

        self._process: subprocess.Popen[bytes] | None = None
        self._fragment_read_fd: int | None = None
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._stats = _MutableStats()
        self._command: list[str] = []

    @property
    def command(self) -> list[str]:
        """O argv efetivamente executado. Vale logar na subida de cada câmera."""
        return list(self._command)

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self) -> int | None:
        return self._process.poll() if self._process is not None else None

    def stats(self) -> IngestStats:
        with self._stats.lock:
            return IngestStats(
                frames=self._stats.frames,
                frames_dropped=self._frames.dropped,
                fragments=self._stats.fragments,
                fragments_dropped=self._fragments.dropped,
                fragment_bytes=self._stats.fragment_bytes,
                last_frame_at=self._stats.last_frame_at,
                started_at=self._stats.started_at,
                frames_per_second=self._stats.rate(),
            )

    def start(self) -> None:
        if self.is_running():
            raise RuntimeError("processo já está rodando")

        # O ffmpeg escreve os fragmentos num descritor extra. `pass_fds` mantém o
        # número do descritor igual no filho, então o número que o os.pipe()
        # devolveu é o mesmo que vai no argumento `pipe:N` — não precisa ser 3.
        read_fd, write_fd = os.pipe()
        self._command = build_ingest_command(
            self._url, self._decode, write_fd, ffmpeg_bin=self._ffmpeg_bin
        )

        self._stopping.clear()
        with self._stats.lock:
            self._stats.started_at = time.monotonic()

        try:
            self._process = subprocess.Popen(  # noqa: S603 - argv montado por build_ingest_command
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(write_fd,),
                bufsize=0,
            )
        except OSError:
            os.close(read_fd)
            raise
        finally:
            # A ponta de escrita precisa ser fechada no pai imediatamente: enquanto
            # ela existir aqui, o EOF nunca chega do outro lado quando o ffmpeg
            # morrer, e a thread leitora fica pendurada para sempre.
            os.close(write_fd)

        self._fragment_read_fd = read_fd
        self._start_threads()

    def _start_threads(self) -> None:
        assert self._process is not None
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        assert self._fragment_read_fd is not None

        workers = (
            ("frames-read", self._read_frames, (self._process.stdout.fileno(),)),
            ("fragments-read", self._read_fragments, (self._fragment_read_fd,)),
            ("stderr-read", self._read_stderr, (self._process.stderr,)),
            ("frames-dispatch", self._dispatch_frames, ()),
            ("fragments-dispatch", self._dispatch_fragments, ()),
        )
        self._threads = [
            threading.Thread(target=target, args=args, name=name, daemon=True)
            for name, target, args in workers
        ]
        for thread in self._threads:
            thread.start()

    # --- threads leitoras ---------------------------------------------------
    # Fazem uma coisa só: tirar bytes do pipe e despejar na fila. Nenhuma delas
    # pode chamar código do consumidor.

    def _read_frames(self, fd: int) -> None:
        frame_bytes = self._decode.frame_bytes
        sequence = 0
        try:
            while not self._stopping.is_set():
                data = read_exact(fd, frame_bytes)
                if data is None:
                    break
                now = time.monotonic()
                sequence += 1
                with self._stats.lock:
                    self._stats.frames = sequence
                    self._stats.last_frame_at = now
                    self._stats.recent_frame_times.append(now)
                self._frames.put(
                    Frame(
                        data=data,
                        width=self._decode.width,
                        height=self._decode.height,
                        pixel_format=self._decode.pixel_format,
                        sequence=sequence,
                        received_at=now,
                    )
                )
        except OSError as error:
            if not self._stopping.is_set():
                log.debug("leitura de frames encerrada: %s", error)
        finally:
            self._frames.close()

    def _read_fragments(self, fd: int) -> None:
        parser = Fmp4Parser()
        try:
            while not self._stopping.is_set():
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    break
                now = time.monotonic()
                try:
                    items = parser.feed(chunk, received_at=now)
                except Fmp4ParseError:
                    # Stream de fragmentos corrompido derruba o buffer do estágio 5,
                    # não a detecção. Encerra a leitura e deixa o supervisor decidir.
                    log.exception("stream fMP4 corrompido")
                    break
                for item in items:
                    if isinstance(item, InitSegment):
                        self._emit_init_segment(item)
                    else:
                        with self._stats.lock:
                            self._stats.fragments += 1
                            self._stats.fragment_bytes += len(item)
                        self._fragments.put(item)
        except OSError as error:
            if not self._stopping.is_set():
                log.debug("leitura de fragmentos encerrada: %s", error)
        finally:
            self._fragments.close()
            with contextlib.suppress(OSError):
                os.close(fd)
            self._fragment_read_fd = None

    def _read_stderr(self, stream) -> None:  # noqa: ANN001 - IO[bytes] do Popen
        try:
            for line in iter(stream.readline, b""):
                text = line.decode(_STDERR_ENCODING, errors="replace").rstrip()
                if not text:
                    continue
                if self._stopping.is_set():
                    # Ao receber SIGTERM o ffmpeg reclama de cada saída que não
                    # conseguiu fechar ("Immediate exit requested"). É o encerramento
                    # funcionando; logar isso encheria o log a cada reconexão e
                    # esconderia o erro que importa.
                    log.debug("ffmpeg (encerrando): %s", text)
                    continue
                if self._callbacks.on_log is not None:
                    _guarded(self._callbacks.on_log, text)
                else:
                    log.warning("ffmpeg: %s", text)
        except (OSError, ValueError) as error:
            if not self._stopping.is_set():
                log.debug("leitura de stderr encerrada: %s", error)

    # --- threads despachantes -----------------------------------------------
    # Só elas chamam código do consumidor, e podem demorar o que for.

    def _dispatch_frames(self) -> None:
        while True:
            frame = self._frames.get(timeout=0.2)
            if frame is None:
                if self._frames.closed and len(self._frames) == 0:
                    return
                continue
            if self._callbacks.on_frame is not None:
                _guarded(self._callbacks.on_frame, frame)

    def _dispatch_fragments(self) -> None:
        while True:
            fragment = self._fragments.get(timeout=0.2)
            if fragment is None:
                if self._fragments.closed and len(self._fragments) == 0:
                    return
                continue
            if self._callbacks.on_fragment is not None:
                _guarded(self._callbacks.on_fragment, fragment)

    def _emit_init_segment(self, item: InitSegment) -> None:
        if self._callbacks.on_init_segment is not None:
            _guarded(self._callbacks.on_init_segment, item)

    # --- encerramento -------------------------------------------------------

    def stop(self, grace_s: float = 3.0) -> int | None:
        """SIGTERM, espera, SIGKILL. Devolve o código de saída."""
        process = self._process
        if process is None:
            return None

        self._stopping.set()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                log.warning("ffmpeg não saiu em %.1fs, enviando SIGKILL", grace_s)
                process.kill()
                process.wait(timeout=grace_s)

        # Fechar os pipes só depois de o processo morrer: enquanto ele existir,
        # fechar a ponta de leitura faria o ffmpeg tomar EPIPE e sujar o log com um
        # erro que não aconteceu de verdade.
        self._frames.close()
        self._fragments.close()
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        if self._fragment_read_fd is not None:
            with contextlib.suppress(OSError):
                os.close(self._fragment_read_fd)
            self._fragment_read_fd = None

        for thread in self._threads:
            thread.join(timeout=_JOIN_TIMEOUT_S)
        self._threads = []
        return process.returncode

    def __enter__(self) -> FfmpegIngest:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def _guarded(callback: Callable[..., None], *args: object) -> None:
    """Uma exceção no consumidor não pode derrubar a thread que drena o pipe —
    seria exatamente o deadlock que todo este módulo existe para evitar."""
    try:
        callback(*args)
    except Exception:
        log.exception("callback de ingestão levantou exceção")


__all__ = [
    "FfmpegIngest",
    "IngestProcess",
    "IngestCallbacks",
    "IngestStats",
    "Frame",
    "Fragment",
    "InitSegment",
]
