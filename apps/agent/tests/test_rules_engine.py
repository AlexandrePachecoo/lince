"""O motor de regras (§3.4), em cenários de loja.

Cada teste encena uma pessoa atravessando a planta de `tests/regras.py` e verifica o
**desfecho**, não o caminho: quem pagou não vira alerta, quem não passou no caixa vira,
e quem o tracker acabou de inventar não vira nunca.

O peso dos testes está deliberadamente no que **não** deve disparar. O R-1 diz que o
produto morre por excesso de alerta falso, não por falta de alerta: um evento perdido é
um caso que o gerente não viu, e um evento falso a mais é o gerente desligando a
notificação.
"""

from __future__ import annotations

import pytest
from regras import (
    CAIXA,
    LINHA,
    LONGE_DO_CAIXA,
    NO_CAIXA,
    PASSO_S,
    caminho,
    frame,
    opcoes,
    percorre,
    track,
)

from lince_agent.config import RuleOptions
from lince_agent.rules.engine import RuleEngine, RuleEnginePool
from lince_agent.rules.state import EstadoRegra, MotivoDescarte
from lince_agent.track.state import TrackStatus

SAINDO_SEM_PAGAR = caminho((520.0, 470.0), (520.0, 355.0), 16)
"""Do fundo da loja até a rua, pelo corredor longe do caixa. A travessia cai no 11º
frame, com o track já maduro — é o caso que o produto existe para pegar."""


def motor(**kwargs) -> RuleEngine:
    return RuleEngine("cam1", opcoes(**kwargs))


# --- o caso que justifica o produto ---------------------------------------------


def test_quem_cruza_a_saida_sem_passar_no_caixa_dispara():
    unidade = motor()
    gatilhos = percorre(unidade, SAINDO_SEM_PAGAR)

    assert len(gatilhos) == 1
    gatilho = gatilhos[0]
    assert gatilho.camera_id == "cam1"
    assert gatilho.track_id == 1
    assert gatilho.rule_id == "saida-sem-caixa"
    assert gatilho.rule_version == 1
    assert gatilho.tempo_no_caixa_s == 0.0, "nunca esteve na zona do caixa"
    assert unidade.stats().eventos == 1


def test_o_instante_do_gatilho_e_o_do_frame_nao_o_da_inferencia():
    """`at` vira o `t0` do corte do clipe (§3.5). Se ele carregasse o relógio de quando
    a regra rodou, o pré-roll de 5 s começaria deslocado pela latência da GPU — e num
    box saturado (§10.10) esse deslocamento é de quase um segundo, o bastante para o
    clipe não conter o momento da travessia."""
    unidade = motor()
    (gatilho,) = percorre(unidade, SAINDO_SEM_PAGAR, inicio=1000.0)
    assert gatilho.at == 1000.0 + 10 * (1 / 3)


# --- o que não pode disparar ----------------------------------------------------


def test_quem_ficou_no_caixa_nao_dispara():
    """O desfecho da esmagadora maioria das travessias de uma loja. Se este caso
    disparar, a câmera produz um alerta por cliente atendido."""
    unidade = motor()
    parado = [NO_CAIXA] * 20
    gatilhos = percorre(unidade, parado + caminho(NO_CAIXA, (200.0, 355.0), 13))

    assert gatilhos == ()
    stats = unidade.stats()
    assert stats.eventos == 0
    assert stats.descartados_pagou == 1
    assert stats.travessias_saida == 1, "a travessia foi vista; só não virou evento"


def test_quem_entra_na_loja_nunca_dispara():
    """A linha é orientada (§3.4). Uma linha desenhada ao contrário no dashboard faz a
    câmera alertar para todo cliente que **entra** — falso positivo em massa, e o
    contador de entradas contra o de saídas é o que denuncia isso no heartbeat."""
    unidade = motor()
    gatilhos = percorre(unidade, caminho((520.0, 355.0), (520.0, 470.0), 16))

    assert gatilhos == ()
    stats = unidade.stats()
    assert stats.travessias_entrada == 1
    assert stats.travessias_saida == 0


def test_track_jovem_demais_nao_dispara():
    """**O contador do R-2.** Uma troca de ID perto da porta produz exatamente isto: um
    track que nasce do nada, sem histórico de caixa, e cruza a saída poucos frames
    depois. Sem o filtro de tempo mínimo de vida, todo ID switch na saída vira alerta —
    a causa raiz mais provável de falso positivo no v1 (§3.4)."""
    unidade = motor()
    gatilhos = percorre(unidade, caminho((520.0, 420.0), (520.0, 380.0), 4))

    assert gatilhos == ()
    assert unidade.stats().descartados_vida_curta == 1


def test_track_com_idade_mas_sem_associacoes_nao_dispara():
    """Idade **e** hits, não um ou outro. Um track que passou a vida perdido envelhece
    sem nunca ter sido observado: sua trajetória é mais extrapolação do Kalman que
    observação, e decidir sobre ela é decidir sobre uma pessoa que a câmera não viu."""
    unidade = motor(hits_min=20)
    gatilhos = percorre(unidade, SAINDO_SEM_PAGAR)

    assert gatilhos == ()
    assert unidade.stats().descartados_vida_curta == 1


def test_pessoa_oscilando_sobre_a_linha_gera_um_evento_so():
    """O debounce de travessia do §3.4.

    Alguém parado na porta conversando tem os pés oscilando sobre a linha a cada frame.
    Sem debounce, uma pessoa vira trinta alertas em dez segundos — e o NFR-2 permite
    três por câmera **por dia**.
    """
    unidade = motor()
    oscilacao = [(520.0, 407.0), (520.0, 392.0)] * 4
    gatilhos = percorre(unidade, caminho((520.0, 470.0), (520.0, 392.0), 12) + oscilacao)

    assert len(gatilhos) == 1, "a primeira travessia decide; as outras não existem"


def test_track_provisorio_nao_entra_na_maquina_de_estados():
    """`PROVISORIO` é a detecção espúria de um frame só que o §3.3 chama de objeto
    estático virando pessoa. Ela não pode nem ocupar estado."""
    unidade = motor()
    unidade.update(
        frame((track(1, LONGE_DO_CAIXA, age_s=9.0, hits=9, status=TrackStatus.PROVISORIO),))
    )
    assert unidade.stats().acompanhados == 0


def test_posicao_prevista_pelo_kalman_nao_decide_travessia():
    """**Um track `PERDIDO` é extrapolação, não observação.**

    O §3.3 registra que a caixa extrapolada chega a virar do avesso numa oclusão de
    segundos. Um alerta disparado por essa previsão manda o gerente abrir um clipe em
    que não há ninguém atravessando nada — e é assim que ele aprende a ignorar o
    aplicativo.

    Aqui a pessoa é observada dentro da loja e depois só prevista, já do lado de fora.
    A travessia não pode ser julgada sobre a previsão.
    """
    unidade = motor()
    percorre(unidade, [LONGE_DO_CAIXA] * 10)
    unidade.update(
        frame(
            (
                track(
                    1,
                    (520.0, 355.0),
                    age_s=9.0,
                    hits=10,
                    status=TrackStatus.PERDIDO,
                    time_since_update_s=0.7,
                ),
            ),
            sequence=11,
            received_at=10.0,
        )
    )
    assert unidade.stats().eventos == 0
    assert unidade.stats().travessias_saida == 0


def test_motor_desligado_nao_decide_nada():
    """Câmera sem zonas desenhadas continua ingerindo, detectando e rastreando — só não
    decide. É o estado de toda câmera no dia da instalação."""
    unidade = RuleEngine("cam1", RuleOptions())
    assert percorre(unidade, SAINDO_SEM_PAGAR) == ()
    assert unidade.stats().configurada is False


# --- a amostra ambígua ----------------------------------------------------------


def test_amostra_exatamente_sobre_a_linha_nao_engole_a_travessia():
    """Um ponto sobre a linha não tem lado, e a decisão espera o próximo frame — mas
    **esperar não pode virar esquecer**.

    Se o ponto ambíguo substituir a referência anterior, a comparação seguinte parte de
    um ponto sem lado e devolve "não cruzou" de novo. A pessoa atravessa a porta e o
    motor nunca vê: silêncio total, sem log e sem contador. É a falha mais perigosa
    deste módulo, porque um evento que não acontece não deixa rastro nenhum.
    """
    unidade = motor()
    gatilhos = percorre(unidade, [(520.0, 470.0)] * 8 + [(520.0, 400.0), (520.0, 380.0)])
    assert len(gatilhos) == 1


# --- tempo no caixa -------------------------------------------------------------


def test_tempo_no_caixa_so_conta_entre_duas_amostras_dentro_da_zona():
    """Creditar o intervalo inteiro para quem **acabou** de entrar na zona daria, a quem
    passou raspando pelo caixa, todo o tempo que gastou vindo do outro lado da loja.

    O percurso tem 26 frames, dos quais só 4 caem dentro do caixa — e 4 amostras têm 3
    intervalos entre elas, não 4. A chegada na zona não conta.
    """
    unidade = motor(tempo_caixa_min_s=100.0)
    (gatilho,) = percorre(
        unidade,
        [LONGE_DO_CAIXA] * 10
        + [NO_CAIXA] * 4
        + [LONGE_DO_CAIXA] * 4
        + caminho(LONGE_DO_CAIXA, (520.0, 355.0), 8),
    )
    assert gatilho.tempo_no_caixa_s == pytest.approx(3 * PASSO_S)


def test_oclusao_longa_dentro_do_caixa_nao_credita_tudo():
    """O teto de `intervalo_max_s`.

    Alguém some atrás de uma gôndola dentro da zona do caixa e reaparece cinco segundos
    depois. Creditar os cinco segundos é creditar tempo que ninguém observou — e o erro
    cai do lado que **suprime** o alerta, que é o pior lado: o caso deixa de chegar à
    triagem humana e não deixa rastro.
    """
    unidade = motor(tempo_caixa_min_s=100.0, intervalo_max_s=1.0)
    unidade.update(frame((track(1, NO_CAIXA, age_s=3.0, hits=9),), sequence=1, received_at=0.0))
    unidade.update(frame((track(1, NO_CAIXA, age_s=8.0, hits=10),), sequence=2, received_at=5.0))

    (gatilho,) = percorre(
        unidade,
        caminho(NO_CAIXA, (200.0, 355.0), 13),
        idade_inicial=8.0,
        inicio=5.0,
        sequence_inicial=3,
    )
    # 1 s do teto pelos 5 s de oclusão, mais 3 passos de 1/3 s andando dentro da zona.
    # Sem o teto seriam 6 s — acima do limiar de qualquer loja, e o caso sumiria.
    assert gatilho.tempo_no_caixa_s == pytest.approx(1.0 + 3 * PASSO_S)


# --- memória --------------------------------------------------------------------


def test_track_que_some_nao_deixa_lixo():
    """O agente da loja roda semanas sem reiniciar. Um dicionário que guarda toda pessoa
    que já passou pela loja é vazamento de memória com prazo de validade."""
    unidade = motor()
    percorre(unidade, [LONGE_DO_CAIXA] * 6)
    assert unidade.stats().acompanhados == 1

    unidade.update(frame((), sequence=7, received_at=2.5))
    stats = unidade.stats()
    assert stats.acompanhados == 0
    assert stats.expirados == 1


def test_track_decidido_continua_na_memoria_enquanto_a_pessoa_esta_em_quadro():
    """Esquecer um track decidido enquanto ele ainda está vivo o recriaria do zero na
    frente seguinte — e ele voltaria a poder disparar. É o debounce por outra porta."""
    unidade = motor()
    percorre(unidade, SAINDO_SEM_PAGAR)
    assert unidade.stats().acompanhados == 1
    assert unidade.stats().expirados == 0


def test_estados_terminais_nao_contam_como_expirados():
    unidade = motor()
    percorre(unidade, SAINDO_SEM_PAGAR)
    unidade.update(frame((), sequence=99, received_at=99.0))
    assert unidade.stats().expirados == 0, "ele decidiu, não sumiu"


# --- telemetria -----------------------------------------------------------------


def test_tempo_caixa_medio_reune_o_dado_que_calibra_o_n():
    """A pendência §10.5 — o N por layout de loja — se resolve com este número.

    Ele entra na média em **toda** travessia de saída, disparando ou não. Se só as que
    disparam contassem, a média mediria o limiar em vez de medir a loja: por
    construção, todo valor seria menor que o N configurado.
    """
    unidade = motor(tempo_caixa_min_s=0.5)
    percorre(unidade, [NO_CAIXA] * 20 + caminho(NO_CAIXA, (200.0, 355.0), 13))
    assert unidade.stats().descartados_pagou == 1
    assert unidade.stats().tempo_caixa_medio_s > 5.0, "a travessia de quem pagou entrou na média"


# --- o pool ---------------------------------------------------------------------


def test_cada_camera_tem_as_proprias_zonas():
    """A linha de saída de uma câmera é o corredor de outra. Zonas compartilhadas
    fariam recalibrar uma loja mexer em todas as câmeras de uma vez (ADR-002)."""
    pool = RuleEnginePool()
    pool.register("cam1", opcoes())
    pool.register("cam2", RuleOptions())

    assert percorre(pool, SAINDO_SEM_PAGAR, camera_id="cam1") != ()
    assert percorre(pool, SAINDO_SEM_PAGAR, camera_id="cam2") == ()

    por_camera = {stats.camera_id: stats for stats in pool.stats().cameras}
    assert por_camera["cam1"].configurada and not por_camera["cam2"].configurada


def test_camera_desconhecida_nao_explode():
    """Uma câmera sem regra registrada continua entregando frames. O motor a ignora em
    silêncio — levantar aqui mataria a thread de detecção, que é uma só (ADR-007)."""
    assert RuleEnginePool().update(frame(())) == ()


def test_reconexao_da_camera_zera_o_estado():
    """**Na reconexão o `ByteTracker` é recriado e os IDs recomeçam do 1.**

    Sem zerar junto, o estado do track 1 da sessão anterior — já decidido, e com a
    posição da cena antiga — seria herdado pela primeira pessoa da sessão nova. Ela
    nasceria decidida (nunca mais alertaria) ou cruzaria uma linha vinda de um ponto
    onde nunca esteve. E a câmera pode ter sido reposicionada no meio, o que torna a
    posição antiga pior que inútil.
    """
    pool = RuleEnginePool()
    pool.register("cam1", opcoes())
    percorre(pool, SAINDO_SEM_PAGAR)
    assert pool.stats().cameras[0].eventos == 1

    # O ffmpeg reiniciou: `sequence` recua e o ID 1 é outra pessoa.
    gatilhos = percorre(pool, SAINDO_SEM_PAGAR, sequence_inicial=1, inicio=100.0)
    assert len(gatilhos) == 1, "a pessoa nova é avaliada do zero, não herda o desfecho"
    assert pool.stats().cameras[0].eventos == 2


def test_zona_e_linha_da_planta_batem_com_o_cenario():
    """Guarda o cenário compartilhado: se a planta de `regras.py` for editada de um
    jeito que mova o caixa para fora da loja, metade dos testes acima passaria a medir
    outra coisa sem falhar."""
    assert CAIXA.contem(NO_CAIXA) and not CAIXA.contem(LONGE_DO_CAIXA)
    assert LINHA.dentro(NO_CAIXA) and LINHA.dentro(LONGE_DO_CAIXA)


def test_todo_motivo_de_descarte_tem_contador():
    """Motivo sem contador é diagnóstico que não chega ao heartbeat: a loja descarta e
    ninguém sabe por quê."""
    unidade = motor()
    assert set(unidade.descartes) == set(MotivoDescarte)
    assert EstadoRegra.AVALIANDO not in {EstadoRegra.EVENTO, EstadoRegra.DESCARTADO}
