"""O que liga o tracking à pose: quem entra, e o que sai depois de uma reconexão.

O `PoseWorker` não olha `TrackStatus` nem sabe o que é reconexão — de propósito,
para continuar testável sozinho. As duas decisões moram aqui:

**Quem entra.** Só track `CONFIRMADO` e com detecção neste frame
(`time_since_update_s == 0`). O provisório é a detecção espúria de um frame; o
perdido, e o confirmado que não foi visto agora, têm caixa *prevista* pelo
Kalman. Recortar uma caixa prevista estima o pulso de alguém num lugar onde ele
não está, com junta plausível e sem erro. É a mesma regra do §3.4, que só
amostra track com detecção nova.

**O que sai depois de uma reconexão.** Na reconexão o `Frame.sequence` recomeça
e o `TrackerPool` recria os IDs a partir do 1 (armadilha já paga no CLAUDE.md).
Um snapshot que já estava na fila da pose sai da inferência depois disso
falando de um "track 1" que não existe mais — e um consumidor com estado por
track (a janela de 2 s do gesto, quando existir) somaria a junta da pessoa
antiga à pessoa nova. O adaptador marca o instante da borda em que a sessão nova
começou e descarta, na entrega, toda pose de antes dele naquela câmera.
`received_at` é monotônico do box, então "antes do corte" é exatamente "da
sessão anterior".

O frame é o mesmo que o tracker viu: o runtime o tira do `DetectionResult` que
produziu este `TrackingResult`. Um frame de outra sequência é recusado e
contado — recortar outro frame com estas caixas poria o recorte em outra cena.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.worker import CaixaPose, ResultadoPose
from lince_agent.track.state import TrackingResult, TrackStatus

log = logging.getLogger(__name__)


class Submissor(Protocol):
    """O pedaço do `PoseWorker` que o adaptador usa."""

    def submit(
        self,
        camera_id: str,
        imagem: Frame | np.ndarray,
        pessoas: Sequence[CaixaPose],
        *,
        received_at: float | None = None,
        sequence: int | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class PoseAdapterStats:
    submetidos: int = 0
    """Snapshots entregues ao `PoseWorker`. O que acontece depois — fila, descarte,
    erro — está nas contas do próprio worker."""

    sem_pessoa: int = 0
    """Frames em que nenhum track era elegível. Não ocupam a fila: ela guarda o frame
    inteiro, e um snapshot vazio por frame empurraria para fora o que tinha gente."""

    desencontrados: int = 0
    """Frame que não era o do tracking. Deveria ser sempre zero: diferente de zero é
    fiação errada no runtime, não carga."""

    reconexoes: int = 0
    descartados_reconexao: int = 0
    """Poses que saíram da inferência depois de a câmera reconectar e eram da sessão
    anterior. Descartadas na entrega, não na fila: o worker não sabe de sessão."""

    entregues: int = 0


def caixas_para_pose(resultado: TrackingResult) -> tuple[CaixaPose, ...]:
    """As caixas do frame que a pose deve estimar, na ordem dos tracks."""
    return tuple(
        CaixaPose(track_id=t.track_id, x1=t.x1, y1=t.y1, x2=t.x2, y2=t.y2)
        for t in resultado.tracks
        if t.status is TrackStatus.CONFIRMADO and t.time_since_update_s == 0
    )


class PoseAdapter:
    """Escolhe quem vai para a pose e filtra o que volta dela.

    `submete` roda na thread de detecção, logo depois do tracking; `entrega` roda na
    thread da pose. O lock protege só o estado por câmera e as contas — nenhum dos
    dois lados espera o outro.
    """

    def __init__(
        self,
        worker: Submissor,
        *,
        on_pose: Callable[[ResultadoPose], None] | None = None,
    ) -> None:
        self._worker = worker
        self._on_pose = on_pose
        self._lock = threading.Lock()
        self._ultima_sequencia: dict[str, int] = {}
        self._corte: dict[str, float] = {}
        """Por câmera, o `received_at` do primeiro frame da sessão atual. Pose com
        instante anterior é da sessão que acabou."""

        self._submetidos = 0
        self._sem_pessoa = 0
        self._desencontrados = 0
        self._reconexoes = 0
        self._descartados_reconexao = 0
        self._entregues = 0

    def submete(self, resultado: TrackingResult, imagem: Frame | np.ndarray) -> None:
        """Manda para a pose os tracks elegíveis deste frame. Não bloqueia."""
        self._marca_sessao(resultado)

        sequencia = imagem.sequence if isinstance(imagem, Frame) else None
        if sequencia != resultado.sequence:
            log.error(
                "pose: frame %s não é o do tracking %s na câmera %s; snapshot recusado",
                sequencia,
                resultado.sequence,
                resultado.camera_id,
            )
            with self._lock:
                self._desencontrados += 1
            return

        caixas = caixas_para_pose(resultado)
        if not caixas:
            with self._lock:
                self._sem_pessoa += 1
            return

        self._worker.submit(
            resultado.camera_id,
            imagem,
            caixas,
            received_at=resultado.received_at,
            sequence=resultado.sequence,
        )
        with self._lock:
            self._submetidos += 1

    def entrega(self, resultado: ResultadoPose) -> None:
        """O `on_result` do `PoseWorker`. Descarta pose da sessão anterior.

        Erro do consumidor sobe: quem conta e registra erro de callback é o worker.
        """
        with self._lock:
            corte = self._corte.get(resultado.camera_id)
            if corte is not None and resultado.received_at < corte:
                self._descartados_reconexao += 1
                return
            self._entregues += 1
        if self._on_pose is not None:
            self._on_pose(resultado)

    def _marca_sessao(self, resultado: TrackingResult) -> None:
        # Marcado antes do filtro de elegibilidade: a sessão nova pode começar com o
        # corredor vazio, e a pose atrasada da sessão antiga ainda precisa ser cortada.
        with self._lock:
            anterior = self._ultima_sequencia.get(resultado.camera_id)
            self._ultima_sequencia[resultado.camera_id] = resultado.sequence
            if anterior is None or resultado.sequence >= anterior:
                return
            self._corte[resultado.camera_id] = resultado.received_at
            self._reconexoes += 1
        log.info(
            "pose: câmera %s reconectou (sequence %d → %d); poses da sessão anterior "
            "serão descartadas",
            resultado.camera_id,
            anterior,
            resultado.sequence,
        )

    def stats(self) -> PoseAdapterStats:
        with self._lock:
            return PoseAdapterStats(
                submetidos=self._submetidos,
                sem_pessoa=self._sem_pessoa,
                desencontrados=self._desencontrados,
                reconexoes=self._reconexoes,
                descartados_reconexao=self._descartados_reconexao,
                entregues=self._entregues,
            )
