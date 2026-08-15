"""Estado observável do estágio 3.

O §3.3 é explícito sobre o que um track pode e não pode ser: ID **local à câmera**,
efêmero, descartado ao fim. Não há re-identificação entre câmeras (§9) e não há nenhuma
característica de aparência — a associação é só geometria, o que mantém o NFR-4
verdadeiro por construção e não por disciplina.

O que sai daqui alimenta duas coisas: a máquina de estados do §3.4, que é *por track*, e
a telemetria do §5.3. A segunda existe porque troca de ID — o R-2, o risco crítico deste
estágio — **não aparece em contador**: o número de tracks sobe igual quando o tracker
acerta e quando erra. O que dá para medir é fragmentação, e é o que está aqui.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TrackStatus(StrEnum):
    """Ciclo de vida de um track dentro de uma câmera.

    Nomes deliberadamente diferentes dos estados do motor de regras (`NOVO`, `EM_LOJA`,
    `NO_CAIXA`, `AVALIANDO`…): são duas máquinas de estado, sobre a mesma pessoa, em
    níveis diferentes. Confundi-las depois — quando o §3.4 existir e as duas estiverem
    no mesmo processo — custaria caro.
    """

    PROVISORIO = "provisorio"
    """Nasceu de uma detecção, ainda não se confirmou. Não vale para regra: é aqui que
    mora a detecção espúria de um frame só, que o §3.3 chama de "objeto estático
    detectado como pessoa"."""

    CONFIRMADO = "confirmado"
    """Associado em frames suficientes para ser tratado como pessoa."""

    PERDIDO = "perdido"
    """Sem detecção associada, mas ainda dentro da janela de recuperação. O Kalman
    continua prevendo onde a pessoa estaria; se ela reaparecer, recupera o **mesmo ID**.
    É o que trata a oclusão curta sem quebrar o track em dois (§3.3)."""

    ENCERRADO = "encerrado"
    """Perdido além da janela. O §3.3 é explícito: reaparição vira track novo."""


@dataclass(frozen=True, slots=True)
class Track:
    """Uma pessoa, com identidade, no instante de um frame."""

    track_id: int
    """Sequencial por câmera e **nunca reusado**. Reciclar número faria dois percursos
    distintos parecerem o mesmo track num histórico de eventos."""

    camera_id: str
    status: TrackStatus

    x1: float
    y1: float
    x2: float
    y2: float
    score: float
    """Confiança da última detecção associada. Num track `PERDIDO` é a da última que
    houve — a previsão do Kalman não tem confiança própria."""

    hits: int
    """Quantas detecções foram associadas a este track."""

    age_s: float
    """Tempo desde o primeiro frame em que apareceu. Junto com `hits`, é o que atende o
    "filtro por tempo mínimo de vida do track antes de valer para regra" do §3.3.

    **O tracker não aplica esse filtro**: expõe o dado e o §3.4 decide. Misturar os dois
    é exatamente o que o ADR-002 separou — limiar é configuração por câmera, versionada
    em banco, e não constante enterrada no estágio."""

    time_since_update_s: float
    """Zero quando acabou de ser associado. Num track `PERDIDO`, há quanto tempo a
    pessoa sumiu."""

    first_seen_at: float
    last_seen_at: float
    """Monotônicos da borda, herdados do `Frame` (§3.1). O relógio da câmera nunca é
    usado, e o instante da inferência também não: o §3.4 vai medir tempo em zona com
    isto, e embutir a latência da GPU faria os limiares variarem com a carga do box."""

    vx: float = 0.0
    vy: float = 0.0
    """Velocidade estimada do centro, em pixels por segundo. É o que o Kalman usa para
    prever, e o que torna a associação viável a 3 fps."""

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def base_central(self) -> tuple[float, float]:
        """O ponto de referência da travessia (§3.4): os pés, não o centro do corpo.

        Mesma definição de `Detection.base_central`, de propósito — se o candidato mudar
        depois do benchmark, muda nos dois lugares e em uma linha cada.
        """
        return ((self.x1 + self.x2) / 2, self.y2)

    @property
    def vale_para_regra(self) -> bool:
        """Se o track já é sólido o bastante para o §3.4 sequer considerá-lo.

        Só o estado, não o tempo: o limiar de tempo é configuração por câmera e mora no
        motor de regras. O que o tracker afirma aqui é apenas que este track não é uma
        detecção espúria de um frame só.
        """
        return self.status in (TrackStatus.CONFIRMADO, TrackStatus.PERDIDO)


@dataclass(frozen=True, slots=True)
class TrackingResult:
    """A saída do estágio 3 para um frame. É o que o motor de regras (§3.4) consome."""

    camera_id: str
    sequence: int
    received_at: float
    tracks: tuple[Track, ...]
    """Todos os tracks vivos, inclusive `PROVISORIO` e `PERDIDO` — cabe a quem consome
    filtrar. Entregar só os confirmados esconderia do §3.4 a informação de que alguém
    está temporariamente ocluído, que é diferente de não estar lá."""

    tracking_ms: float


@dataclass(frozen=True, slots=True)
class CameraTrackingStats:
    """Fragmentação por câmera — o mais perto que dá para chegar do R-2 sem *ground
    truth*."""

    camera_id: str
    ativos: int = 0
    """Tracks confirmados no último frame."""

    provisorios: int = 0
    perdidos: int = 0
    criados: int = 0
    encerrados: int = 0

    criados_por_minuto: float = 0.0
    """**O sinal que importa.** Numa loja de bairro, o número de tracks criados por
    minuto deveria se parecer com o número de pessoas que entraram no quadro. Muito
    acima disso é o tracker quebrando uma pessoa em vários pedaços — a "fragmentação em
    corredor cheio" do §3.3 — e é o que antecede o falso positivo do R-2, porque um
    track que nasce já fora do caixa não tem histórico para o §3.4 avaliar."""

    vida_media_s: float = 0.0
    """Média de duração dos tracks encerrados. Caindo, é o mesmo sintoma pela outra
    ponta."""

    associacoes_alta: int = 0
    associacoes_baixa: int = 0
    """Quantas associações vieram de detecção de baixa confiança. É a medida do ganho do
    ByteTrack neste ambiente: perto de zero significa que a segunda passada não está
    servindo para nada, e aí o piso do detector pode subir de volta."""

    resets: int = 0
    """Reconexões de câmera que zeraram os tracks. Crescendo, é problema de rede ou de
    câmera (§3.1), não de tracking."""


@dataclass(frozen=True, slots=True)
class TrackerStats:
    enabled: bool = False
    cameras: tuple[CameraTrackingStats, ...] = field(default_factory=tuple)

    @property
    def ativos(self) -> int:
        return sum(camera.ativos for camera in self.cameras)

    @property
    def criados(self) -> int:
        return sum(camera.criados for camera in self.cameras)

    @property
    def encerrados(self) -> int:
        return sum(camera.encerrados for camera in self.cameras)
