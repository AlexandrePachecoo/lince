"""A tradução do que a borda mediu para o que a nuvem armazena (§5.2, §6).

O que quebra aqui não estoura no agente: estoura semanas depois, quando alguém tenta
explicar a taxa de falso positivo de uma câmera e descobre que os eventos não dizem
sob qual regra nasceram, ou que o pré-roll registrado é o pedido e não o entregue.
"""

from __future__ import annotations

import pytest
from eventos import IDENTIDADE, INSTANTE, rascunho, resultado

from lince_agent.clip.state import ClipStatus
from lince_agent.clip.store import ClipStore
from lince_agent.outbox.event import (
    AgentIdentity,
    EventSource,
    build_clip_patch,
    build_event_payload,
    clip_block,
    new_event_id,
)


def test_event_id_serve_de_nome_de_arquivo(tmp_path):
    """O mesmo id é chave de idempotência na nuvem e nome de arquivo no box. Um
    gerador que produzisse algo com `/` passaria no POST e explodiria no corte."""
    store = ClipStore(tmp_path)
    for _ in range(20):
        event_id = new_event_id()
        assert store.path_for(event_id) == tmp_path / f"{event_id}.mp4"


def test_payload_leva_a_regra_e_as_versoes_vigentes():
    """Sem regra e versões no evento, uma série histórica de falso positivo é
    ininterpretável: metade dos eventos nasceu sob outros limiares (§6)."""
    event_id = new_event_id()
    payload = build_event_payload(
        rascunho(event_id), resultado(event_id), identity=IDENTIDADE, reported_at=INSTANTE
    )

    assert payload["rule"] == {"id": "saida-sem-caixa", "version": 3}
    assert payload["versions"] == {
        "agent": "0.0.0",
        "model": "yolo-v8n-2026.07",
        "config": "17",
    }
    assert payload["tenant_id"] == "rede-abc"
    assert payload["source"] == "rule"


def test_payload_leva_os_rolls_medidos_e_os_pedidos():
    """O §3.5 exige registrar o pré-roll efetivo. Um clipe com 4,1 s em vez de 5 s não
    é defeito — é câmera recém-reconectada —, e quem faz a triagem só sabe disso se os
    dois números viajarem."""
    event_id = new_event_id()
    payload = build_event_payload(
        rascunho(event_id),
        resultado(event_id, truncated_pre_roll=True),
        identity=IDENTIDADE,
        reported_at=INSTANTE,
    )

    clipe = payload["clip"]
    assert clipe["pre_roll_s"] == 4.1
    assert clipe["pre_roll_requested_s"] == 5.0
    assert clipe["truncated_pre_roll"] is True
    assert clipe["duration_s"] == 12.9, "duração vai em milissegundos, sem ruído de float"


def test_instante_do_gatilho_atravessa_intacto():
    event_id = new_event_id()
    payload = build_event_payload(
        rascunho(event_id),
        resultado(event_id),
        identity=IDENTIDADE,
        reported_at="2026-08-12T23:59:00.000Z",
    )

    assert payload["occurred_at"] == INSTANTE
    assert payload["reported_at"] == "2026-08-12T23:59:00.000Z"


def test_clipe_que_falhou_no_corte_vira_clip_failed_no_evento():
    """O alerta sobe do mesmo jeito. Perder o alerta porque o vídeo não saiu seria
    trocar um problema pequeno por um grande (§3.5)."""
    event_id = new_event_id()
    payload = build_event_payload(
        rascunho(event_id),
        resultado(event_id, status=ClipStatus.CLIP_FAILED, path=None, error="sem init segment"),
        identity=IDENTIDADE,
        reported_at=INSTANTE,
    )

    assert payload["clip"]["status"] == "clip_failed"
    assert payload["clip"]["error"] == "sem init segment"


def test_gatilho_manual_nao_finge_ser_regra():
    """Gatilho de teste de câmera contaminando a métrica de FP por câmera destruiria
    justamente o número que o R-1 manda vigiar."""
    event_id = new_event_id()
    payload = build_event_payload(
        rascunho(event_id, source=EventSource.MANUAL, rule_id=None, rule_version=None),
        resultado(event_id),
        identity=IDENTIDADE,
        reported_at=INSTANTE,
    )

    assert payload["source"] == "manual"
    assert payload["rule"] is None


def test_evento_de_regra_sem_regra_e_recusado():
    with pytest.raises(ValueError, match="qual regra"):
        rascunho(new_event_id(), rule_id=None, rule_version=None)


def test_regra_sem_versao_e_recusada():
    with pytest.raises(ValueError, match="andam juntos"):
        rascunho(new_event_id(), rule_version=None)


def test_clipe_de_outro_evento_nao_monta_payload():
    """Um cruzamento aqui mandaria o clipe de uma pessoa com os metadados de outra —
    o pior desfecho possível num sistema que produz suspeita (R-10)."""
    with pytest.raises(ValueError, match="não é do rascunho"):
        build_event_payload(
            rascunho(new_event_id()),
            resultado(new_event_id()),
            identity=IDENTIDADE,
            reported_at=INSTANTE,
        )


def test_patch_separa_desfecho_do_upload_do_desfecho_do_corte():
    """Um clipe cortado com sucesso ainda vira `clip_failed` se o upload esgotar as
    tentativas — e o evento continua válido nos dois casos."""
    event_id = new_event_id()
    congelado = clip_block(resultado(event_id))

    patch = build_clip_patch(
        congelado, status=ClipStatus.CLIP_FAILED, error="upload esgotou 5 tentativas"
    )

    assert patch["clip"]["status"] == "clip_failed"
    assert patch["clip"]["error"] == "upload esgotou 5 tentativas"
    assert patch["clip"]["duration_s"] == 12.9, "as medidas do corte continuam valendo"
    assert "object_key" not in patch
    assert congelado["status"] == "ok", "o bloco na fila não pode ser alterado no lugar"


def test_patch_de_upload_confirmado_leva_a_chave_do_objeto():
    event_id = new_event_id()
    patch = build_clip_patch(
        clip_block(resultado(event_id)),
        status=ClipStatus.OK,
        object_key=f"rede-abc/{event_id}.mp4",
    )

    assert patch["clip"]["status"] == "ok"
    assert patch["object_key"] == f"rede-abc/{event_id}.mp4"


def test_identidade_exige_tenant_e_loja():
    """NFR-6: entidade com dado de tenant sem `tenant_id` não deveria nem existir."""
    with pytest.raises(ValueError, match="tenant_id"):
        AgentIdentity(tenant_id="", store_id="loja-01", agent_version="0.0.0")
    with pytest.raises(ValueError, match="store_id"):
        AgentIdentity(tenant_id="rede-abc", store_id="", agent_version="0.0.0")
