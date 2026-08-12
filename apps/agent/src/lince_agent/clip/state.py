"""Estado observável do estágio 5, no formato que o heartbeat do §5.3 espera."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


@dataclass(frozen=True, slots=True)
class BufferStats:
    """Instantâneo do buffer circular de uma câmera.

    `retained_bytes` é a primeira medida real da pendência §10.8 (consumo de RAM do
    buffer de 30 s × N câmeras), que hoje é só estimativa no documento.
    """

    camera_id: str
    session_id: int | None
    """`None` antes do primeiro init. Muda a cada reconexão."""

    has_init: bool
    """Sem init segment não existe clipe: nenhum fragmento é reproduzível sozinho."""

    fragments: int
    retained_bytes: int
    retained_s: float
    """Segundos de mídia efetivamente retidos. Comparado com `window_s`, é o sinal
    de que o teto de bytes está mordendo antes da janela de tempo."""

    ceiling_evictions: int
    """Fragmentos descartados pelo teto rígido de RAM, não pela janela de tempo.
    Crescendo, significa que o bitrate da câmera passou do dimensionamento e que os
    clipes dela estão saindo com pré-roll curto (§3.5, R-4)."""

    foreign_session_fragments: int
    """Fragmentos recusados por virem de outra execução do ffmpeg. Um punhado a
    cada reconexão é o esperado; um número grande sem reconexão significa que as
    threads da instância anterior não morreram."""


class ClipStatus(StrEnum):
    """Desfecho do corte de um evento."""

    OK = "ok"

    CLIP_FAILED = "clip_failed"
    """Mesmo literal do §3.5 e do `PATCH /v1/events` do §5.2, de propósito: este
    valor atravessa a fronteira agente↔nuvem e vira estado do evento no banco.

    Não é fracasso do evento — o alerta sobe do mesmo jeito. Perder o alerta porque
    o vídeo não saiu seria trocar um problema pequeno por um grande."""


@dataclass(frozen=True, slots=True)
class ClipResult:
    """O que o estágio 6 precisa saber para montar o evento (§5.2, §6).

    Os campos de roll são **medidos**, não os pedidos: o §3.5 exige registrar o
    pré-roll efetivo, e a entidade `CLIPE` do §6 guarda "pré/pós-roll efetivos".
    Um triador que recebe 1 s de pré-roll precisa saber que foi limitação da câmera.
    """

    event_id: str
    camera_id: str
    status: ClipStatus
    triggered_at: float

    path: Path | None = None
    size_bytes: int = 0
    duration_s: float = 0.0

    pre_roll_s: float = 0.0
    post_roll_s: float = 0.0
    pre_roll_requested_s: float = 0.0
    post_roll_requested_s: float = 0.0
    fragments: int = 0

    truncated_pre_roll: bool = False
    """O buffer não tinha o pré-roll inteiro: gatilho logo após a subida ou após uma
    reconexão, ou teto de bytes mordendo antes da janela de tempo."""

    truncated_post_roll: bool = False
    """O pós-roll não chegou dentro do prazo. Câmera de GOP longo, ou câmera que
    parou de entregar entre o gatilho e o corte."""

    session_lost: bool = False
    """Houve reconexão entre o gatilho e o corte. Os fragmentos da sessão nova são
    inconcatenáveis com o init da antiga, então o clipe fica só com o que já havia
    sido congelado no gatilho — o pré-roll."""

    error: str | None = None


@dataclass(frozen=True, slots=True)
class RecorderStats:
    """Alimenta o heartbeat: um `dropped` crescendo aqui significa mais gatilhos do
    que o box consegue cortar, o que é sinal de câmera mal calibrada (R-1) antes de
    ser sinal de hardware."""

    requested: int = 0
    completed: int = 0
    failed: int = 0
    dropped: int = 0
    pending: int = 0
