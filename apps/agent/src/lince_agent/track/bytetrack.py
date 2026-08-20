"""O tracker de **uma** câmera.

ByteTrack, que se resume a uma ideia: associar duas vezes. A primeira passada oferece as
detecções confiáveis a todos os tracks vivos. A segunda oferece as **detecções fracas** —
aquelas que qualquer detector descartaria — apenas aos tracks que sobraram órfãos.

É essa segunda passada que trata a linha do §3.2 que diz *"oclusão, contraluz,
aglomeração → detecções perdidas; é aceito como perda de recall, **tratado no estágio
3**"*. Uma pessoa que passa atrás de uma gôndola vira uma caixa de score 0.2 por um
frame ou dois; jogá-la fora quebraria o track em dois, e track quebrado é pessoa que
"nasce" na linha de saída sem histórico — o falso positivo do R-2.

**A regra que preserva a precisão:** detecção fraca nunca cria track. Ela só continua um
que já existia, e com um gate geométrico mais apertado que o da primeira passada. Uma
sombra com score 0.15 não vira pessoa; uma pessoa que o modelo perdeu de vista por um
instante volta a ser a mesma pessoa.

O ID é local à câmera, sequencial e nunca reusado (§3.3). Nada de aparência entra na
associação — só geometria, o que mantém o NFR-4 verdadeiro por construção.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from lince_agent.config import TrackingOptions
from lince_agent.detect.state import Detection
from lince_agent.track.assign import associa
from lince_agent.track.kalman import atualizar, iniciar, para_xyah, para_xyxy, prever
from lince_agent.track.state import Track, TrackStatus


@dataclass(slots=True)
class _Interno:
    """Um track e o estado do filtro. Nunca sai daqui: o que atravessa a fronteira é o
    `Track` congelado, para que ninguém a jusante mexa na covariância sem querer."""

    track_id: int
    media: np.ndarray
    covariancia: np.ndarray
    status: TrackStatus
    score: float
    hits: int
    first_seen_at: float
    last_seen_at: float

    def caixa(self) -> tuple[float, float, float, float]:
        return para_xyxy(self.media)

    def congela(self, camera_id: str, agora: float) -> Track:
        x1, y1, x2, y2 = self.caixa()
        return Track(
            track_id=self.track_id,
            camera_id=camera_id,
            status=self.status,
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            score=self.score,
            hits=self.hits,
            age_s=agora - self.first_seen_at,
            time_since_update_s=agora - self.last_seen_at,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            vx=float(self.media[4]),
            vy=float(self.media[5]),
        )


@dataclass(slots=True)
class TrackerCounters:
    """Contadores brutos. O `pool` transforma em taxa; aqui só se acumula."""

    criados: int = 0
    encerrados: int = 0
    associacoes_alta: int = 0
    associacoes_baixa: int = 0
    duracoes: list[float] = field(default_factory=list)
    """Duração dos tracks encerrados, para a vida média. Aparada para não crescer sem
    fim num agente que roda por semanas."""


MAX_DURACOES = 256


class ByteTracker:
    """Tracking de uma câmera. Não é thread-safe: quem chama é a thread de detecção."""

    def __init__(self, camera_id: str, options: TrackingOptions | None = None) -> None:
        self.camera_id = camera_id
        self._options = options or TrackingOptions()
        self._tracks: list[_Interno] = []
        self._ids = itertools.count(1)
        self._ultimo_at: float | None = None
        self.counters = TrackerCounters()

    def reconfigure(self, options: TrackingOptions) -> None:
        """Troca os limiares a partir do próximo `update`, preservando os tracks.

        Seguro porque `TrackingOptions` só carrega limiares de associação e de morte —
        nada dele foi congelado em estado na construção. O `max_tracks` novo, se for
        menor, é aplicado na próxima poda, não retroativamente.
        """
        self._options = options

    # --- ciclo principal ----------------------------------------------------

    def update(self, deteccoes: tuple[Detection, ...], *, at: float) -> tuple[Track, ...]:
        """Um frame: prevê, associa duas vezes, nasce, morre. Devolve os tracks vivos."""
        dt = 0.0 if self._ultimo_at is None else at - self._ultimo_at
        if dt < 0:
            raise ValueError(
                f"frame fora de ordem na câmera {self.camera_id}: dt={dt:.3f}s. "
                "O Kalman não volta no tempo, e aceitar isto corromperia a velocidade"
            )
        self._ultimo_at = at

        for track in self._tracks:
            track.media, track.covariancia = prever(track.media, track.covariancia, dt)

        alta = [d for d in deteccoes if d.score >= self._options.high_threshold]
        baixa = [d for d in deteccoes if d.score < self._options.high_threshold]

        # Quem estava confirmado **antes** deste frame. A segunda passada só oferece
        # detecção fraca a estes: ressuscitar um track perdido há segundos com uma caixa
        # de score 0.2 é como o ID salta para outra pessoa.
        confirmados_antes = {
            track.track_id for track in self._tracks if track.status is TrackStatus.CONFIRMADO
        }

        candidatos = [
            t for t in self._tracks if t.status in (TrackStatus.CONFIRMADO, TrackStatus.PERDIDO)
        ]
        livres_track, alta_livre = self._passada(
            candidatos, alta, self._options.iou_min, at, alta=True
        )

        orfaos = [t for t in livres_track if t.track_id in confirmados_antes]
        nao_recuperados, _ = self._passada(
            orfaos, baixa, self._options.iou_min_baixa, at, alta=False
        )

        provisorios = [t for t in self._tracks if t.status is TrackStatus.PROVISORIO]
        provisorios_livres, alta_sobrando = self._passada(
            provisorios, alta_livre, self._options.iou_min_novo, at, alta=True
        )

        perdidos = [t for t in livres_track if t.track_id not in confirmados_antes]
        self._desassocia(nao_recuperados + perdidos + provisorios_livres, at)
        self._nascem(alta_sobrando, at)
        self._recolhe(at)

        return tuple(track.congela(self.camera_id, at) for track in self._tracks)

    # --- passos -------------------------------------------------------------

    def _passada(
        self,
        tracks: list[_Interno],
        deteccoes: list[Detection],
        iou_min: float,
        at: float,
        *,
        alta: bool,
    ) -> tuple[list[_Interno], list[Detection]]:
        """Associa e aplica; devolve `(tracks sem par, detecções sem par)`."""
        if not tracks or not deteccoes:
            return list(tracks), list(deteccoes)

        caixas_track = np.array([t.caixa() for t in tracks], dtype=np.float64)
        caixas_det = np.array([(d.x1, d.y1, d.x2, d.y2) for d in deteccoes], dtype=np.float64)
        pares, tracks_livres, dets_livres = associa(caixas_track, caixas_det, iou_min=iou_min)

        for indice_track, indice_det in pares:
            self._associa(tracks[indice_track], deteccoes[indice_det], at)
            if alta:
                self.counters.associacoes_alta += 1
            else:
                self.counters.associacoes_baixa += 1

        return [tracks[i] for i in tracks_livres], [deteccoes[j] for j in dets_livres]

    def _associa(self, track: _Interno, deteccao: Detection, at: float) -> None:
        medicao = para_xyah(deteccao.x1, deteccao.y1, deteccao.x2, deteccao.y2)
        track.media, track.covariancia = atualizar(track.media, track.covariancia, medicao)
        track.score = deteccao.score
        track.hits += 1
        track.last_seen_at = at

        if track.status is TrackStatus.PROVISORIO:
            if track.hits >= self._options.min_hits:
                track.status = TrackStatus.CONFIRMADO
        else:
            # Inclui a volta de um `PERDIDO`: a pessoa reapareceu e recupera o mesmo ID,
            # que é o ponto inteiro de manter o track vivo durante a oclusão.
            track.status = TrackStatus.CONFIRMADO

    def _desassocia(self, tracks: list[_Interno], at: float) -> None:
        for track in tracks:
            if track.status is TrackStatus.PROVISORIO:
                # Detecção de um frame só que não se sustentou: sombra, reflexo, ou o
                # "objeto estático detectado como pessoa" do §3.3. Morre sem deixar
                # rastro, e é por isso que ela nunca chega ao motor de regras.
                self._encerra(track, at)
            else:
                track.status = TrackStatus.PERDIDO

    def _nascem(self, deteccoes: list[Detection], at: float) -> None:
        for deteccao in deteccoes:
            medicao = para_xyah(deteccao.x1, deteccao.y1, deteccao.x2, deteccao.y2)
            media, covariancia = iniciar(medicao)
            self._tracks.append(
                _Interno(
                    track_id=next(self._ids),
                    media=media,
                    covariancia=covariancia,
                    # Nasce confirmado quando `min_hits` é 1; caso contrário precisa se
                    # sustentar por mais frames antes de valer para o §3.4.
                    status=(
                        TrackStatus.CONFIRMADO
                        if self._options.min_hits <= 1
                        else TrackStatus.PROVISORIO
                    ),
                    score=deteccao.score,
                    hits=1,
                    first_seen_at=at,
                    last_seen_at=at,
                )
            )
            self.counters.criados += 1

    def _recolhe(self, at: float) -> None:
        """Encerra o que passou da janela e aplica o teto de tracks."""
        for track in list(self._tracks):
            if (
                track.status is TrackStatus.PERDIDO
                and at - track.last_seen_at > self._options.max_perdido_s
            ):
                # O §3.3 é explícito: track perdido por oclusão longa expira, e a
                # reaparição vira track novo. Segurar o ID por mais tempo seria pior —
                # a pessoa pode ter saído e outra entrado no mesmo lugar.
                self._encerra(track, at)

        if len(self._tracks) <= self._options.max_tracks:
            return
        # Teto defensivo: uma câmera apontada para a rua, ou um modelo alucinando numa
        # cena difícil, não pode fazer o agente crescer sem limite. Sacrifica-se o mais
        # antigo sem associação recente, que é o menos provável de estar certo.
        excedente = len(self._tracks) - self._options.max_tracks
        for track in sorted(self._tracks, key=lambda t: t.last_seen_at)[:excedente]:
            self._encerra(track, at)

    def _encerra(self, track: _Interno, at: float) -> None:
        track.status = TrackStatus.ENCERRADO
        self._tracks.remove(track)
        self.counters.encerrados += 1
        self.counters.duracoes.append(track.last_seen_at - track.first_seen_at)
        del self.counters.duracoes[:-MAX_DURACOES]

    # --- observação ---------------------------------------------------------

    @property
    def vivos(self) -> tuple[Track, ...]:
        agora = self._ultimo_at or 0.0
        return tuple(track.congela(self.camera_id, agora) for track in self._tracks)

    def conta(self, status: TrackStatus) -> int:
        return sum(1 for track in self._tracks if track.status is status)
