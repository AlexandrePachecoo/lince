"""Quente ou estrutural: o que separa recalibrar de reiniciar (§5.2).

Este é o teste que impede a pior falha silenciosa do poll de configuração. Um campo
classificado errado não estoura nada:

- **Estrutural tratado como quente** — o dashboard mostra a resolução nova, o agente
  segue decodificando na antiga, as zonas passam a ser desenhadas sobre um quadro que
  não existe e a câmera alerta para todo cliente que sai (R-1). Nenhum log, nenhum erro.
- **Quente tratado como estrutural** — a loja fica esperando um reinício que ninguém vai
  dar para receber um limiar novo. Custa uma visita, mas ao menos aparece no heartbeat.

Errar para o segundo lado é o desenho; o `test_todo_campo_tem_classificacao` é o que
garante que ninguém erre para o primeiro por esquecimento.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from lince_agent.config import (
    CameraConfig,
    ClipOptions,
    DecodeOptions,
    DetectionOptions,
    RuleOptions,
    SupervisionOptions,
    TrackingOptions,
)
from lince_agent.config_diff import Veredito, campos_por_classe, classifica
from lince_agent.config_loader import monta_config
from lince_agent.rules.geometry import LinhaOrientada, Poligono

CLIPES = Path("/tmp/lince-clipes")


def config(**mudancas):
    """Uma loja com duas câmeras, uma delas com as zonas do §3.4 desenhadas."""
    documento = {
        "schema_version": 1,
        "config_version": "v1",
        "tenant_id": "rede-abc",
        "store_id": "loja-1",
        "cameras": [
            {
                "camera_id": "cam3",
                "url": "rtsp://x/cam3",
                "rules": {
                    "enabled": True,
                    "linha_saida": {"origem": [0, 400], "destino": [640, 400]},
                    "zonas_caixa": [{"vertices": [[0, 410], [260, 410], [260, 478], [0, 478]]}],
                },
            },
            {"camera_id": "cam1", "url": "rtsp://x/cam1"},
        ],
    } | mudancas
    return monta_config(documento, clips_dir=CLIPES)


def test_documento_identico_nao_e_mudanca():
    """A nuvem serve o mesmo conteúdo com rótulo novo quando alguém salva no dashboard
    sem mexer em nada. Tratar isso como recalibração zeraria os tracks em curso de todas
    as câmeras da loja — cegueira paga por um clique que não mudou nada."""
    diff = classifica(config(), config())

    assert diff.veredito is Veredito.IGUAL
    assert diff.a_quente == ()
    assert diff.estruturais == ()


def test_config_version_diferente_sozinha_nao_e_mudanca():
    """`config_version` é o rótulo, não um ajuste: muda em todo documento novo. Se
    entrasse no diff, todo poll pareceria recalibração e nada nunca seria `IGUAL`."""
    diff = classifica(config(), config(config_version="v2"))

    assert diff.veredito is Veredito.IGUAL


def test_zona_nova_e_troca_a_quente():
    """O caso que o ADR-002 comprou: recalibrar a zona do caixa de uma câmera pelo
    dashboard tem que chegar na loja em 30 s, sem reiniciar nada. Se isto exigisse
    reinício, a separação entre regras e modelo não teria entregado nada."""
    atual = config()
    nova = replace(
        atual,
        config_version="v2",
        cameras=(
            replace(
                atual.cameras[0],
                rules=replace(atual.cameras[0].rules, tempo_caixa_min_s=12.0),
            ),
            atual.cameras[1],
        ),
    )

    diff = classifica(atual, nova)

    assert diff.veredito is Veredito.A_QUENTE
    assert diff.a_quente == ("cameras[cam3].rules.tempo_caixa_min_s",)
    assert diff.estruturais == ()


def test_caminho_do_campo_identifica_a_camera():
    """ "A configuração mudou" não é depurável. Quem for entender por que uma loja não
    aplicou a versão nova precisa do campo e da câmera, no mesmo tom das mensagens do
    `config_loader` — sem acesso ao box."""
    atual = config()
    nova = replace(
        atual,
        cameras=(
            atual.cameras[0],
            replace(
                atual.cameras[1],
                decode=replace(atual.cameras[1].decode, width=1280, height=720),
            ),
        ),
    )

    diff = classifica(atual, nova)

    assert diff.estruturais == (
        "cameras[cam1].decode.height",
        "cameras[cam1].decode.width",
    )


def test_resolucao_de_decode_e_estrutural():
    """O caso que mais dói se escapar: zona desenhada sobre um frame de outra resolução
    não levanta erro — ela simplesmente não contém ninguém. O tempo de caixa fica em
    zero e a câmera passa a alertar para todo cliente que sai."""
    atual = config()
    nova = replace(
        atual,
        cameras=(
            replace(atual.cameras[0], decode=replace(atual.cameras[0].decode, width=1280)),
            atual.cameras[1],
        ),
    )

    assert classifica(atual, nova).veredito is Veredito.ESTRUTURAL


def test_camera_entrando_ou_saindo_e_estrutural():
    """Câmera nova precisa de processo ffmpeg, buffer circular, tracker e motor de
    regras próprios; câmera removida precisa que tudo isso seja desmontado. Nada disso
    é troca de referência, e o `camera_id` aparece no motivo porque é ele que diz qual
    corredor da loja ficou de fora."""
    atual = config()
    sem_cam1 = replace(atual, cameras=(atual.cameras[0],))

    removida = classifica(atual, sem_cam1)
    acrescentada = classifica(sem_cam1, atual)

    assert removida.estruturais == ("cameras[cam1] (removida)",)
    assert acrescentada.estruturais == ("cameras[cam1] (acrescentada)",)


def test_camera_reordenada_nao_e_mudanca():
    """O dashboard pode reordenar a lista sem que nada tenha mudado. Comparar por
    posição faria disso "todas as câmeras mudaram" — e, pior, compararia a calibração de
    uma câmera com a de outra."""
    atual = config()
    invertida = replace(atual, cameras=(atual.cameras[1], atual.cameras[0]))

    assert classifica(atual, invertida).veredito is Veredito.IGUAL


def test_limiar_de_deteccao_e_quente_e_input_size_e_estrutural():
    """`score_threshold` é lido a cada `detect`; `input_size` já virou sessão do ONNX
    Runtime e um `PreprocessSpec`. Trocar o segundo a quente não daria erro: as caixas
    sairiam deslocadas, que é o sintoma que o §3.4 mostra como zona errada."""
    atual = config()

    quente = classifica(
        atual, replace(atual, detection=replace(atual.detection, iou_threshold=0.6))
    )
    estrutural = classifica(
        atual, replace(atual, detection=replace(atual.detection, input_size=320))
    )

    assert quente.veredito is Veredito.A_QUENTE
    assert quente.a_quente == ("detection.iou_threshold",)
    assert estrutural.veredito is Veredito.ESTRUTURAL


def test_limiares_de_tracking_sao_quentes():
    """São limiares de associação, não estado: o Kalman de quem está andando pela loja
    continua válido. É o que permite ajustar o R-2 numa loja piloto sem cegar a câmera
    a cada tentativa."""
    atual = config()
    nova = replace(atual, tracking=replace(atual.tracking, iou_min=0.25))

    diff = classifica(atual, nova)

    assert diff.veredito is Veredito.A_QUENTE
    assert diff.a_quente == ("tracking.iou_min",)


def test_um_campo_estrutural_derruba_o_documento_inteiro():
    """Tudo ou nada. Aplicar só a metade quente deixaria o evento subindo com
    `versions.config` de uma calibração que nunca existiu — e é esse campo que precisa
    ser confiável quando alguém for entender um falso positivo (R-1)."""
    atual = config()
    nova = replace(
        atual,
        tracking=replace(atual.tracking, iou_min=0.25),
        cameras=(
            replace(atual.cameras[0], url="rtsp://outro/cam3"),
            atual.cameras[1],
        ),
    )

    diff = classifica(atual, nova)

    assert diff.veredito is Veredito.ESTRUTURAL
    assert diff.estruturais == ("cameras[cam3].url",)
    # O que era quente continua listado. Quem decide não aplicar é o veredito, não a
    # ausência da lista: no log do box tem que dar para ver a recalibração inteira que
    # ficou parada esperando reinício, não só o campo que a barrou.
    assert diff.a_quente == ("tracking.iou_min",)


def test_tenant_e_store_sao_estruturais():
    """O `store_id` é o prefixo das chaves da fila local (NFR-6). Trocá-lo a quente
    deixaria os eventos já enfileirados órfãos sob o prefixo antigo, e o agente subiria
    escrevendo num namespace e drenando de outro."""
    atual = config()

    assert classifica(atual, replace(atual, store_id="loja-2")).veredito is Veredito.ESTRUTURAL
    assert classifica(atual, replace(atual, tenant_id="rede-xyz")).veredito is Veredito.ESTRUTURAL


@pytest.mark.parametrize(
    "cls",
    [
        DecodeOptions,
        SupervisionOptions,
        ClipOptions,
        RuleOptions,
        CameraConfig,
        DetectionOptions,
        TrackingOptions,
    ],
)
def test_todo_campo_tem_classificacao(cls):
    """A rede de segurança do módulo, no estilo do `test_contrato_config.py`.

    Um campo novo que não entre em `A_QUENTE`, `ESTRUTURAL` nem `IGNORADO` é ignorado
    pelo diff — e ser ignorado pelo diff significa **trocado a quente sem ninguém
    reiniciar nada**, que é o lado errado do erro. Sem este teste, a falha aparece meses
    depois, numa loja, como uma configuração que o dashboard jura ter aplicado.
    """
    from dataclasses import fields

    esperados = {campo.name for campo in fields(cls)}
    classificados = set(campos_por_classe(cls))

    assert esperados - classificados == set(), (
        f"campo(s) de {cls.__name__} sem classificação em config_diff: "
        f"{sorted(esperados - classificados)}"
    )


def test_geometria_de_zona_e_comparada_por_valor():
    """As zonas são objetos, não escalares. Se a comparação caísse em identidade, dois
    polígonos iguais vindos de dois `json.loads` diferentes pareceriam mudança — e todo
    poll zeraria os tracks da loja inteira, de 30 em 30 segundos, para sempre."""
    atual = config()
    igual = config()
    assert classifica(atual, igual).veredito is Veredito.IGUAL

    movida = replace(
        atual,
        cameras=(
            replace(
                atual.cameras[0],
                rules=replace(
                    atual.cameras[0].rules,
                    linha_saida=LinhaOrientada((0.0, 380.0), (640.0, 380.0)),
                    zonas_caixa=(
                        Poligono(((0.0, 390.0), (260.0, 390.0), (260.0, 478.0), (0.0, 478.0))),
                    ),
                ),
            ),
            atual.cameras[1],
        ),
    )

    diff = classifica(atual, movida)
    assert diff.veredito is Veredito.A_QUENTE
    assert diff.a_quente == (
        "cameras[cam3].rules.linha_saida",
        "cameras[cam3].rules.zonas_caixa",
    )
