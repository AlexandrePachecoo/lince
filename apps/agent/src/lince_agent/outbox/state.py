"""Estado de um item da fila local (§3.6) e o instantâneo que vai ao heartbeat (§5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ItemKind(StrEnum):
    """As duas coisas que a borda envia por evento, nesta ordem.

    A ordem é a regra central do §3.6: o metadado é pequeno e destrava o alerta, o
    clipe é grande e pode esperar. Elas são filas separadas justamente para que uma
    fila de clipes represada por link ruim não atrase nenhum alerta.
    """

    EVENT = "event"
    CLIP = "clip"


class ItemState(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DEAD = "dead"
    """Recusado por validação (§5.4). Fica registrado em vez de sumir: uma fila morta
    crescendo é o sintoma de agente e API em versões incompatíveis, e apagar a
    evidência esconderia justamente isso."""


class ClipState(StrEnum):
    """Onde o vídeo está. Independe do estado do evento, de propósito: o alerta sobe
    antes de o clipe terminar de subir (§2.3)."""

    NONE = "none"
    """Não há clipe — o corte falhou. O evento sobe com `clip_failed` e nada mais
    acontece."""

    PENDING = "pending"
    """Existe em disco, esperando o `PUT` no R2."""

    UPLOADED = "uploaded"
    """Bytes no R2, falta confirmar com o `PATCH`. Separar este estado é o que evita
    reenviar megabytes só porque a confirmação de 200 bytes falhou."""

    GIVEN_UP = "given_up"
    """Tentativas esgotadas, teto de disco ou TTL. Falta avisar a nuvem com um
    `PATCH clip_failed` — o evento continua válido e triável, só sem vídeo."""

    DONE = "done"


@dataclass(frozen=True, slots=True)
class QueueItem:
    """Um evento na fila local, na visão de uma das duas etapas de envio.

    O `payload` é congelado no enfileiramento e nunca é remontado: é ele que carrega
    o instante em que o evento aconteceu, e reserializar no envio faria um evento que
    esperou a noite inteira subir com o horário de quando o link voltou.
    """

    event_id: str
    kind: ItemKind
    payload: dict[str, object]
    created_at_ms: int
    attempts: int = 0
    """Falhas da etapa **atual**. Zera ao passar do upload para a confirmação: o
    `PATCH` merece o próprio orçamento de tentativas."""

    state: ItemState = ItemState.PENDING
    clip_state: ClipState = ClipState.NONE
    clip_path: str | None = None
    clip_size_bytes: int = 0
    upload_url: str | None = None
    upload_expires_ms: int | None = None
    object_key: str | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class OutboxStats:
    """Instantâneo da fila. São literalmente os campos de fila do heartbeat (§5.3).

    `oldest_age_s` é o número que denuncia uma loja com link caído antes de o TTL
    começar a descartar: profundidade sozinha não distingue rajada de represamento.
    """

    depth: int = 0
    events: int = 0
    clips: int = 0
    ready: int = 0
    """Prontos para enviar agora. `depth - ready` está esperando backoff ou está com
    lease de outra tentativa em andamento."""

    oldest_age_s: float = 0.0
    payload_bytes: int = 0
    dead: int = 0


@dataclass(frozen=True, slots=True)
class SenderStats:
    """Contadores do processo de envio. Zeram no restart, como `restarts` do §5.3."""

    sent: int = 0
    clips_uploaded: int = 0
    rejected: int = 0
    expired: int = 0
    clips_expired: int = 0
    clips_given_up: int = 0
    failures: int = 0
    last_success_at: float | None = None
    last_error: str | None = None
