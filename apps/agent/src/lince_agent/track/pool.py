"""Um tracker por câmera, e a telemetria de fragmentação.

Não é organização de código: é o §3.3. Os IDs são **locais à câmera**, não há
re-identificação entre câmeras, e o §9 exclui isso do v1 justamente porque depende de um
tracking robusto dentro de uma câmera — que é o R-2, ainda aberto. Um tracker
compartilhado abriria a porta para associar pessoas entre câmeras sem ninguém ter
decidido isso.

Aqui também mora a única métrica que chega perto de medir o R-2 sem *ground truth*:
**tracks criados por minuto**. Troca de ID não aparece em contador — o número de tracks
sobe igual quando o tracker acerta e quando erra. Fragmentação aparece: se o tracker está
quebrando uma pessoa em vários pedaços, nascem muito mais tracks do que pessoas entraram
no quadro.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

from lince_agent.config import TrackingOptions
from lince_agent.detect.state import DetectionResult
from lince_agent.track.bytetrack import ByteTracker
from lince_agent.track.state import (
    CameraTrackingStats,
    TrackerStats,
    TrackingResult,
    TrackStatus,
)

log = logging.getLogger(__name__)

JANELA_NASCIMENTOS = 128
"""Nascimentos considerados no cálculo da taxa. Janela, e não média desde a subida, pelo
mesmo motivo do `inference_fps` do §3.2: uma degradação recente precisa aparecer."""


class TrackerPool:
    """Roteia detecções para o tracker da câmera de origem. Não é thread-safe: quem
    chama é a thread de detecção, uma só para o agente (ADR-007)."""

    def __init__(
        self,
        options: TrackingOptions | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._options = options or TrackingOptions()
        self._clock = clock
        self._trackers: dict[str, ByteTracker] = {}
        self._sequencias: dict[str, int] = {}
        self._nascimentos: dict[str, deque[float]] = {}
        self._criados_vistos: dict[str, int] = {}
        self._resets: dict[str, int] = {}

    def register(self, camera_id: str) -> None:
        """Faz a câmera aparecer na telemetria antes do primeiro frame."""
        self._tracker(camera_id)

    def reconfigure(self, options: TrackingOptions) -> None:
        """Troca os limiares de associação sem derrubar os tracks vivos (§5.2).

        Os tracks **não** são zerados, e isso é deliberado: o que muda aqui são limiares
        de associação, não estado. O Kalman de quem está andando pela loja continua
        válido — posição, velocidade e histórico de hits não dependem de qual IoU mínima
        foi usada para chegar até aqui. Zerar custaria uma janela de cegueira em cada
        recalibração, sem comprar nada.

        Vale a partir do frame seguinte porque `ByteTracker.update` lê `self._options`
        a cada chamada. Substituir o objeto em cada tracker vivo é o suficiente; um
        tracker criado depois já nasce com o novo, pelo `self._options` do pool.
        """
        self._options = options
        for tracker in self._trackers.values():
            tracker.reconfigure(options)

    def update(self, resultado: DetectionResult) -> TrackingResult:
        """Rastreia as detecções de um frame e devolve os tracks vivos daquela câmera."""
        inicio = self._clock()
        camera_id = resultado.camera_id
        self._verifica_reconexao(camera_id, resultado.sequence)

        tracker = self._tracker(camera_id)
        tracks = tracker.update(resultado.detections, at=resultado.received_at)

        self._contabiliza_nascimentos(camera_id, tracker, resultado.received_at)
        self._sequencias[camera_id] = resultado.sequence

        return TrackingResult(
            camera_id=camera_id,
            sequence=resultado.sequence,
            received_at=resultado.received_at,
            tracks=tracks,
            tracking_ms=(self._clock() - inicio) * 1000.0,
        )

    # --- reconexão ----------------------------------------------------------

    def _verifica_reconexao(self, camera_id: str, sequence: int) -> None:
        """Zera a câmera quando o `sequence` recua.

        Sequência que anda para trás significa que o ffmpeg daquela câmera reiniciou
        (§3.1). Deixar os tracks atravessarem a reconexão seria pior que perdê-los: a
        câmera pode ter sido reposicionada, e um track carregando posição e velocidade da
        cena antiga associaria a primeira pessoa que aparecesse no lugar errado — um ID
        que salta de uma pessoa para outra, que é o R-2 pela porta dos fundos.
        """
        anterior = self._sequencias.get(camera_id)
        if anterior is None or sequence >= anterior:
            return

        log.info(
            "câmera %s reconectou (sequence %d → %d): %d tracks encerrados",
            camera_id,
            anterior,
            sequence,
            len(self._trackers[camera_id].vivos),
        )
        self._trackers[camera_id] = ByteTracker(camera_id, self._options)
        self._criados_vistos[camera_id] = 0
        self._resets[camera_id] = self._resets.get(camera_id, 0) + 1

    # --- telemetria ---------------------------------------------------------

    def _contabiliza_nascimentos(self, camera_id: str, tracker: ByteTracker, at: float) -> None:
        vistos = self._criados_vistos.get(camera_id, 0)
        novos = tracker.counters.criados - vistos
        marcos = self._nascimentos[camera_id]
        for _ in range(novos):
            marcos.append(at)
        self._criados_vistos[camera_id] = tracker.counters.criados

    def _tracker(self, camera_id: str) -> ByteTracker:
        if camera_id not in self._trackers:
            self._trackers[camera_id] = ByteTracker(camera_id, self._options)
            self._sequencias.pop(camera_id, None)
            self._nascimentos[camera_id] = deque(maxlen=JANELA_NASCIMENTOS)
            self._criados_vistos[camera_id] = 0
            self._resets.setdefault(camera_id, 0)
        return self._trackers[camera_id]

    def tracks(self, camera_id: str):
        """Tracks vivos de uma câmera, sem avançar nada. Serve à ferramenta visual."""
        tracker = self._trackers.get(camera_id)
        return () if tracker is None else tracker.vivos

    def stats(self) -> TrackerStats:
        agora = self._clock()
        return TrackerStats(
            enabled=self._options.enabled,
            cameras=tuple(
                self._stats_da_camera(camera_id, tracker, agora)
                for camera_id, tracker in sorted(self._trackers.items())
            ),
        )

    def _stats_da_camera(
        self, camera_id: str, tracker: ByteTracker, agora: float
    ) -> CameraTrackingStats:
        contadores = tracker.counters
        duracoes = contadores.duracoes
        return CameraTrackingStats(
            camera_id=camera_id,
            ativos=tracker.conta(TrackStatus.CONFIRMADO),
            provisorios=tracker.conta(TrackStatus.PROVISORIO),
            perdidos=tracker.conta(TrackStatus.PERDIDO),
            criados=contadores.criados,
            encerrados=contadores.encerrados,
            criados_por_minuto=_por_minuto(self._nascimentos[camera_id], agora),
            vida_media_s=sum(duracoes) / len(duracoes) if duracoes else 0.0,
            associacoes_alta=contadores.associacoes_alta,
            associacoes_baixa=contadores.associacoes_baixa,
            resets=self._resets.get(camera_id, 0),
        )


def _por_minuto(marcos: deque[float], agora: float) -> float:
    """Taxa de nascimentos na janela, contando o tempo desde o último.

    Incluir `agora` é o que faz o número **cair** quando a loja esvazia. Sem isso, uma
    câmera que teve movimento há uma hora continuaria reportando a taxa daquele momento,
    e a métrica de fragmentação passaria a mentir justamente no período tranquilo, que é
    quando um pico chamaria atenção.
    """
    if len(marcos) < 2:
        return 0.0
    intervalo = max(agora, marcos[-1]) - marcos[0]
    if intervalo <= 0:
        return 0.0
    return (len(marcos) - 1) / intervalo * 60.0
