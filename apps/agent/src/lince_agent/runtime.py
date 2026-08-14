"""Composição do agente: onde os cinco estágios viram um processo só.

Até aqui cada estágio existia isolado e testável. Isto é o que os liga:

    ffmpeg ──fragmentos──> ClipBuffer ──┐
       │                                │  (por câmera)
    CameraSupervisor                    │
                                        ▼
    gatilho ──> ClipRecorder ──ClipResult──> fila local ──> nuvem

Três decisões moram aqui, e nenhuma cabia nos estágios:

**O `event_id` nasce no gatilho, não no envio.** Ele é a chave de idempotência do
§5.4 e o nome do arquivo do clipe. Gerado depois, o reenvio criaria duplicata; gerado
antes, o clipe e o evento não se encontrariam.

**O rascunho sobrevive à espera do pós-roll.** Entre o gatilho e o `ClipResult` passam
uns 15 s, e o resultado não carrega nada do lado da regra — nem qual regra disparou,
nem o instante de parede. O `EventDraft` guarda essa metade.

**Gatilho recusado ainda vira evento.** Se a fila do recorder estiver cheia (rajada,
R-1), o alerta sobe sem vídeo. Perder o alerta junto com o clipe seria trocar um
problema pequeno por um grande.

Este módulo é código de produção e tem teste próprio; o `__main__.py` só monta a
configuração e chama o que está aqui.
"""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from lince_agent import __version__
from lince_agent.clip.buffer import ClipBuffer
from lince_agent.clip.recorder import ClipRecorder
from lince_agent.clip.state import BufferStats, ClipResult, ClipStatus, RecorderStats
from lince_agent.clip.store import ClipStore
from lince_agent.config import AgentConfig, CameraConfig
from lince_agent.detect.detector import Detector, NullDetector
from lince_agent.detect.state import DetectionResult, DetectorStats
from lince_agent.detect.worker import DetectorWorker
from lince_agent.ffmpeg.process import IngestCallbacks
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.ingest.state import CameraHealth
from lince_agent.ingest.supervisor import CameraSupervisor
from lince_agent.outbox.clock import MonotonicAnchor
from lince_agent.outbox.event import (
    AgentIdentity,
    EventDraft,
    EventSource,
    build_event_payload,
    new_event_id,
)
from lince_agent.outbox.http import CloudClient
from lince_agent.outbox.sender import OutboxSender
from lince_agent.outbox.state import ClipState, ItemKind, OutboxStats, QueueItem, SenderStats
from lince_agent.outbox.store import OutboxStore

log = logging.getLogger(__name__)

MAX_RASCUNHOS = 64
"""Gatilhos esperando o clipe. Precisa ser maior que o `pending_requests` do recorder,
senão uma rajada descartaria o rascunho de um clipe que ainda vai chegar — e aí o
`ClipResult` apareceria órfão, sem regra e sem instante."""


@dataclass(frozen=True, slots=True)
class AgentHealth:
    """Tudo que o agente sabe sobre si. É o corpo do heartbeat do §5.3 antes de existir
    heartbeat: os mesmos campos, coletados no mesmo lugar, esperando só o transporte."""

    cameras: tuple[CameraHealth, ...] = ()
    buffers: tuple[BufferStats, ...] = ()
    detector: DetectorStats = field(default_factory=DetectorStats)
    recorder: RecorderStats = field(default_factory=RecorderStats)
    outbox: OutboxStats = field(default_factory=OutboxStats)
    sender: SenderStats = field(default_factory=SenderStats)
    uptime_s: float = 0.0
    frames_ingested: int = 0
    """Frames que chegaram da ingestão, contando os das câmeras que o R-3 marcou como
    inelegíveis para IA. Comparado com `detector.frames_in`, mostra quanto do decode
    está sendo pago sem virar inferência — que é o dimensionamento do §3.1."""

    clip_disk_bytes: int = 0
    clock_reanchors: int = 0
    enqueue_failures: int = 0
    """Eventos que o corte produziu e a fila local recusou. Deveria ser sempre zero:
    diferente de zero significa Redis local fora do ar, e aí o evento se perdeu de
    verdade — é a fronteira de durabilidade que o ADR-004 assume."""


class AgentRuntime:
    """O agente da loja, montado e em pé."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        store: OutboxStore,
        client: CloudClient,
        clip_store: ClipStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = new_event_id,
        supervisor_factory: Callable[[CameraConfig, IngestCallbacks], CameraSupervisor]
        | None = None,
        recorder: ClipRecorder | None = None,
        sender: OutboxSender | None = None,
        detector: Detector | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._clock = clock
        self._wall_clock = wall_clock
        self._id_factory = id_factory
        self._supervisor_factory = supervisor_factory or self._monta_supervisor

        self._anchor = MonotonicAnchor(clock=clock, wall_clock=wall_clock)
        self._identity = AgentIdentity(
            tenant_id=config.tenant_id,
            store_id=config.store_id,
            agent_version=__version__,
        )
        self._clip_store = clip_store or ClipStore(
            config.clips_dir,
            max_bytes=config.clip.max_disk_bytes,
            on_evict=self._ao_despejar_clipe,
        )
        self._recorder = recorder or ClipRecorder(
            config.clip,
            self._clip_store,
            clock=clock,
            on_result=self._ao_terminar_clipe,
        )
        self._sender = sender or OutboxSender(
            store,
            client,
            options=config.outbox,
            clip_store=self._clip_store,
            anchor=self._anchor,
            wall_clock=wall_clock,
        )

        self._detector = detector or _monta_detector(config)
        self._deteccao = DetectorWorker(
            self._detector,
            options=config.detection,
            on_result=self._ao_detectar,
            clock=clock,
        )

        self._lock = threading.Lock()
        self._detecta = {camera.camera_id: camera.detect for camera in config.cameras}
        """Elegibilidade para IA por câmera (R-3). A câmera inelegível continua
        ingerindo e alimentando o buffer do clipe; só não gasta inferência."""

        self._supervisores: dict[str, CameraSupervisor] = {}
        self._buffers: dict[str, ClipBuffer] = {}
        self._rascunhos: dict[str, EventDraft] = {}
        self._frames = 0
        self._enqueue_failures = 0
        self._iniciado_em: float | None = None

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        if self._iniciado_em is not None:
            raise RuntimeError("o agente já está rodando")

        self._supervisores.clear()
        self._buffers.clear()
        for camera in self._config.cameras:
            # É aqui que `CameraConfig.clip` deixa de ser campo morto: cada câmera tem
            # a própria janela e o próprio teto de RAM, porque uma câmera de saída com
            # bitrate alto não pode espremer o buffer das outras (§3.5, R-4).
            buffer = ClipBuffer(camera.camera_id, camera.clip)
            # O `on_frame` da ingestão entrega só o `Frame`, que não sabe de que câmera
            # veio — a origem é o supervisor que está sendo montado agora. Amarrar o
            # `camera_id` aqui é o que permite ao §5.3 separar `inference_fps` e
            # `dropped_frames` por câmera com uma fila de detecção só (ADR-007).
            callbacks = IngestCallbacks(
                on_frame=functools.partial(self._ao_receber_frame, camera.camera_id),
                on_init_segment=buffer.on_init_segment,
                on_fragment=buffer.on_fragment,
            )
            self._buffers[camera.camera_id] = buffer
            self._supervisores[camera.camera_id] = self._supervisor_factory(camera, callbacks)
            self._recorder.attach(camera.camera_id, buffer)
            if camera.detect:
                self._deteccao.register(camera.camera_id)

        # Consumidores antes de produtores: um clipe cortado antes de o sender existir
        # ficaria esperando o próximo poll ocioso, e o NFR-1 já gastou 10 s no pós-roll.
        self._sender.start()
        self._recorder.start()
        self._deteccao.start()
        for supervisor in self._supervisores.values():
            supervisor.start()

        self._iniciado_em = self._clock()

    def stop(self, timeout: float = 30.0) -> None:
        """Desmonta na ordem inversa, para o que acabou de ser cortado ainda subir.

        Os supervisores e buffers **não** são esquecidos: `health()` depois do `stop()`
        é o resumo que vai para o log de encerramento e para quem estiver depurando um
        agente que acabou de cair. Zerar os contadores na saída faria toda parada
        limpa parecer uma execução que nunca ingeriu nada.
        """
        for supervisor in self._supervisores.values():
            supervisor.stop(timeout=timeout)
        self._deteccao.stop(timeout=timeout)
        self._recorder.stop(timeout=timeout)
        self._sender.stop(timeout=timeout)
        self._iniciado_em = None

    # --- gatilho ------------------------------------------------------------

    def trigger(
        self,
        camera_id: str,
        *,
        source: EventSource = EventSource.MANUAL,
        rule_id: str | None = None,
        rule_version: int | None = None,
        at: float | None = None,
    ) -> str:
        """Registra uma possível ocorrência e devolve o `event_id`.

        Chamado pelo motor de regras (§3.4) quando ele existir, e hoje pelo andaime da
        CLI. Não bloqueia: quem chama é a thread despachante do ffmpeg, e segurá-la por
        10 s esperando pós-roll travaria a leitura do RTSP de todas as câmeras.
        """
        if camera_id not in self._buffers:
            raise KeyError(f"câmera desconhecida: {camera_id!r}")

        instante = self._clock() if at is None else at
        rascunho = EventDraft(
            event_id=self._id_factory(),
            camera_id=camera_id,
            triggered_at=instante,
            occurred_at=self._anchor.to_iso(instante),
            source=source,
            rule_id=rule_id,
            rule_version=rule_version,
        )
        self._guarda_rascunho(rascunho)

        if not self._recorder.trigger(camera_id, event_id=rascunho.event_id, at=instante):
            # Fila de cortes cheia: rajada numa câmera mal calibrada (R-1). O vídeo se
            # perde, o alerta não.
            log.warning(
                "recorder ocupado; evento %s sobe sem clipe (rajada na câmera %s?)",
                rascunho.event_id,
                camera_id,
            )
            self._ao_terminar_clipe(
                ClipResult(
                    event_id=rascunho.event_id,
                    camera_id=camera_id,
                    status=ClipStatus.CLIP_FAILED,
                    triggered_at=instante,
                    error="fila de cortes cheia",
                )
            )
        return rascunho.event_id

    # --- saída do estágio 5 -------------------------------------------------

    def _ao_terminar_clipe(self, result: ClipResult) -> None:
        """Recebe o clipe e enfileira o evento. Roda na thread do recorder.

        Nenhuma rede acontece aqui: o `enqueue` fala com o Redis local, com timeout
        curto, e o envio é assunto de outra thread. Bloquear esta seguraria o corte do
        próximo clipe.
        """
        with self._lock:
            rascunho = self._rascunhos.pop(result.event_id, None)

        if rascunho is None:
            log.error(
                "clipe %s chegou sem rascunho: o evento não pode ser montado e o vídeo "
                "vai ser descartado. Rascunhos demais em voo?",
                result.event_id,
            )
            self._clip_store.discard(result.event_id)
            return

        payload = build_event_payload(
            rascunho, result, identity=self._identity, reported_at=self._anchor.now_iso()
        )
        tem_clipe = result.status is ClipStatus.OK and result.path is not None
        item = QueueItem(
            event_id=result.event_id,
            kind=ItemKind.EVENT,
            payload=payload,
            created_at_ms=int(self._wall_clock() * 1000),
            clip_state=ClipState.PENDING if tem_clipe else ClipState.NONE,
            clip_path=str(result.path) if tem_clipe else None,
            clip_size_bytes=result.size_bytes,
        )

        if not self._enfileira(item):
            return
        self._sender.notify()

    def _enfileira(self, item: QueueItem) -> bool:
        try:
            self._store.enqueue(item)
        except Exception:  # noqa: BLE001 - ver abaixo
            # Redis local fora do ar. É a fronteira de durabilidade assumida no
            # ADR-004: o evento se perde, mas de forma **visível** — contador no
            # heartbeat e ERROR no log. Um `except: pass` aqui produziria uma loja que
            # não alerta e não reclama.
            log.exception("fila local recusou o evento %s: o alerta se perdeu", item.event_id)
            with self._lock:
                self._enqueue_failures += 1
            self._clip_store.discard(item.event_id)
            return False
        return True

    def _ao_despejar_clipe(self, event_id: str) -> None:
        """O teto de disco apagou um clipe que ainda não subiu (§3.6).

        Chamado pelo `ClipStore`. Sem isto o sender só descobriria na hora do upload,
        depois de gastar tentativas com um arquivo que não existe.
        """
        log.warning("clipe de %s despejado pelo teto de disco antes de subir", event_id)
        self._store.give_up_clip(event_id, reason="teto de disco de clipes")

    def _ao_receber_frame(self, camera_id: str, frame: Frame) -> None:
        """Entrega o frame ao estágio 2. Roda na thread despachante do ffmpeg.

        `submit` não bloqueia, e não pode: enquanto esta thread não volta para o pipe,
        o buffer de 64 KB do kernel enche e o ffmpeg trava **inteiro** — junto com a
        saída de fragmentos que alimenta o buffer do clipe e com a leitura do RTSP.
        Quando a inferência não acompanha, quem paga é a fila do §3.2, descartando o
        frame mais antigo.
        """
        with self._lock:
            self._frames += 1
        if self._detecta.get(camera_id, True):
            self._deteccao.submit(camera_id, frame)

    def _ao_detectar(self, resultado: DetectionResult) -> None:
        """Recebe as caixas de um frame. Roda na thread de detecção.

        Por enquanto só passa. **É onde o estágio 3 (tracking, §3.3) entra** — e daí o
        §3.4 chama `trigger` com `source=RULE`, que é o que enfim tira o andaime da CLI
        do caminho. Nada mais precisa mudar para o evento chegar à nuvem.
        """

    def _guarda_rascunho(self, rascunho: EventDraft) -> None:
        with self._lock:
            self._rascunhos[rascunho.event_id] = rascunho
            excedente = len(self._rascunhos) - MAX_RASCUNHOS
            for event_id in list(self._rascunhos)[:excedente]:
                log.error("rascunho %s descartado: gatilhos demais sem clipe", event_id)
                del self._rascunhos[event_id]

    # --- observação ---------------------------------------------------------

    def _monta_supervisor(
        self, camera: CameraConfig, callbacks: IngestCallbacks
    ) -> CameraSupervisor:
        return CameraSupervisor(camera, callbacks, ffmpeg_bin=self._config.ffmpeg_bin)

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(self._supervisores)

    def health(self) -> AgentHealth:
        agora_ms = int(self._wall_clock() * 1000)
        with self._lock:
            falhas = self._enqueue_failures
            inicio = self._iniciado_em
            frames = self._frames
        return AgentHealth(
            cameras=tuple(sup.health() for sup in self._supervisores.values()),
            buffers=tuple(buffer.stats() for buffer in self._buffers.values()),
            detector=self._deteccao.stats(),
            frames_ingested=frames,
            recorder=self._recorder.stats(),
            outbox=self._store.stats(now_ms=agora_ms),
            sender=self._sender.stats(),
            uptime_s=0.0 if inicio is None else self._clock() - inicio,
            clip_disk_bytes=self._clip_store.usage_bytes(),
            clock_reanchors=self._anchor.reanchors,
            enqueue_failures=falhas,
        )


def _monta_detector(config: AgentConfig) -> Detector:
    """Escolhe o detector a partir da configuração, sem `if gpu` em lugar nenhum.

    Sem modelo no disco o agente sobe assim mesmo, com `NullDetector`: continua
    ingerindo, mantendo o buffer circular e respondendo a gatilho manual. É o que
    mantém o §3.9 verdadeiro — o mesmo artefato roda numa máquina de desenvolvimento
    sem GPU e no box da loja, e o que muda é configuração externa.

    Um modelo que não abre **derruba a subida** em vez de degradar para `NullDetector`.
    Um agente que ingere, grava clipe e nunca detecta nada é pior que um que não sobe:
    ele parece saudável no heartbeat e some do radar até alguém reparar que aquela loja
    nunca alertou.
    """
    if not config.detection.enabled or config.detection.model_path is None:
        return NullDetector()

    # Import adiado: `onnxruntime` leva perto de um segundo para carregar e reserva
    # arenas de memória. Um agente com a detecção desligada não deve pagar por isso.
    from lince_agent.detect.onnx import OnnxDetector

    return OnnxDetector(config.detection.model_path, config.detection)
