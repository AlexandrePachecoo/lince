"""O documento de configuração do agente contra o schema de `packages/shared` (§5.2).

O contrato do evento é verificado no sentido de quem produz (`test_contrato_evento`);
este é o sentido de quem consome, e por isso a divergência é mais silenciosa. Um campo
que o agente aceita e o schema não é um limiar que a API vai recusar quando alguém
tentar salvá-lo pelo dashboard; um campo que o schema aceita e o agente ignora é uma
loja calibrada na tela e não calibrada no box — sem erro em lugar nenhum, e com um
número de falso positivo que ninguém consegue explicar.

Os dois primeiros testes comparam **conjuntos de campos**, não payloads. É de
propósito: um exemplo só percorre o caminho feliz, e o que quebra na prática é o campo
novo que entrou em dois dos três lugares.
"""

from __future__ import annotations

import json
from dataclasses import fields
from pathlib import Path

import pytest
from conftest import SCHEMAS
from jsonschema.exceptions import ValidationError

from lince_agent import config_loader
from lince_agent.config import (
    AgentConfig,
    CameraConfig,
    ClipOptions,
    DecodeOptions,
    DetectionOptions,
    RuleOptions,
    SupervisionOptions,
    TrackingOptions,
)

EXEMPLO = Path(__file__).resolve().parents[1] / "config.exemplo.json"

SCHEMA = json.loads((SCHEMAS / "config.v1.json").read_text(encoding="utf-8"))

BLOCOS = {
    "decode": (config_loader._DECODE, DecodeOptions),
    "supervision": (config_loader._SUPERVISION, SupervisionOptions),
    "clip": (config_loader._CLIP, ClipOptions),
    "rules": (config_loader._RULES, RuleOptions),
    "camera": (config_loader._CAMERA, CameraConfig),
    "detection": (config_loader._DETECTION, DetectionOptions),
    "tracking": (config_loader._TRACKING, TrackingOptions),
}

SO_DO_BOX = {
    # O que é da máquina e não da loja. A nuvem manda **quais** limiares; onde o
    # arquivo pousou no disco e que provider existe naquele hardware é do box, e uma
    # resposta HTTP não pode repointar nem uma coisa nem outra (§5.2, §10.10).
    "detection": {"model_path", "providers"},
    "raiz": {"clips_dir", "outbox", "cloud", "ffmpeg_bin"},
}

SO_DO_CONTRATO = {
    # Não vira campo de dataclass: é roteamento de versão, e quem o interpreta é o
    # próprio loader.
    "raiz": {"schema_version"},
}


def propriedades(nome: str) -> set[str]:
    fonte = SCHEMA if nome == "raiz" else SCHEMA["$defs"][nome]
    return set(fonte["properties"])


@pytest.mark.parametrize("bloco", sorted(BLOCOS))
def test_schema_e_loader_aceitam_os_mesmos_campos(bloco):
    """Campo que existe num lado e não no outro é a divergência que este contrato
    existe para impedir — e ela não estoura sozinha: o loader recusa o documento que a
    nuvem considera válido, e a loja fica com a configuração antiga sem ninguém saber.
    """
    do_loader = set(BLOCOS[bloco][0])
    if bloco == "camera":
        # Os blocos aninhados são convertidos à parte, mas são campos da câmera.
        do_loader |= {"decode", "supervision", "clip", "rules"}

    assert do_loader == propriedades(bloco) - SO_DO_BOX.get(bloco, set())


@pytest.mark.parametrize("bloco", sorted(BLOCOS))
def test_todo_campo_da_dataclass_e_configuravel_pela_nuvem(bloco):
    """O caso que este teste pega é o mais provável de todos: alguém acrescenta um
    limiar ao `config.py` e para por aí.

    O agente ganha o campo, o default vira constante de fato, e recalibrar aquilo numa
    loja passa a exigir republicar imagem — que é exatamente o que o §3.4 diz que não
    pode acontecer. Um campo novo que é mesmo do box entra em `SO_DO_BOX`, com o
    motivo escrito.
    """
    _, cls = BLOCOS[bloco]
    da_dataclass = {campo.name for campo in fields(cls)}

    assert da_dataclass - SO_DO_BOX.get(bloco, set()) == propriedades(bloco) - SO_DO_BOX.get(
        bloco, set()
    )


def test_raiz_do_schema_e_a_do_agente():
    do_schema = propriedades("raiz")
    da_dataclass = {campo.name for campo in fields(AgentConfig)}

    assert do_schema - SO_DO_CONTRATO["raiz"] == da_dataclass - SO_DO_BOX["raiz"]
    assert set(config_loader._RAIZ) == do_schema


def test_exemplo_do_repositorio_valida(validador):
    """`config.exemplo.json` é o que alguém copia. Um exemplo que o schema recusa
    ensina um formato que a API vai rejeitar no primeiro poll."""
    validador("config.v1.json").validate(json.loads(EXEMPLO.read_text(encoding="utf-8")))


def test_documento_completo_valida(validador):
    """Todo campo de todo bloco preenchido de uma vez: o exemplo exercita o caminho
    feliz e mínimo, e é nos campos raramente usados que um `type` errado passa
    despercebido."""
    documento = {
        "schema_version": 1,
        "config_version": "cfg-1",
        "tenant_id": "rede-abc",
        "store_id": "loja-1",
        "cameras": [
            {
                "camera_id": "cam1",
                "url": "rtsp://cam/stream",
                "detect": True,
                "decode": {
                    "sample_fps": 3,
                    "width": 640,
                    "height": 480,
                    "pixel_format": "bgr24",
                    "hwaccel": "cuda",
                    "rtsp_transport": "tcp",
                    "socket_timeout_s": 5,
                    "probesize_bytes": 1000000,
                    "analyze_duration_s": 2,
                    "low_latency": True,
                    "wallclock_timestamps": True,
                    "log_level": "warning",
                },
                "supervision": {
                    "backoff_base_s": 1,
                    "backoff_cap_s": 60,
                    "backoff_jitter_s": 1,
                    "offline_after_failures": 5,
                    "stall_timeout_s": 10,
                    "stop_grace_s": 3,
                },
                "clip": {
                    "window_s": 30,
                    "pre_roll_s": 5,
                    "post_roll_s": 10,
                    "post_roll_grace_s": 5,
                    "max_bytes": 16777216,
                    "max_disk_bytes": 2147483648,
                    "remux_timeout_s": 30,
                    "pending_requests": 8,
                },
                "rules": {
                    "enabled": True,
                    "rule_id": "saida-sem-caixa",
                    "rule_version": 3,
                    "linha_saida": {"origem": [0, 400], "destino": [640, 400]},
                    "zonas_caixa": [{"vertices": [[0, 410], [260, 410], [260, 478]]}],
                    "tempo_caixa_min_s": 8,
                    "vida_min_s": 2,
                    "hits_min": 3,
                    "intervalo_max_s": 1,
                    "janela_tempo_caixa": 64,
                },
            }
        ],
        "detection": {
            "enabled": True,
            "input_size": 640,
            "score_threshold": 0.1,
            "iou_threshold": 0.45,
            "classes": [0],
            "queue_size": 8,
            "fps_window": 32,
        },
        "tracking": {
            "enabled": True,
            "high_threshold": 0.5,
            "iou_min": 0.2,
            "iou_min_baixa": 0.4,
            "iou_min_novo": 0.1,
            "min_hits": 2,
            "max_perdido_s": 2,
            "max_tracks": 64,
        },
    }

    validador("config.v1.json").validate(documento)
    # E o agente aceita o mesmo documento: schema válido que o loader recusa seria
    # divergência com outra cara.
    config_loader.monta_config(
        documento, clips_dir=Path("/tmp/clipes"), model_path=Path("/opt/m.onnx")
    )


@pytest.mark.parametrize(
    ("nome", "estrago"),
    [
        ("sem config_version", lambda d: d.pop("config_version")),
        ("sem câmera nenhuma", lambda d: d.update(cameras=[])),
        ("major errada", lambda d: d.update(schema_version=2)),
        ("campo desconhecido na raiz", lambda d: d.update(modelo_url="https://…")),
        (
            "campo desconhecido na câmera",
            lambda d: d["cameras"][0].update(zona_caixa=[]),
        ),
        (
            "ponto com três números",
            lambda d: d["cameras"][0]["rules"]["linha_saida"].update(origem=[0, 400, 0]),
        ),
        (
            "polígono de dois vértices",
            lambda d: d["cameras"][0]["rules"].update(zonas_caixa=[{"vertices": [[0, 0], [1, 1]]}]),
        ),
        (
            "limiar acima de 1",
            lambda d: d.update(tracking={"high_threshold": 1.5}),
        ),
        (
            "tempo de caixa zero",
            lambda d: d["cameras"][0]["rules"].update(tempo_caixa_min_s=0),
        ),
        ("tenant vazio", lambda d: d.update(tenant_id="")),
    ],
)
def test_documento_adulterado_e_rejeitado(nome, estrago, validador):
    """Sem os casos negativos, os positivos podem estar verdes por vacuidade: um schema
    frouxo aceita tudo, inclusive o que a loja não deveria conseguir salvar.

    `tempo_caixa_min_s: 0` é o exemplo que dói: com zero, nenhuma travessia jamais
    dispara e a câmera fica muda — a falha que não deixa rastro nenhum para depurar.
    """
    documento = {
        "schema_version": 1,
        "config_version": "cfg-1",
        "tenant_id": "rede-abc",
        "store_id": "loja-1",
        "cameras": [
            {
                "camera_id": "cam1",
                "url": "rtsp://cam/stream",
                "rules": {
                    "enabled": True,
                    "linha_saida": {"origem": [0, 400], "destino": [640, 400]},
                    "zonas_caixa": [{"vertices": [[0, 410], [260, 410], [260, 478]]}],
                },
            }
        ],
    }
    estrago(documento)

    with pytest.raises(ValidationError):
        validador("config.v1.json").validate(documento)


def test_o_que_o_schema_recusa_o_agente_tambem_recusa(validador):
    """As duas pontas precisam recusar as mesmas coisas.

    Se só o schema recusa, a API barra e a loja fica sem calibrar — ruim, mas visível.
    Se só o agente recusa, o dashboard salva uma zona que a loja nunca aplica, e a tela
    passa a mentir sobre o que a câmera está fazendo.
    """
    documento = {
        "schema_version": 1,
        "config_version": "cfg-1",
        "tenant_id": "rede-abc",
        "store_id": "loja-1",
        "cameras": [
            {
                "camera_id": "cam1",
                "url": "rtsp://cam/stream",
                "rules": {
                    "enabled": True,
                    "linha_saida": {"origem": [0, 400], "destino": [640, 400]},
                    "zonas_caixa": [{"vertices": [[0, 0], [1, 1]]}],
                },
            }
        ],
    }

    with pytest.raises(ValidationError):
        validador("config.v1.json").validate(documento)
    with pytest.raises(config_loader.ConfigError):
        config_loader.monta_config(documento, clips_dir=Path("/tmp/clipes"))
