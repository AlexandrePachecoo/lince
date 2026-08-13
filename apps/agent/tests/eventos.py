"""Fábricas de rascunho e de resultado de clipe, compartilhadas entre suítes.

Não é fixture porque metade dos usos é dentro de `parametrize`, que roda antes de
qualquer fixture existir.
"""

from __future__ import annotations

from pathlib import Path

from lince_agent.clip.state import ClipResult, ClipStatus
from lince_agent.outbox.event import AgentIdentity, EventDraft, EventSource

IDENTIDADE = AgentIdentity(
    tenant_id="rede-abc",
    store_id="loja-01",
    agent_version="0.0.0",
    model_version="yolo-v8n-2026.07",
    config_version="17",
)
INSTANTE = "2026-08-12T18:04:11.238Z"


def rascunho(event_id: str, **extra) -> EventDraft:
    campos = {
        "event_id": event_id,
        "camera_id": "cam-saida",
        "triggered_at": 1234.5,
        "occurred_at": INSTANTE,
        "source": EventSource.RULE,
        "rule_id": "saida-sem-caixa",
        "rule_version": 3,
    }
    campos.update(extra)
    return EventDraft(**campos)


def resultado(event_id: str, **extra) -> ClipResult:
    campos = {
        "event_id": event_id,
        "camera_id": "cam-saida",
        "status": ClipStatus.OK,
        "triggered_at": 1234.5,
        "path": Path("/tmp/x.mp4"),
        "size_bytes": 1_234_567,
        "duration_s": 12.9004,
        "pre_roll_s": 4.1,
        "post_roll_s": 8.8,
        "pre_roll_requested_s": 5.0,
        "post_roll_requested_s": 10.0,
        "fragments": 13,
    }
    campos.update(extra)
    return ClipResult(**campos)
