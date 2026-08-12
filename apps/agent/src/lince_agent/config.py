"""Configuração da ingestão.

Tudo aqui é dado, não comportamento: o §3.9 da arquitetura exige que o mesmo
artefato rode na nuvem e na loja sem `if cloud`, e o que muda entre os dois é
configuração externa. Na prática isso significa que nenhum valor deste módulo pode
aparecer literal dentro da camada ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class PixelFormat(StrEnum):
    """Formato dos frames que saem no pipe de detecção.

    BGR24 custa 3 bytes por pixel e é o layout que OpenCV e Ultralytics consomem
    direto. NV12 e YUV420P custam 1,5 — metade da banda do pipe — mas cobram uma
    conversão por frame do lado Python. Comece em BGR24; os outros dois são a
    alavanca para quando o pipe saturar.
    """

    BGR24 = "bgr24"
    NV12 = "nv12"
    YUV420P = "yuv420p"

    def frame_bytes(self, width: int, height: int) -> int:
        """Tamanho exato de um frame decodificado, em bytes.

        Este número é o contrato do pipe de detecção: `rawvideo` não tem framing
        nenhum, então o leitor fatia o stream em blocos deste tamanho. Errar aqui
        não produz erro — produz todos os frames embaralhados.
        """
        if self is PixelFormat.BGR24:
            return width * height * 3
        # NV12 e YUV420P são planares 4:2:0: um plano Y de w×h mais dois planos de
        # croma subamostrados por 2 em cada eixo.
        if width % 2 or height % 2:
            raise ValueError(f"{self} exige largura e altura pares, recebi {width}x{height}")
        return width * height * 3 // 2


class HwAccel(StrEnum):
    """Estratégia de decode por hardware.

    A escolha é em tempo de execução (`capabilities.detect_hwaccel`), nunca em
    tempo de build: o box da loja tem GPU NVIDIA e a máquina de desenvolvimento
    não, e o §3.1 exige fallback para CPU.
    """

    NONE = "none"
    """Decode em CPU. Único modo disponível sem GPU."""

    CUDA = "cuda"
    """`-hwaccel cuda`: decodifica na GPU e devolve os frames para a RAM do
    sistema, que é o que o pipe rawvideo exige. Agnóstico de codec, então funciona
    igual numa frota com câmeras H.264 e H.265 misturadas."""

    CUDA_GPU_FILTER = "cuda-gpu-filter"
    """Mantém os frames na VRAM e faz amostragem e escala na própria GPU, descendo
    pelo PCIe só os 3 fps que sobreviveram em vez dos 15. É a configuração ótima do
    box de referência. Custo: obriga NV12, porque `hwdownload` não produz BGR24 a
    partir de frames CUDA."""


@dataclass(frozen=True, slots=True)
class DecodeOptions:
    """Opções que viram argumentos do ffmpeg. Ver docs do `ffmpeg/command.py`."""

    sample_fps: float = 3.0
    """Taxa entregue ao estágio de detecção (§3.2). O decode continua em taxa
    cheia — a amostragem economiza inferência e banda de pipe, não decode."""

    width: int = 640
    height: int = 480
    pixel_format: PixelFormat = PixelFormat.BGR24
    hwaccel: HwAccel = HwAccel.NONE

    rtsp_transport: str = "tcp"
    """RTP dentro do TCP. Em UDP, uma LAN carregada perde pacotes e produz a
    pixelação que o §3.1 lista como falha esperada; TCP retransmite."""

    socket_timeout_s: float = 5.0
    """Timeout de I/O do socket RTSP. Sem ele, uma câmera que para de responder
    deixa o ffmpeg pendurado para sempre. Vira microssegundos no argumento."""

    probesize_bytes: int = 1_000_000
    analyze_duration_s: float = 2.0
    """Quanto o ffmpeg consome antes de decidir os parâmetros do stream. O default
    do ffmpeg é 5 s de `analyzeduration`, ou seja, 5 s de atraso na subida de cada
    câmera. Baixar demais faz o ffmpeg errar o formato e abortar."""

    low_latency: bool = True
    """`-fflags nobuffer -flags low_delay`."""

    wallclock_timestamps: bool = True
    """`-use_wallclock_as_timestamps 1`. Implementa o §3.1: o timestamp de
    referência é o da borda, nunca o da câmera. Vale para as duas saídas, então o
    t0 do gatilho e o timeline do clipe compartilham o mesmo relógio.

    Aplicado **só a entradas RTSP**: a opção carimba cada pacote na chegada, e num
    arquivo — lido na velocidade máxima — todos chegariam no mesmo instante."""

    log_level: str = "warning"
    """`error` esconderia os avisos de corrupção de pacote, que são justamente o
    sinal de câmera degradada. `info` polui."""

    def __post_init__(self) -> None:
        if self.sample_fps <= 0:
            raise ValueError(f"sample_fps deve ser positivo, recebi {self.sample_fps}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"resolução inválida: {self.width}x{self.height}")
        if self.hwaccel is HwAccel.CUDA_GPU_FILTER and self.pixel_format is not PixelFormat.NV12:
            raise ValueError(
                "HwAccel.CUDA_GPU_FILTER exige PixelFormat.NV12: o filtro hwdownload "
                "não converte frames CUDA para BGR24"
            )

    @property
    def frame_bytes(self) -> int:
        return self.pixel_format.frame_bytes(self.width, self.height)


@dataclass(frozen=True, slots=True)
class SupervisionOptions:
    """Política de reconexão e watchdog (§3.1).

    Nada disto pode ser delegado ao ffmpeg: `-reconnect` e companhia são opções do
    protocolo HTTP e são silenciosamente ignoradas numa entrada RTSP.
    """

    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    backoff_jitter_s: float = 1.0
    """Jitter aditivo. Sem ele, oito câmeras que caem juntas (queda do switch)
    voltam juntas e batem no mesmo instante, repetidamente."""

    offline_after_failures: int = 5
    """Falhas consecutivas até o estado virar `offline` no heartbeat. O §5.3 exige
    distinguir `reconnecting` de `offline`: a ação do operador é diferente."""

    stall_timeout_s: float = 10.0
    """Sem frame novo por este tempo, o stream travou sem fechar a conexão. O
    `socket_timeout_s` não pega este caso — o socket continua vivo."""

    stop_grace_s: float = 3.0
    """Espera entre SIGTERM e SIGKILL."""


@dataclass(frozen=True, slots=True)
class ClipOptions:
    """Buffer circular e corte do clipe (§3.5).

    Por câmera, não por loja: o §3.4 exige limiares por câmera, e aqui isso é
    concreto — uma câmera de saída com GOP de 10 s precisa de outra tolerância de
    pós-roll que a do corredor.
    """

    window_s: float = 30.0
    """Quanto de vídeo o buffer retém. Precisa cobrir o corte inteiro com folga:
    durante a espera do pós-roll a poda continua rodando, e uma janela apertada
    descartaria o pré-roll antes de o corte acontecer."""

    pre_roll_s: float = 5.0
    post_roll_s: float = 10.0
    """Os 10 s são incompressíveis por definição do produto e consomem dois terços
    do orçamento do §3.7. É por isso que o alerta sobe antes do clipe."""

    post_roll_grace_s: float = 5.0
    """Tolerância além do pós-roll antes de desistir e cortar o que houver. Sem
    ela, uma câmera de GOP longo que parou de entregar seguraria o clipe para
    sempre — e o alerta não pode esperar (§3.7)."""

    max_bytes: int = 16 * 1024 * 1024
    """Teto rígido de RAM por câmera (§3.5). Vence a janela de tempo: RAM é limite
    físico, 30 s é desejo de produto. Quando morde, o pré-roll sai curto e isso é
    registrado no evento em vez de silenciado."""

    max_disk_bytes: int = 2 * 1024**3
    """Teto do diretório de clipes pendentes. Ao estourar, o clipe mais antigo é
    apagado e o evento sobrevive sem ele (§3.6)."""

    remux_timeout_s: float = 30.0
    pending_requests: int = 8
    """Gatilhos aguardando corte. Cheia, descarta e conta: uma rajada não pode
    crescer memória nem bloquear o motor de regras."""

    def __post_init__(self) -> None:
        if self.pre_roll_s <= 0:
            raise ValueError(f"pre_roll_s deve ser positivo, recebi {self.pre_roll_s}")
        if self.post_roll_s <= 0:
            raise ValueError(f"post_roll_s deve ser positivo, recebi {self.post_roll_s}")
        if self.post_roll_grace_s < 0:
            raise ValueError(f"post_roll_grace_s não pode ser negativo: {self.post_roll_grace_s}")
        necessario = self.pre_roll_s + self.post_roll_s + self.post_roll_grace_s
        if self.window_s < necessario:
            raise ValueError(
                f"window_s de {self.window_s}s não cobre pré-roll + pós-roll + tolerância "
                f"({necessario}s): o buffer descartaria o pré-roll antes do corte"
            )
        if self.max_bytes <= 0:
            raise ValueError(f"max_bytes deve ser positivo, recebi {self.max_bytes}")
        if self.max_disk_bytes <= 0:
            raise ValueError(f"max_disk_bytes deve ser positivo, recebi {self.max_disk_bytes}")
        if self.pending_requests <= 0:
            raise ValueError(f"pending_requests deve ser positivo, recebi {self.pending_requests}")

    @property
    def clip_duration_s(self) -> float:
        """Duração nominal. A efetiva vai no evento e costuma diferir: o corte se
        alinha ao keyframe e o buffer pode não ter o pré-roll inteiro."""
        return self.pre_roll_s + self.post_roll_s


@dataclass(frozen=True, slots=True)
class CameraConfig:
    camera_id: str
    url: str
    decode: DecodeOptions = field(default_factory=DecodeOptions)
    supervision: SupervisionOptions = field(default_factory=SupervisionOptions)
    clip: ClipOptions = field(default_factory=ClipOptions)

    def __post_init__(self) -> None:
        if not self.camera_id:
            raise ValueError("camera_id é obrigatório")
        if not self.url:
            raise ValueError("url é obrigatória")
