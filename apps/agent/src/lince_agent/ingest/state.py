"""Estado de saúde de uma câmera, no formato que o heartbeat do §5.3 espera."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CameraStatus(StrEnum):
    """Os três estados que o §5.3 exige por câmera, mais os de borda.

    `RECONNECTING` e `OFFLINE` são distintos de propósito: a ação do operador é
    diferente. Reconectando é ruído de rede e se resolve sozinho; offline significa
    que a câmera não volta há tempo suficiente para valer uma visita.
    """

    STARTING = "starting"
    OK = "ok"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class CameraHealth:
    """Instantâneo por câmera. Vira um item da lista de câmeras no heartbeat."""

    camera_id: str
    status: CameraStatus
    consecutive_failures: int
    restarts: int
    sampled_fps: float
    """Taxa efetivamente entregue ao estágio de detecção. Comparada com a taxa
    configurada, é o sinal de box saturado."""

    frames: int
    frames_dropped: int
    """Frames descartados por lentidão do consumidor, não por erro de decode. Um
    número crescendo aqui significa que o detector não acompanha."""

    fragments: int
    fragments_dropped: int
    fragment_bytes: int
    last_frame_at: float | None
    """`time.monotonic()`. Distingue "stream travado" de "stream ausente" — o §5.3
    pede exatamente essa distinção."""

    last_error: str | None = None
