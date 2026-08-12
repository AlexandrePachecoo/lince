"""Orquestração do corte do clipe (§3.5).

O caminho de um gatilho:

    estágio 4 ──trigger()──> fila limitada ──> thread do recorder
                  (µs)                            │
                                                  ├─ espera o pós-roll (Condition)
                                                  ├─ seleciona a janela (timeline.py)
                                                  ├─ remuxa (remux.py)
                                                  └─ ClipResult ──> estágio 6

**`trigger()` não bloqueia, e isso não é otimização.** Ele será chamado pelo motor
de regras, que roda na thread despachante do `FfmpegIngest` — a mesma que alimenta a
detecção. Segurá-la por 10 s esperando pós-roll encheria a `DropOldestQueue`, e o
`ffmpeg/queues.py` é explícito: consumidor lento não pode virar contrapressão no
ffmpeg, sob pena de travar a leitura do RTSP de **todas** as câmeras daquele
processo. Por isso o gatilho só congela o buffer e enfileira.

**Uma thread para o agente inteiro.** Dois eventos simultâneos serializam. Num box
de poucos núcleos, cortes em paralelo disputariam o mesmo CPU e o mesmo disco de
qualquer forma, e o prazo do remux limita o bloqueio de cabeça de fila.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from lince_agent.clip.buffer import ClipBuffer
from lince_agent.clip.remux import Remuxer, RemuxError, remux_to_mp4
from lince_agent.clip.state import ClipResult, ClipStatus, RecorderStats
from lince_agent.clip.store import ClipStore
from lince_agent.clip.timeline import estimate_media_offset, measure_rolls, select_window
from lince_agent.config import ClipOptions
from lince_agent.ffmpeg.fmp4 import Fragment, InitSegment

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.2
_TOLERANCIA_S = 0.01
"""Folga ao comparar roll medido com roll pedido. Sem ela, ruído de ponto flutuante
marcaria `truncated` num clipe perfeito."""


@dataclass(frozen=True, slots=True)
class ClipRequest:
    """Um gatilho com o buffer já congelado.

    O congelamento acontece dentro do `trigger()` e é o que protege o pré-roll: a
    poda continua rodando durante os 10 s de espera do pós-roll e, sem isto, comeria
    justamente os fragmentos que o clipe precisa. Custa uma tupla de ponteiros —
    os bytes são imutáveis e não são copiados.
    """

    event_id: str
    camera_id: str
    triggered_at: float
    init: InitSegment | None
    fragments: tuple[Fragment, ...]
    session_id: int | None


class ClipRecorder:
    """Transforma gatilhos em arquivos MP4, uma thread para todas as câmeras."""

    def __init__(
        self,
        options: ClipOptions | None = None,
        store: ClipStore | None = None,
        *,
        remuxer: Remuxer = remux_to_mp4,
        clock: Callable[[], float] = time.monotonic,
        on_result: Callable[[ClipResult], None] | None = None,
    ) -> None:
        if store is None:
            raise ValueError("o recorder precisa de um ClipStore")
        self._options = options or ClipOptions()
        self._store = store
        self._remuxer = remuxer
        self._clock = clock
        self._on_result = on_result

        self._buffers: dict[str, ClipBuffer] = {}
        self._requests: queue.Queue[ClipRequest] = queue.Queue(
            maxsize=self._options.pending_requests
        )
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._requested = 0
        self._completed = 0
        self._failed = 0
        self._dropped = 0

    def attach(self, camera_id: str, buffer: ClipBuffer) -> None:
        with self._lock:
            self._buffers[camera_id] = buffer

    def _buffer(self, camera_id: str) -> ClipBuffer:
        """O buffer da câmera, sempre sob o lock.

        Na prática o `attach` acontece na montagem do agente e o dicionário não muda
        mais, mas o acesso vem de duas threads — a despachante, no `trigger`, e a do
        recorder, durante o corte. Ler sem o lock aqui e com ele ali é o tipo de
        assimetria que sobrevive até alguém acrescentar `detach` para uma câmera
        removida em runtime.
        """
        with self._lock:
            buffer = self._buffers.get(camera_id)
        if buffer is None:
            raise KeyError(f"nenhum buffer registrado para a câmera {camera_id!r}")
        return buffer

    # --- gatilho: chamado de qualquer thread, inclusive a despachante -------

    def trigger(self, camera_id: str, *, event_id: str, at: float) -> bool:
        """Congela o buffer e enfileira o corte. Devolve se o pedido foi aceito.

        Nunca bloqueia e nunca levanta por fila cheia: uma rajada de gatilhos — que
        é o sintoma de câmera mal calibrada, R-1 — não pode crescer memória nem
        segurar o motor de regras. Ela descarta e conta.
        """
        init, fragments, session_id = self._buffer(camera_id).snapshot()
        request = ClipRequest(
            event_id=event_id,
            camera_id=camera_id,
            triggered_at=at,
            init=init,
            fragments=fragments,
            session_id=session_id,
        )

        with self._lock:
            self._requested += 1
        try:
            self._requests.put_nowait(request)
        except queue.Full:
            with self._lock:
                self._dropped += 1
            log.warning(
                "fila de clipes cheia (%d): evento %s da câmera %s fica sem vídeo",
                self._options.pending_requests,
                event_id,
                camera_id,
            )
            return False
        return True

    # --- ciclo de vida -------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("o recorder já está rodando")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="clip-recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        """Encerra depois de terminar o corte em andamento.

        O pedido em voo é levado até o fim porque ele representa um evento real. Os
        que ainda estão na fila se perdem — enquanto o estágio 6 não existe, não há
        onde persistir gatilho entre execuções, e fingir o contrário seria pior.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("thread do recorder não encerrou em %.0fs", timeout)
            self._thread = None

    def stats(self) -> RecorderStats:
        with self._lock:
            return RecorderStats(
                requested=self._requested,
                completed=self._completed,
                failed=self._failed,
                dropped=self._dropped,
                pending=self._requests.qsize(),
            )

    # --- thread trabalhadora -------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                request = self._requests.get(timeout=_POLL_INTERVAL_S)
            except queue.Empty:
                continue
            try:
                resultado = self._processa(request)
            except Exception:  # noqa: BLE001 - ver o comentário abaixo
                # Uma câmera não pode derrubar o corte das outras. É a mesma regra
                # do supervisor: falha isolada degrada só o que ela alcança.
                log.exception("falha inesperada ao cortar o clipe de %s", request.event_id)
                resultado = self._falhou(request, "erro inesperado no corte")
            self._contabiliza(resultado)
            self._notifica(resultado)

    def _processa(self, request: ClipRequest) -> ClipResult:
        if request.init is None or not request.fragments or request.session_id is None:
            # Reinício do container ou gatilho logo após uma reconexão (§3.5): o
            # buffer em RAM não existe. O evento sobe sem clipe.
            return self._falhou(request, "buffer vazio no instante do gatilho")

        offset = self._offset(request.fragments)
        trigger_media_s = request.triggered_at + offset
        fim_desejado = trigger_media_s + self._options.post_roll_s

        cobriu = self._espera_pos_roll(request, fim_desejado)
        cauda, session_lost = self._cauda(request)
        todos = request.fragments + cauda

        inicio_desejado = trigger_media_s - self._options.pre_roll_s
        primeiro, ultimo = select_window(todos, inicio_desejado, fim_desejado)
        pre_roll_s, post_roll_s = measure_rolls(todos, primeiro, ultimo, trigger_media_s)
        selecionados = todos[primeiro : ultimo + 1]

        destino = self._store.path_for(request.event_id)
        dados = request.init.data + b"".join(f.data for f in selecionados)
        try:
            self._remuxer(dados, destino, timeout_s=self._options.remux_timeout_s)
            self._store.register(destino)
        except (RemuxError, OSError) as error:
            self._store.discard(request.event_id)
            return self._falhou(request, str(error))

        return ClipResult(
            event_id=request.event_id,
            camera_id=request.camera_id,
            status=ClipStatus.OK,
            triggered_at=request.triggered_at,
            path=destino,
            size_bytes=destino.stat().st_size,
            duration_s=pre_roll_s + post_roll_s,
            pre_roll_s=pre_roll_s,
            post_roll_s=post_roll_s,
            pre_roll_requested_s=self._options.pre_roll_s,
            post_roll_requested_s=self._options.post_roll_s,
            fragments=len(selecionados),
            truncated_pre_roll=pre_roll_s < self._options.pre_roll_s - _TOLERANCIA_S,
            truncated_post_roll=(
                not cobriu or post_roll_s < self._options.post_roll_s - _TOLERANCIA_S
            ),
            session_lost=session_lost,
        )

    def _offset(self, fragments: tuple[Fragment, ...]) -> float:
        """Conversão do relógio do gatilho para o timeline de mídia.

        Com um único fragmento o estimador se recusa a responder (ver
        `timeline.estimate_media_offset`), e aqui cai-se no pareamento ingênuo, que
        embute um GOP de viés. Só acontece no primeiro segundo depois de uma
        (re)conexão, quando o clipe já vai sair curto e marcado de qualquer jeito —
        errar o recorte de um GOP num clipe que só tem um GOP não muda nada.
        """
        offset = estimate_media_offset(fragments)
        if offset is not None:
            return offset
        unico = fragments[-1]
        return unico.start_seconds - unico.received_at

    def _espera_pos_roll(self, request: ClipRequest, fim_desejado: float) -> bool:
        assert request.session_id is not None
        buffer = self._buffer(request.camera_id)
        # O prazo conta do gatilho, não de agora: o pedido pode ter esperado na fila
        # atrás de outro corte, e esse tempo já consumiu parte do pós-roll.
        decorrido = max(0.0, self._clock() - request.triggered_at)
        prazo = self._options.post_roll_s + self._options.post_roll_grace_s - decorrido
        if prazo <= 0:
            return False
        return buffer.wait_for_media_time(request.session_id, fim_desejado, prazo)

    def _cauda(self, request: ClipRequest) -> tuple[tuple[Fragment, ...], bool]:
        assert request.session_id is not None
        buffer = self._buffer(request.camera_id)
        _, _, sessao_atual = buffer.snapshot()
        if sessao_atual != request.session_id:
            # Reconexão entre o gatilho e o corte: o que chegou desde então pertence
            # a outra execução do ffmpeg e é inconcatenável com este init. Sobra o
            # pré-roll que já estava congelado.
            return (), True
        ultimo = request.fragments[-1]
        return buffer.fragments_after(request.session_id, ultimo.base_media_decode_time), False

    def _falhou(self, request: ClipRequest, motivo: str) -> ClipResult:
        log.warning("clipe do evento %s falhou: %s", request.event_id, motivo)
        return ClipResult(
            event_id=request.event_id,
            camera_id=request.camera_id,
            status=ClipStatus.CLIP_FAILED,
            triggered_at=request.triggered_at,
            pre_roll_requested_s=self._options.pre_roll_s,
            post_roll_requested_s=self._options.post_roll_s,
            error=motivo,
        )

    def _contabiliza(self, resultado: ClipResult) -> None:
        with self._lock:
            if resultado.status is ClipStatus.OK:
                self._completed += 1
            else:
                self._failed += 1

    def _notifica(self, resultado: ClipResult) -> None:
        if self._on_result is None:
            return
        try:
            self._on_result(resultado)
        except Exception:  # noqa: BLE001
            # O consumidor é código de outro estágio; uma exceção dele não pode
            # matar a thread que corta os clipes de todas as câmeras.
            log.exception("consumidor de ClipResult levantou")
