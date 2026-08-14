"""A thread do estágio 2, e a fronteira que impede a inferência de travar a ingestão.

O frame chega pela thread despachante do ffmpeg. Essa thread não pode esperar por
inferência nenhuma: enquanto ela está ocupada, o pipe de 64 KB do processo enche, e um
pipe cheio bloqueia o ffmpeg **inteiro** — inclusive a saída de fragmentos que alimenta
o buffer do clipe, inclusive a leitura do RTSP, que passa a perder pacotes no socket.
Por isso `submit` só empurra e volta, e por isso a fila descarta o mais antigo em vez de
crescer: é a política *drop oldest* que o §3.2 prescreve, com a diferença de que aqui
sabemos de qual câmera foi o frame perdido.

**Uma thread para todas as câmeras** (ADR-007): o modelo mora uma vez na VRAM e as
câmeras dividem o mesmo turno de inferência. Batch entre câmeras — que o §3 menciona
como possível — fica de fora até haver GPU para medir se compensa (§10.2); a fila única
já deixa o terreno pronto.

O que sai daqui não vira evento. Vai para `on_result`, que hoje é um contador e amanhã é
o tracking do §3.3.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from lince_agent.config import DetectionOptions
from lince_agent.detect.detector import Detector
from lince_agent.detect.state import CameraDetectionStats, DetectionResult, DetectorStats
from lince_agent.ffmpeg.queues import DropOldestQueue
from lince_agent.ffmpeg.rawframe import Frame

log = logging.getLogger(__name__)

_ESPERA_S = 0.25
"""Quanto a thread espera por um frame antes de reavaliar se foi mandada parar. Só
afeta a latência do `stop`, nunca a do frame: `put` acorda quem está esperando."""


@dataclass(frozen=True, slots=True)
class _Pedido:
    camera_id: str
    frame: Frame


@dataclass(slots=True)
class _Contador:
    """Estado mutável por câmera. Vive sob o lock do worker."""

    frames_in: int = 0
    frames_inferred: int = 0
    dropped: int = 0
    detections: int = 0
    errors: int = 0
    last_inference_ms: float = 0.0
    last_frame_at: float | None = None
    marcos: deque[float] = field(default_factory=deque)
    """Instantes das últimas inferências, para o `inference_fps` em janela deslizante."""


class DetectorWorker:
    """Estágio 2 em pé: uma fila, uma thread e um modelo."""

    def __init__(
        self,
        detector: Detector,
        *,
        options: DetectionOptions | None = None,
        on_result: Callable[[DetectionResult], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._detector = detector
        self._options = options or DetectionOptions()
        self._on_result = on_result
        self._clock = clock

        self._fila: DropOldestQueue[_Pedido] = DropOldestQueue(self._options.queue_size)
        self._lock = threading.Lock()
        self._cameras: dict[str, _Contador] = {}
        self._errors = 0
        self._thread: threading.Thread | None = None

    # --- ciclo de vida ------------------------------------------------------

    def register(self, camera_id: str) -> None:
        """Faz a câmera aparecer no heartbeat antes do primeiro frame.

        Sem isto, uma câmera que nunca entregou nada — a que mais interessa ao alerta
        técnico do §5.3 — seria indistinguível de uma câmera que não existe.
        """
        with self._lock:
            self._cameras.setdefault(camera_id, _Contador())

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("o detector já está rodando")
        self._thread = threading.Thread(target=self._run, name="deteccao", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Fecha a fila e espera a thread. O que já estava na fila é drenado: são no
        máximo `queue_size` frames, e descartá-los na parada esconderia trabalho que
        estava pronto."""
        self._fila.close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():
                log.warning("thread de detecção não terminou em %.1fs", timeout)
        self._detector.close()

    # --- entrada ------------------------------------------------------------

    def submit(self, camera_id: str, frame: Frame) -> None:
        """Enfileira um frame. **Nunca bloqueia** — ver o docstring do módulo."""
        descartado = self._fila.put(_Pedido(camera_id, frame))
        with self._lock:
            contador = self._cameras.setdefault(camera_id, _Contador())
            contador.frames_in += 1
            contador.last_frame_at = frame.received_at
            if descartado is not None:
                self._cameras.setdefault(descartado.camera_id, _Contador()).dropped += 1

    # --- processamento ------------------------------------------------------

    def _run(self) -> None:
        while True:
            pedido = self._fila.get(timeout=_ESPERA_S)
            if pedido is None:
                if self._fila.closed:
                    return
                continue
            self._infere(pedido)

    def tick(self, timeout: float = 0.0) -> bool:
        """Processa no máximo um frame, de forma síncrona. Devolve se havia trabalho.

        Público e síncrono pelo mesmo motivo que o `OutboxSender.tick`: é o que permite
        testar saturação, erro de inferência e contagem sem thread, sem `sleep` e sem
        corrida — e teste de pipeline que depende de tempo real fica instável em CI.
        """
        pedido = self._fila.get(timeout=timeout)
        if pedido is None:
            return False
        self._infere(pedido)
        return True

    def _infere(self, pedido: _Pedido) -> None:
        inicio = self._clock()
        try:
            deteccoes = self._detector.detect(pedido.frame)
        except Exception:
            # Falha de inferência numa câmera não pode parar as outras: a thread é uma
            # só para o agente inteiro (ADR-007), e morrer aqui apagaria a detecção da
            # loja toda por causa de um frame corrompido.
            log.exception("inferência falhou na câmera %s", pedido.camera_id)
            with self._lock:
                self._errors += 1
                self._cameras.setdefault(pedido.camera_id, _Contador()).errors += 1
            return

        fim = self._clock()
        with self._lock:
            contador = self._cameras.setdefault(pedido.camera_id, _Contador())
            contador.frames_inferred += 1
            contador.detections += len(deteccoes)
            contador.last_inference_ms = (fim - inicio) * 1000.0
            contador.marcos.append(fim)
            while len(contador.marcos) > self._options.fps_window:
                contador.marcos.popleft()

        if self._on_result is None:
            return
        resultado = DetectionResult(
            camera_id=pedido.camera_id,
            sequence=pedido.frame.sequence,
            received_at=pedido.frame.received_at,
            detections=deteccoes,
            inference_ms=(fim - inicio) * 1000.0,
        )
        try:
            self._on_result(resultado)
        except Exception:
            # O consumidor é o estágio 3 (§3.3). Deixá-lo derrubar esta thread daria o
            # mesmo estrago que uma inferência ruim: a loja inteira para de detectar.
            log.exception("consumidor da detecção falhou na câmera %s", pedido.camera_id)
            with self._lock:
                self._errors += 1

    # --- observação ---------------------------------------------------------

    def stats(self) -> DetectorStats:
        agora = self._clock()
        with self._lock:
            cameras = tuple(
                CameraDetectionStats(
                    camera_id=camera_id,
                    frames_in=contador.frames_in,
                    frames_inferred=contador.frames_inferred,
                    dropped=contador.dropped,
                    detections=contador.detections,
                    errors=contador.errors,
                    last_inference_ms=contador.last_inference_ms,
                    inference_fps=_fps(contador.marcos, agora),
                    last_frame_at=contador.last_frame_at,
                )
                for camera_id, contador in sorted(self._cameras.items())
            )
            errors = self._errors

        return DetectorStats(
            enabled=self._detector.info() is not None,
            info=self._detector.info(),
            queue_depth=len(self._fila),
            queue_size=self._options.queue_size,
            errors=errors,
            cameras=cameras,
        )


def _fps(marcos: deque[float], agora: float) -> float:
    """Taxa de inferência na janela, contando o tempo desde a última.

    Incluir `agora` é o que faz o número **cair** quando a câmera para de entregar. Com
    a janela sozinha, uma câmera que morreu há dez minutos continuaria reportando a taxa
    que tinha no instante em que morreu — e o §5.3 usa este campo justamente para
    detectar box saturado e câmera degradada.
    """
    if len(marcos) < 2:
        return 0.0
    intervalo = max(agora, marcos[-1]) - marcos[0]
    if intervalo <= 0:
        return 0.0
    return (len(marcos) - 1) / intervalo
