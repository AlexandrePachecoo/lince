"""Recalibrar a loja com o agente em pé (§5.2), e recusar o que não dá.

Esta suíte é sobre a fronteira que o `config_diff` desenha, vista de dentro do runtime.
Duas coisas precisam continuar verdadeiras depois de cada troca, e nenhuma delas
estoura quando quebra:

1. **O que o agente declara é o que ele está rodando.** O `versions.config` do evento é
   o que vai explicar um falso positivo três semanas depois (R-1). Um agente rodando
   "a v7 com as câmeras da v6" declara v7, e a investigação parte de uma calibração que
   nunca existiu.
2. **Uma zona nova não pode herdar medida da zona antiga.** O tempo acumulado no caixa
   foi contado dentro de um polígono que não existe mais, e o lado da linha é o lado de
   uma linha que mudou de lugar.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from deteccoes import DetectorFalso
from regras import CAIXA, PASSO_S, caminho, opcoes
from test_runtime import Agente, ClienteMudo, _ate

from lince_agent.config_diff import Veredito
from lince_agent.rules.geometry import Poligono

DENTRO = (320.0, 460.0)
"""Bem dentro da loja, longe da linha e fora do caixa."""


@pytest.fixture
def agente(tmp_path):
    montado = Agente(tmp_path, cameras=("cam1", "cam2"), regras=opcoes())
    yield montado
    montado.runtime.stop(timeout=5.0)


def com_regras(config, camera_id, **mudancas):
    return replace(
        config,
        cameras=tuple(
            replace(camera, rules=replace(camera.rules, **mudancas))
            if camera.camera_id == camera_id
            else camera
            for camera in config.cameras
        ),
    )


# --- o que dá para trocar a quente -------------------------------------------


def test_zona_nova_chega_ao_motor_daquela_camera(agente):
    """O caminho que o ADR-002 comprou: alguém move a zona do caixa no dashboard e a
    loja passa a decidir pela zona nova em até 30 s, sem reiniciar nada."""
    agente.runtime.start()
    nova_zona = Poligono(((400.0, 420.0), (620.0, 420.0), (620.0, 478.0), (400.0, 478.0)))

    diff = agente.runtime.aplica_config(
        com_regras(
            replace(agente.config, config_version="v2"),
            "cam1",
            zonas_caixa=(nova_zona,),
        )
    )

    assert diff.veredito is Veredito.A_QUENTE
    motor = agente.runtime._regras._motores["cam1"]
    assert motor._options.zonas_caixa == (nova_zona,)
    # A outra câmera não foi tocada: recalibrar uma não pode mexer nas demais.
    assert agente.runtime._regras._motores["cam2"]._options.zonas_caixa == (CAIXA,)


def test_troca_a_quente_zera_o_tempo_de_caixa_acumulado(agente):
    """O estado por track guarda tempo medido **dentro do polígono antigo**. Mantê-lo
    depois de mover a zona daria a alguém que nunca esteve no caixa novo o crédito de
    ter estado — ou o contrário, um cliente que pagou saindo como suspeito. Ninguém
    consegue explicar esse evento depois, porque o número que o produziu não existe mais.
    """
    agente.runtime.start()
    motor = agente.runtime._regras._motores["cam1"]
    for i, ponto in enumerate(caminho((200.0, 450.0), (200.0, 450.0), 6)):
        motor.update(_frame_em(ponto, sequence=i + 1, at=i * PASSO_S))
    assert motor._tracks, "o cenário precisa de alguém acompanhado antes da troca"

    agente.runtime.aplica_config(
        com_regras(
            replace(agente.config, config_version="v2"),
            "cam1",
            tempo_caixa_min_s=9.0,
        )
    )

    assert motor._tracks == {}


def test_contadores_de_telemetria_sobrevivem_a_recalibracao(agente):
    """`eventos` e `descartes` por câmera são a métrica de falso positivo (R-1) — a
    série que diz se a recalibração funcionou. Zerá-la a cada troca destruiria
    justamente a evidência que se está tentando produzir."""
    agente.runtime.start()
    motor = agente.runtime._regras._motores["cam1"]
    motor.eventos = 3
    motor.travessias_saida = 7

    agente.runtime.aplica_config(
        com_regras(replace(agente.config, config_version="v2"), "cam1", vida_min_s=4.0)
    )

    assert motor.eventos == 3
    assert motor.travessias_saida == 7


def test_limiar_de_tracking_troca_sem_perder_os_tracks(agente):
    """Limiares de associação não são estado: o Kalman de quem está andando pela loja
    continua válido. Zerar os trackers a cada ajuste de IoU cegaria a loja no exato
    minuto em que alguém está calibrando o R-2 com vídeo real."""
    agente.runtime.start()
    tracker = agente.runtime._tracking._tracker("cam1")
    tracker._tracks.append(object())

    agente.runtime.aplica_config(
        replace(
            agente.config,
            config_version="v2",
            tracking=replace(agente.config.tracking, iou_min=0.3),
        )
    )

    assert tracker._options.iou_min == 0.3
    assert len(tracker._tracks) == 1


def test_versao_nova_vale_para_o_proximo_evento(agente):
    """A identidade que viaja no evento é congelada na construção. Sem trocá-la junto, o
    agente passaria a decidir pelos limiares novos e a declarar a versão antiga — a
    mentira exata que o "tudo ou nada" existe para evitar."""
    agente.runtime.start()

    agente.runtime.aplica_config(
        com_regras(replace(agente.config, config_version="v2"), "cam1", vida_min_s=3.0)
    )

    assert agente.runtime._identity.config_version == "v2"
    assert agente.runtime.health().config.config_version == "v2"


def test_evento_emitido_depois_da_troca_declara_a_versao_nova(tmp_path, fmp4_sessao_longa):
    """O teste anterior olha o campo; este olha o payload que sobe. É `versions.config`
    que a nuvem grava junto do evento, e é por ele que alguém vai ligar um falso
    positivo à calibração que o produziu."""
    from test_runtime import sessao

    montado = Agente(tmp_path, cliente=ClienteMudo(), regras=opcoes())
    init, fragmentos = sessao(fmp4_sessao_longa)
    try:
        montado.runtime.start()
        montado.alimenta("cam1", init, fragmentos[:20])
        montado.runtime.aplica_config(
            com_regras(replace(montado.config, config_version="v2"), "cam1", vida_min_s=3.0)
        )
        montado.relogio.agora = 10.0
        event_id = montado.runtime.trigger("cam1", at=8.0)
        montado.espera_clipe()

        assert _ate(lambda: montado.cliente.posts), "o evento não subiu no prazo"
        assert montado.cliente.posts[-1]["event_id"] == event_id
        assert montado.cliente.posts[-1]["versions"]["config"] == "v2"
    finally:
        montado.runtime.stop(timeout=5.0)


def test_documento_igual_com_rotulo_novo_nao_recalibra_nada(agente):
    """Salvar no dashboard sem mudar nada. Tratar isso como recalibração zeraria os
    tracks em curso de todas as câmeras da loja — cegueira paga por um clique que não
    mudou coisa nenhuma."""
    agente.runtime.start()
    motor = agente.runtime._regras._motores["cam1"]
    motor.update(_frame_em(DENTRO, sequence=1, at=0.0))
    antes = dict(motor._tracks)
    assert antes

    diff = agente.runtime.aplica_config(replace(agente.config, config_version="v2"))

    assert diff.veredito is Veredito.IGUAL
    assert motor._tracks == antes
    # A versão avança mesmo assim: a nuvem precisa parar de ver divergência.
    assert agente.runtime.health().config.config_version == "v2"


# --- o que exige reinício -----------------------------------------------------


def test_mudanca_estrutural_nao_aplica_nada(agente):
    """Tudo ou nada. Este é o teste que fixa a decisão: mesmo com um limiar quente no
    mesmo documento, nada é aplicado — nem a metade que daria."""
    agente.runtime.start()
    nova = replace(
        replace(agente.config, config_version="v2"),
        tracking=replace(agente.config.tracking, iou_min=0.3),
        cameras=(
            replace(agente.config.cameras[0], url="rtsp://outra/cam1"),
            agente.config.cameras[1],
        ),
    )

    diff = agente.runtime.aplica_config(nova)

    assert diff.veredito is Veredito.ESTRUTURAL
    assert agente.runtime._tracking._options.iou_min == agente.config.tracking.iou_min
    assert agente.runtime._identity.config_version == agente.config.config_version


def test_recusa_estrutural_aparece_na_saude_com_o_motivo(agente):
    """Sem isto, o dashboard mostra a configuração salva e o box roda outra, em
    silêncio. `pendente` é o que faz a nuvem saber que aquela loja precisa de reinício, e
    o motivo é o que responde "por quê" sem acesso ao box."""
    agente.runtime.start()

    agente.runtime.aplica_config(
        replace(
            replace(agente.config, config_version="v9"),
            cameras=(
                replace(
                    agente.config.cameras[0],
                    decode=replace(agente.config.cameras[0].decode, width=1280),
                ),
                agente.config.cameras[1],
            ),
        )
    )

    saude = agente.runtime.health().config
    assert saude.config_version == agente.config.config_version
    assert saude.pendente_version == "v9"
    assert saude.pendente_motivos == ("cameras[cam1].decode.width",)
    assert saude.recusas_estruturais == 1


def test_pendencia_some_quando_uma_configuracao_aplicavel_chega(agente):
    """Uma versão estrutural rejeitada e depois substituída por uma que só mexe em
    limiares deixou de exigir reinício. Se a pendência ficasse grudada, a nuvem
    continuaria pedindo uma visita que já não é necessária."""
    agente.runtime.start()
    agente.runtime.aplica_config(
        replace(
            replace(agente.config, config_version="v9"),
            cameras=(
                replace(
                    agente.config.cameras[0],
                    decode=replace(agente.config.cameras[0].decode, width=1280),
                ),
                agente.config.cameras[1],
            ),
        )
    )
    assert agente.runtime.health().config.pendente_version == "v9"

    agente.runtime.aplica_config(
        com_regras(replace(agente.config, config_version="v10"), "cam1", vida_min_s=3.0)
    )

    saude = agente.runtime.health().config
    assert saude.pendente_version is None
    assert saude.pendente_motivos == ()
    assert saude.config_version == "v10"


def test_limiar_de_deteccao_chega_ao_detector(tmp_path):
    """`score_threshold` é lido a cada `detect`. Se a troca não chegasse ao objeto do
    detector, o dashboard mostraria o limiar novo e o box continuaria filtrando pelo
    antigo — sem erro, com a diferença aparecendo só como alerta a mais ou a menos."""
    detector = DetectorFalso()
    montado = Agente(tmp_path, detector=detector, regras=opcoes())
    try:
        montado.runtime.start()
        montado.runtime.aplica_config(
            replace(
                montado.config,
                config_version="v2",
                detection=replace(montado.config.detection, score_threshold=0.55),
            )
        )

        assert [o.score_threshold for o in detector.reconfiguracoes] == [0.55]
    finally:
        montado.runtime.stop(timeout=5.0)


def test_detector_sem_modelo_aceita_a_troca(tmp_path):
    """`NullDetector` é código de produção, não dublê: é o que roda numa câmera marcada
    como inelegível para IA (R-3) e num box sem modelo no disco. Se ele não honrasse o
    `reconfigure`, o primeiro documento novo estouraria justamente nas lojas com câmera
    inelegível — e o `AttributeError` só apareceria com o poll ligado, em produção."""
    montado = Agente(tmp_path, regras=opcoes())
    try:
        montado.runtime.start()
        diff = montado.runtime.aplica_config(
            replace(
                montado.config,
                config_version="v2",
                detection=replace(montado.config.detection, score_threshold=0.55),
            )
        )

        assert diff.veredito is Veredito.A_QUENTE
        assert montado.runtime.health().config.config_version == "v2"
    finally:
        montado.runtime.stop(timeout=5.0)


def _frame_em(ponto, *, sequence: int, at: float):
    from regras import frame, track

    return frame(
        (track(1, ponto, age_s=at + 3.0, hits=sequence + 3),),
        camera_id="cam1",
        sequence=sequence,
        received_at=at,
    )
