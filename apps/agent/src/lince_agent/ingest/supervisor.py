"""Supervisão de uma câmera: reconexão e watchdog.

Tudo aqui existe porque o ffmpeg não faz nada disso sozinho numa entrada RTSP.

**`-reconnect` não funciona com RTSP.** `-reconnect`, `-reconnect_streamed` e
`-reconnect_delay_max` são opções do protocolo HTTP/HTTPS. Passá-las numa entrada
`rtsp://` não dá erro e não tem efeito nenhum — é a pegadinha mais comum desta
área, e a razão de toda a reconexão do §3.1 ser código Python.

São duas falhas distintas, com detecções distintas:

| Falha                        | Detecção                       |
|------------------------------|--------------------------------|
| Câmera offline, RTSP recusou | o processo morre               |
| Stream travado sem fechar    | nenhum frame novo por N s      |

A segunda é a que engana: o socket continua vivo, o `-timeout` do demuxer não
dispara, o ffmpeg fica feliz e nenhum frame sai. Só a ausência de frame denuncia.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable

from lince_agent.config import CameraConfig, SupervisionOptions
from lince_agent.ffmpeg.process import FfmpegIngest, IngestCallbacks, IngestProcess
from lince_agent.ingest.state import CameraHealth, CameraStatus

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.25


def backoff_delay(
    consecutive_failures: int,
    options: SupervisionOptions,
    *,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Backoff exponencial com jitter aditivo, com teto.

    O jitter não é enfeite: quando o switch da loja cai, as oito câmeras falham no
    mesmo instante. Sem jitter elas voltam no mesmo instante, batem no mesmo
    instante e repetem o padrão a cada tentativa.
    """
    if consecutive_failures <= 0:
        return 0.0
    exponential = options.backoff_base_s * (2 ** (consecutive_failures - 1))
    return min(exponential, options.backoff_cap_s) + jitter() * options.backoff_jitter_s


class CameraSupervisor:
    """Mantém uma câmera ingerindo, custe o que custar.

    A regra do §5.4 vale aqui também: nenhuma falha pode parar a detecção das
    outras câmeras. Uma exceção nesta thread derruba esta câmera e só ela.
    """

    def __init__(
        self,
        config: CameraConfig,
        callbacks: IngestCallbacks | None = None,
        *,
        ffmpeg_bin: str = "ffmpeg",
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
        ingest_factory: Callable[[], IngestProcess] | None = None,
    ) -> None:
        self._config = config
        self._callbacks = callbacks or IngestCallbacks()
        self._ffmpeg_bin = ffmpeg_bin
        self._clock = clock
        self._jitter = jitter
        # Injetável para os testes: com um dublê e um relógio falso, as transições
        # de saúde e o watchdog são exercitados em milissegundos e sem rede.
        self._ingest_factory = ingest_factory or self._build_ingest

        self._status = CameraStatus.STOPPED
        self._consecutive_failures = 0
        self._starts = 0
        """Quantas vezes o ffmpeg foi executado. `restarts` é este número menos a
        subida inicial: uma câmera saudável precisa reportar zero, senão a métrica
        de crash loop do §5.3 nasce com ruído fixo."""
        self._last_error: str | None = None
        self._ingest: IngestProcess | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _build_ingest(self) -> IngestProcess:
        return FfmpegIngest(
            self._config.url,
            self._config.decode,
            self._callbacks,
            ffmpeg_bin=self._ffmpeg_bin,
        )

    @property
    def camera_id(self) -> str:
        return self._config.camera_id

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError(f"supervisor de {self.camera_id} já está rodando")
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"supervisor-{self.camera_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        self._stop_event.set()
        with self._lock:
            ingest = self._ingest
        if ingest is not None:
            ingest.stop(grace_s=self._config.supervision.stop_grace_s)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._set_status(CameraStatus.STOPPED)

    def health(self) -> CameraHealth:
        with self._lock:
            ingest = self._ingest
            status = self._status
            failures = self._consecutive_failures
            restarts = max(0, self._starts - 1)
            last_error = self._last_error

        stats = ingest.stats() if ingest is not None else None
        return CameraHealth(
            camera_id=self.camera_id,
            status=status,
            consecutive_failures=failures,
            restarts=restarts,
            sampled_fps=stats.frames_per_second if stats else 0.0,
            frames=stats.frames if stats else 0,
            frames_dropped=stats.frames_dropped if stats else 0,
            fragments=stats.fragments if stats else 0,
            fragments_dropped=stats.fragments_dropped if stats else 0,
            fragment_bytes=stats.fragment_bytes if stats else 0,
            last_frame_at=stats.last_frame_at if stats else None,
            last_error=last_error,
        )

    # --- laço principal -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            reason = self._run_once()
            if self._stop_event.is_set():
                break

            with self._lock:
                self._consecutive_failures += 1
                failures = self._consecutive_failures
                self._last_error = reason

            options = self._config.supervision
            self._set_status(
                CameraStatus.OFFLINE
                if failures >= options.offline_after_failures
                else CameraStatus.RECONNECTING
            )

            delay = backoff_delay(failures, options, jitter=self._jitter)
            log.warning(
                "câmera %s caiu (%s); tentativa %d em %.1fs",
                self.camera_id,
                reason,
                failures,
                delay,
            )
            # `wait` em vez de `sleep`: um `stop()` durante um backoff de 60 s não
            # pode esperar os 60 s.
            self._stop_event.wait(delay)

        self._set_status(CameraStatus.STOPPED)

    def _run_once(self) -> str:
        """Sobe o ffmpeg e vigia até ele morrer, travar ou receber `stop()`.

        Devolve o motivo do encerramento.
        """
        ingest = self._ingest_factory()
        with self._lock:
            self._ingest = ingest
            self._starts += 1

        self._set_status(CameraStatus.STARTING)
        try:
            ingest.start()
        except OSError as error:
            return f"falha ao executar o ffmpeg: {error}"

        log.info("câmera %s: %s", self.camera_id, " ".join(ingest.command))
        try:
            return self._watch(ingest)
        finally:
            ingest.stop(grace_s=self._config.supervision.stop_grace_s)

    def _watch(self, ingest: IngestProcess) -> str:
        options = self._config.supervision
        saw_first_frame = False

        while not self._stop_event.is_set():
            if not ingest.is_running():
                return f"processo saiu com código {ingest.returncode}"

            stats = ingest.stats()
            if not saw_first_frame and stats.frames > 0:
                saw_first_frame = True
                # A conexão só conta como boa depois do primeiro frame. Um ffmpeg
                # que sobe, conecta e nunca entrega nada não é uma câmera saudável.
                with self._lock:
                    self._consecutive_failures = 0
                self._set_status(CameraStatus.OK)

            # Antes do primeiro frame o relógio de referência é a subida do
            # processo: é isso que dá um teto ao tempo de conexão.
            reference = stats.last_frame_at if stats.last_frame_at is not None else stats.started_at
            if reference and self._clock() - reference > options.stall_timeout_s:
                return (
                    f"sem frame novo há mais de {options.stall_timeout_s:.0f}s "
                    f"(stream travado com socket vivo)"
                )

            self._stop_event.wait(_POLL_INTERVAL_S)

        return "encerrado a pedido"

    def _set_status(self, status: CameraStatus) -> None:
        with self._lock:
            if self._status is status:
                return
            previous = self._status
            self._status = status
        log.info("câmera %s: %s → %s", self.camera_id, previous, status)
