"""Estado observável do estágio 2, no formato que o heartbeat do §5.3 espera.

O §5.3 pede `inference_fps` e `dropped_frames` **por câmera**, e o estágio roda com
uma thread só para todas elas (ADR-007). Por isso os contadores nascem separados por
câmera e o total é derivado, nunca o contrário: um box saturado costuma estar saturado
por causa de uma câmera, e um número agregado esconde exatamente isso.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Detection:
    """Uma caixa emitida pelo modelo.

    As coordenadas são **do frame da câmera**, com o letterbox já desfeito. Guardá-las
    no espaço do modelo economizaria uma conversão e entregaria ao motor de regras
    (§3.4) caixas que não conversam com as zonas desenhadas sobre o frame no dashboard
    — o erro daria zona errada, e zona errada é falso positivo em massa (R-1).

    Não há campo de identidade nem de aparência, e não pode haver: o §3.2 limita o
    modelo a responder *onde* estão pessoas e objetos, e o NFR-4 proíbe dado
    biométrico no MVP.
    """

    class_id: int
    """Índice da classe no modelo (COCO: 0 é pessoa). O rótulo legível é assunto de
    apresentação; o que atravessa o pipeline é o índice."""

    score: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def base_central(self) -> tuple[float, float]:
        """O ponto de referência que o §3.4 elege como candidato natural para avaliar
        a travessia da linha de saída — os pés, não o centro do corpo.

        Fica aqui, e não no motor de regras, porque é propriedade da caixa. **A validar
        por benchmark** (§3.4): se o candidato mudar, muda numa linha só.
        """
        return ((self.x1 + self.x2) / 2, self.y2)


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """A saída do estágio 2 para um frame. É o que o estágio 3 (tracking) consome."""

    camera_id: str
    sequence: int
    """Número do frame na sessão atual do ffmpeg daquela câmera. Buraco na sequência
    é frame descartado por saturação em algum ponto do caminho."""

    received_at: float
    """`time.monotonic()` da **chegada do frame**, não da inferência. É o timestamp da
    borda que o §3.1 exige, e é ele que o §3.4 vai usar para medir tempo em zona: usar
    o instante da inferência embutiria a latência da GPU no relógio das regras."""

    detections: tuple[Detection, ...]
    inference_ms: float


@dataclass(frozen=True, slots=True)
class DetectorInfo:
    """Identidade do modelo carregado. Alimenta `versions.model` do `event.v1.json`,
    que hoje sobe `null`, e o `model_version` do heartbeat (§5.3)."""

    model_version: str
    input_size: int
    provider: str
    """Provider do ONNX Runtime que de fato pegou. Pedido não é obtido: um box com o
    driver quebrado aceita `CUDAExecutionProvider` na lista e roda em CPU (§10.10)."""

    classes: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CameraDetectionStats:
    """Os campos por câmera que o §5.3 lista, medidos onde eles acontecem."""

    camera_id: str
    frames_in: int = 0
    """Frames entregues ao estágio 2 pela ingestão."""

    frames_inferred: int = 0
    dropped: int = 0
    """Frames que entraram na fila do estágio 2 e foram descartados sem inferência,
    pela política *drop oldest* do §3.2. Diferente de zero significa GPU que não
    acompanha a taxa amostrada — o sinal de saturação do R-4."""

    detections: int = 0
    errors: int = 0
    last_inference_ms: float = 0.0
    inference_fps: float = 0.0
    """Medido numa janela deslizante das últimas inferências, não desde a subida: a
    média desde o início esconde uma degradação que começou há dez minutos."""

    last_frame_at: float | None = None
    """Monotônico da borda. Com `status` da câmera, distingue "stream travado" de
    "stream ausente" (§5.3)."""


@dataclass(frozen=True, slots=True)
class DetectorStats:
    """Instantâneo do estágio inteiro."""

    enabled: bool = False
    info: DetectorInfo | None = None
    """`None` quando o estágio está desligado ou o modelo não abriu."""

    queue_depth: int = 0
    queue_size: int = 0
    """Profundidade e teto da fila de entrada — o par que o §3.2 manda expor no
    heartbeat para detectar GPU saturada antes de o descarte começar."""

    errors: int = 0
    """Inferências que levantaram exceção. A thread sobrevive a todas: falha numa
    câmera não pode parar as outras."""

    cameras: tuple[CameraDetectionStats, ...] = field(default_factory=tuple)

    @property
    def frames_in(self) -> int:
        return sum(camera.frames_in for camera in self.cameras)

    @property
    def frames_inferred(self) -> int:
        return sum(camera.frames_inferred for camera in self.cameras)

    @property
    def dropped(self) -> int:
        return sum(camera.dropped for camera in self.cameras)

    @property
    def detections(self) -> int:
        return sum(camera.detections for camera in self.cameras)
