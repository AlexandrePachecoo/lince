"""Montagem do payload do evento (§5.2), a partir do que a borda mediu.

O formato vive em `packages/shared/schemas/event.v1.json` — aqui está só a tradução
do estado interno para ele. `tests/test_contrato_evento.py` valida o resultado desta
tradução contra o schema, que é o que impede as duas definições de divergirem.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from lince_agent.clip.state import ClipResult, ClipStatus

SCHEMA_VERSION = 1

_CASAS = 3
"""Instantes vão em milissegundos, então durações em mais casas seriam ruído de
ponto flutuante atravessando a fronteira e virando coluna no banco."""


def new_event_id() -> str:
    """UUID de evento, gerado na borda.

    Gerado no gatilho e nunca no envio: é a chave de idempotência do §5.4, e um id
    novo a cada tentativa criaria uma duplicata na nuvem a cada retry. O formato do
    `uuid4` também é o que satisfaz a validação de nome de arquivo do `ClipStore` —
    o mesmo id vira `{event_id}.mp4` no disco do box.
    """
    return str(uuid.uuid4())


class EventSource(StrEnum):
    """Quem puxou o gatilho."""

    RULE = "rule"

    MANUAL = "manual"
    """Gatilho humano: teste de câmera na instalação e, hoje, o andaime da CLI
    enquanto o estágio 4 não existe. A nuvem precisa distinguir para não contar
    esses eventos na taxa de falso positivo por câmera (NFR-2, R-1)."""


@dataclass(frozen=True, slots=True)
class AgentIdentity:
    """De quem é o evento e sob quais versões ele nasceu.

    As versões viajam no payload porque o §6 exige poder interpretar uma taxa
    histórica de falso positivo: sem saber sob qual modelo e qual configuração o
    evento nasceu, comparar duas semanas não significa nada.
    """

    tenant_id: str
    store_id: str
    agent_version: str
    model_version: str | None = None
    config_version: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id:
            raise ValueError("tenant_id é obrigatório (NFR-6)")
        if not self.store_id:
            raise ValueError("store_id é obrigatório (NFR-6)")


@dataclass(frozen=True, slots=True)
class EventDraft:
    """O que se sabe no instante do gatilho, antes de o clipe existir.

    Existe porque o `ClipResult` chega até 15 s depois e não carrega nada do lado da
    regra — nem qual regra disparou, nem em que instante de parede isso aconteceu.
    O rascunho guarda essa metade enquanto o pós-roll não completa.
    """

    event_id: str
    camera_id: str
    triggered_at: float
    """Monotônico, o mesmo relógio do `ClipResult`. É a chave de correlação."""

    occurred_at: str
    """Já convertido para parede **no gatilho**, não no envio. Um evento que espera
    seis horas na fila porque o link caiu não pode subir dizendo que aconteceu
    agora."""

    source: EventSource = EventSource.RULE
    rule_id: str | None = None
    rule_version: int | None = None

    def __post_init__(self) -> None:
        if (self.rule_id is None) != (self.rule_version is None):
            raise ValueError("rule_id e rule_version andam juntos ou não andam")
        if self.source is EventSource.RULE and self.rule_id is None:
            raise ValueError("evento de regra precisa dizer qual regra disparou (§6)")


def clip_block(result: ClipResult) -> dict[str, object]:
    """O bloco de clipe do payload, medido pelo estágio 5."""
    return {
        "status": result.status.value,
        "duration_s": round(result.duration_s, _CASAS),
        "size_bytes": result.size_bytes,
        "pre_roll_s": round(result.pre_roll_s, _CASAS),
        "post_roll_s": round(result.post_roll_s, _CASAS),
        "pre_roll_requested_s": round(result.pre_roll_requested_s, _CASAS),
        "post_roll_requested_s": round(result.post_roll_requested_s, _CASAS),
        "fragments": result.fragments,
        "truncated_pre_roll": result.truncated_pre_roll,
        "truncated_post_roll": result.truncated_post_roll,
        "session_lost": result.session_lost,
        "error": result.error,
    }


def build_event_payload(
    draft: EventDraft,
    result: ClipResult,
    *,
    identity: AgentIdentity,
    reported_at: str,
) -> dict[str, object]:
    """Traduz gatilho + clipe no corpo do `POST /v1/events`.

    Os rolls que vão no payload são os **medidos**, não os pedidos, e os dois viajam:
    o §3.5 exige registrar o pré-roll efetivo e a entidade `CLIPE` do §6 guarda
    ambos. Um clipe com 1 s de pré-roll não é um clipe errado — é uma câmera que
    tinha acabado de reconectar, e quem faz a triagem precisa saber a diferença.
    """
    if result.event_id != draft.event_id:
        raise ValueError(
            f"clipe {result.event_id!r} não é do rascunho {draft.event_id!r}: "
            "montar o payload cruzado enviaria o clipe de um evento com os metadados de outro"
        )
    rule = None
    if draft.rule_id is not None:
        rule = {"id": draft.rule_id, "version": draft.rule_version}

    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": draft.event_id,
        "tenant_id": identity.tenant_id,
        "store_id": identity.store_id,
        "camera_id": draft.camera_id,
        "occurred_at": draft.occurred_at,
        "reported_at": reported_at,
        "source": draft.source.value,
        "rule": rule,
        "versions": {
            "agent": identity.agent_version,
            "model": identity.model_version,
            "config": identity.config_version,
        },
        "clip": clip_block(result),
    }


def build_clip_patch(
    clip: dict[str, object],
    *,
    status: ClipStatus,
    object_key: str | None = None,
    error: str | None = None,
) -> dict[str, object]:
    """Corpo do `PATCH /v1/events/{event_id}`: o desfecho do clipe.

    Recebe o bloco de clipe **já congelado no payload**, e não um `ClipResult`, porque
    o `PATCH` acontece minutos ou horas depois do corte — possivelmente depois de um
    restart do agente, quando o `ClipResult` original já não existe em lugar nenhum. O
    que sobrevive é o que está na fila.

    O `status` vem por fora porque o desfecho do **upload** é outra coisa que o
    desfecho do **corte**: um clipe cortado com sucesso ainda vira `clip_failed` aqui
    se as tentativas se esgotarem ou se o teto de disco o despejar antes de ele subir.
    """
    clipe = dict(clip)
    clipe["status"] = status.value
    if error is not None:
        clipe["error"] = error

    payload: dict[str, object] = {"schema_version": SCHEMA_VERSION, "clip": clipe}
    if object_key is not None:
        payload["object_key"] = object_key
    return payload
