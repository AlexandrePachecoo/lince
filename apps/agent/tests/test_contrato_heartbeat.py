"""O heartbeat que o agente produz contra o `heartbeat.v1.json` de `packages/shared`.

O par de `test_contrato_evento.py`, e existe pelo mesmo motivo: o contrato da §5 mora
num lugar só, e a API valida o corpo com Ajv contra este mesmo arquivo antes de gravar
(`apps/api/src/routes/agents/heartbeat-schema.ts`). Sem este teste, uma divergência
entre `monta_heartbeat` e o schema só apareceria numa loja — como `400` a cada 30 s e
uma nuvem que declara o agente offline enquanto ele detecta normalmente.

O caminho de erro é pior que o do evento, aliás: evento recusado vai para a fila morta
e deixa rastro. Heartbeat recusado não deixa nada, porque heartbeat não tem fila.
"""

from __future__ import annotations

import pytest
from jsonschema.exceptions import ValidationError
from telemetria import camera, deteccao, saude

from lince_agent.heartbeat import monta_heartbeat
from lince_agent.ingest.state import CameraStatus
from lince_agent.outbox.clock import to_iso
from lince_agent.outbox.state import OutboxStats


def _to_iso(monotonic_s: float) -> str:
    """Âncora de mentira: monotônico 0 é 2026-01-01T00:00:00Z."""
    return to_iso(1767225600.0 + monotonic_s)


@pytest.fixture
def valida(validador):
    schema = validador("heartbeat.v1.json")

    def checa(corpo: dict) -> dict:
        schema.validate(corpo)
        return corpo

    return checa


def test_o_corpo_de_uma_loja_saudavel_passa_no_schema(valida):
    """A loja em regime é o caso que roda 2 880 vezes por dia, por loja.

    Se ele não valida, nada valida — e o sintoma é uma nuvem cega para toda a rede.
    """
    corpo = monta_heartbeat(
        saude(cameras=(camera("cam1"), camera("cam2"))),
        agent_version="0.1.0",
        to_iso=_to_iso,
        clock_skew_s=0.4,
        disk_free_bytes=50 * 1024**3,
    )

    valida(corpo)
    assert len(corpo["cameras"]) == 2
    assert corpo["host"]["disk_free_bytes"] == 50 * 1024**3


def test_a_subida_sem_modelo_e_sem_configuracao_ainda_passa(valida):
    """O primeiro heartbeat de um box novo: sem `.onnx` no disco, sem configuração
    aplicada, sem nenhum frame ainda.

    É o instante em que a nuvem mais precisa enxergar a loja — é quando se descobre que
    o modelo não desceu —, e é o corpo com mais `null` de todos. Um schema que só aceita
    o caso feliz falharia exatamente aí.
    """
    corpo = monta_heartbeat(
        saude(
            cameras=(camera(status=CameraStatus.STARTING, last_frame_at=None, sampled_fps=0.0),),
            deteccoes=(),
            com_modelo=False,
            config_version=None,
            uptime_s=0.5,
        ),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    valida(corpo)
    assert corpo["model_version"] is None
    assert corpo["config_version"] is None
    assert corpo["cameras"][0]["last_frame_at"] is None
    # Sem `--clips-dir` acessível o bloco fica vazio, e vazio é válido: todo campo de
    # `host` é opcional porque nem todo box tem GPU ou sensor de temperatura.
    assert corpo["host"] == {}


def test_a_loja_com_o_link_caido_passa_no_schema(valida):
    """Fila funda, mais velha que o TTL de uma noite, disco quase cheio.

    É o corpo que abre o alerta operacional, então é o que menos pode ser recusado por
    validação — uma loja represada que não consegue reportar que está represada é
    exatamente o ponto cego que o §5.3 existe para fechar.
    """
    corpo = monta_heartbeat(
        saude(
            outbox=OutboxStats(depth=412, events=206, clips=206, oldest_age_s=39_600.0),
            clip_disk_bytes=8 * 1024**3,
        ),
        agent_version="0.1.0",
        to_iso=_to_iso,
        clock_skew_s=-12.5,
        disk_free_bytes=0,
    )

    valida(corpo)
    assert corpo["queue"]["oldest_age_s"] == 39_600.0
    assert corpo["clock_skew_s"] == -12.5


@pytest.mark.parametrize("status", list(CameraStatus))
def test_todo_status_de_camera_do_agente_e_aceito_pelo_schema(valida, status):
    """O `CameraStatus` do agente e o `enum` do schema são duas listas escritas à mão,
    em linguagens diferentes, em arquivos diferentes.

    Um estado novo no agente que ninguém acrescentar ao schema derruba o heartbeat
    inteiro — não só o daquela câmera — no momento em que a primeira câmera entrar nele.
    O parâmetro é a enum inteira para que acrescentar um estado quebre aqui.
    """
    corpo = monta_heartbeat(
        saude(cameras=(camera(status=status),)),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )
    valida(corpo)


def test_um_campo_a_mais_e_recusado_pelo_schema(valida):
    """`additionalProperties: false` é o que dá valor a este arquivo de teste.

    Sem ele, o schema aceitaria qualquer coisa que contivesse os campos obrigatórios, e
    os testes acima concordariam com um `monta_heartbeat` quebrado.
    """
    corpo = monta_heartbeat(saude(), agent_version="0.1.0", to_iso=_to_iso)
    corpo["gpu_temp"] = 71.0

    with pytest.raises(ValidationError):
        valida(corpo)


def test_o_instante_do_ultimo_frame_sai_no_formato_do_contrato(valida):
    """`last_frame_at` é o único instante do heartbeat, e o `pattern` do `instant` é
    estrito: ISO-8601 UTC com **milissegundos** e sufixo `Z`.

    Um `datetime.isoformat()` cru produz `+00:00` e microssegundos, e nenhum dos dois
    casa. Só o `to_iso` do agente produz o formato certo — este teste é o que impede
    alguém de "simplificar" a conversão.
    """
    corpo = monta_heartbeat(
        saude(cameras=(camera(last_frame_at=12.5),)),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    valida(corpo)
    assert corpo["cameras"][0]["last_frame_at"] == "2026-01-01T00:00:12.500Z"


def test_a_camera_sem_deteccao_ainda_sobe_no_heartbeat(valida):
    """Câmera com `detect: false` (R-3) decodifica, alimenta buffer e pode cair.

    Se a lista de câmeras do heartbeat viesse da detecção, essa câmera sumiria da nuvem
    — e uma câmera de corredor offline por três semanas não abriria alerta nenhum,
    porque para a nuvem ela não existe.
    """
    corpo = monta_heartbeat(
        saude(
            cameras=(camera("cam1"), camera("cam9", status=CameraStatus.OFFLINE)),
            deteccoes=(deteccao("cam1"),),
        ),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    valida(corpo)
    assert [c["camera_id"] for c in corpo["cameras"]] == ["cam1", "cam9"]
    assert corpo["cameras"][1]["status"] == "offline"
    assert corpo["cameras"][1]["inference_fps"] == 0.0
