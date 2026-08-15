"""A física do tracking, sozinha — sem tracker, sem detector, sem relógio.

O que este arquivo guarda é a propriedade que separa esta implementação de todas as de
referência: o `dt` é tempo real. A fila do §3.2 descarta o frame mais antigo quando a
inferência não acompanha, então o intervalo entre frames varia com a carga do box. Um
filtro que assume cadência fixa erra a extrapolação exatamente quando o box está
saturado, e erra em silêncio — a caixa prevista sai deslocada, a associação falha, e o
sintoma final é troca de ID (R-2).
"""

from __future__ import annotations

import numpy as np
import pytest

from lince_agent.track.kalman import (
    atualizar,
    iniciar,
    para_xyah,
    para_xyxy,
    prever,
    projetar,
)

PASSO_S = 1 / 3
"""O intervalo nominal entre frames avaliados, a 3 fps (§3.2)."""


def medicao(cx: float = 320.0, cy: float = 240.0, altura: float = 200.0) -> np.ndarray:
    return np.array([cx, cy, 0.4, altura])


def andando(passos: int, *, dx: float, dt: float = PASSO_S):
    """Roda o filtro sobre alguém se movendo em linha reta a velocidade constante."""
    media, covariancia = iniciar(medicao())
    for i in range(1, passos + 1):
        media, covariancia = prever(media, covariancia, dt)
        media, covariancia = atualizar(media, covariancia, medicao(cx=320.0 + dx * i))
    return media, covariancia


# --- conversão de caixa ---------------------------------------------------------


def test_ida_e_volta_entre_xyxy_e_xyah():
    """A conversão acontece a cada frame, nos dois sentidos. Um erro aqui desloca toda
    caixa prevista sem levantar nada."""
    original = (100.0, 50.0, 180.0, 250.0)
    estado = np.concatenate([para_xyah(*original), np.zeros(4)])
    assert para_xyxy(estado) == pytest.approx(original)


def test_caixa_degenerada_e_recusada():
    """Altura zero dividiria a proporção por zero e envenenaria o estado com `inf`, que
    atravessaria o filtro inteiro sem erro."""
    with pytest.raises(ValueError, match="altura não positiva"):
        para_xyah(10.0, 20.0, 30.0, 20.0)


# --- o modelo de movimento ------------------------------------------------------


def test_sem_movimento_a_previsao_nao_anda():
    """Velocidade começa em zero, então o primeiro passo prevê que a pessoa está onde
    estava. Confiar num movimento que nunca foi observado seria inventar."""
    media, covariancia = iniciar(medicao())
    prevista, _ = prever(media, covariancia, PASSO_S)
    assert prevista[:4] == pytest.approx(media[:4])


def test_velocidade_constante_extrapola_para_a_frente():
    """Depois de alguns frames andando 30 px por passo, o filtro deve prever o próximo
    30 px adiante — não onde a pessoa estava."""
    media, covariancia = andando(6, dx=30.0)
    prevista, _ = prever(media, covariancia, PASSO_S)
    assert prevista[0] > media[0] + 20.0


def test_velocidade_estimada_bate_com_a_real():
    """30 px a cada 1/3 s são 90 px/s. É esse número que sustenta a associação a 3 fps,
    e é ele que vai para `Track.vx`."""
    media, _ = andando(10, dx=30.0)
    assert media[4] == pytest.approx(90.0, rel=0.15)


def test_dois_passos_curtos_chegam_no_mesmo_lugar_que_um_longo():
    """**A propriedade que o descarte do §3.2 exige.** Quando um frame é descartado, o
    tracker recebe um intervalo dobrado. Com `dt` fixo em "um frame", a média pararia na
    metade do caminho e a caixa prevista sairia atrás da pessoa."""
    media, covariancia = andando(8, dx=30.0)

    dois_curtos = prever(*prever(media, covariancia, PASSO_S), PASSO_S)[0]
    um_longo = prever(media, covariancia, 2 * PASSO_S)[0]

    assert dois_curtos[:4] == pytest.approx(um_longo[:4])


def test_intervalo_maior_gera_mais_incerteza():
    """Quanto mais tempo sem medição, menos o filtro pode afirmar. Ruído fixo deixaria o
    filtro confiante numa previsão esticada por segundos — e confiança indevida vira
    associação errada, que é troca de ID."""
    media, covariancia = andando(4, dx=30.0)
    curto = prever(media, covariancia, PASSO_S)[1]
    longo = prever(media, covariancia, 4 * PASSO_S)[1]
    assert np.trace(longo) > np.trace(curto)


def test_dt_negativo_e_recusado():
    """Frame fora de ordem. O filtro não tem como voltar no tempo, e aceitar em silêncio
    corromperia a velocidade."""
    media, covariancia = iniciar(medicao())
    with pytest.raises(ValueError, match="dt"):
        prever(media, covariancia, -0.1)


# --- a correção -----------------------------------------------------------------


def test_medicao_puxa_a_previsao_na_direcao_observada():
    media, covariancia = iniciar(medicao())
    prevista, cov_prevista = prever(media, covariancia, PASSO_S)
    corrigida, _ = atualizar(prevista, cov_prevista, medicao(cx=400.0))

    assert prevista[0] < corrigida[0] <= 400.0


def test_medicao_reduz_a_incerteza():
    media, covariancia = iniciar(medicao())
    prevista, cov_prevista = prever(media, covariancia, PASSO_S)
    _, cov_corrigida = atualizar(prevista, cov_prevista, medicao(cx=330.0))
    assert np.trace(cov_corrigida) < np.trace(cov_prevista)


def test_tremor_do_detector_nao_vira_movimento():
    """A caixa treme alguns pixels entre frames mesmo com a pessoa parada. Sem o ruído
    de medição, o filtro leria isso como velocidade e passaria a prever deslocamento de
    quem está imóvel — e o §3.4 mede tempo em zona sobre essa posição."""
    media, covariancia = iniciar(medicao())
    ruidos = [2.0, -3.0, 1.0, -2.0, 3.0, -1.0, 2.0, -2.0]
    for tremor in ruidos:
        media, covariancia = prever(media, covariancia, PASSO_S)
        media, covariancia = atualizar(media, covariancia, medicao(cx=320.0 + tremor))

    assert abs(media[4]) < 20.0, "tremor de poucos pixels virou velocidade"


def test_medicao_com_forma_errada_e_recusada():
    media, covariancia = iniciar(medicao())
    with pytest.raises(ValueError, match="cx, cy, a, h"):
        atualizar(media, covariancia, np.array([1.0, 2.0]))


def test_iniciar_com_forma_errada_e_recusado():
    with pytest.raises(ValueError, match="cx, cy, a, h"):
        iniciar(np.zeros(3))


# --- ruído proporcional ao tamanho ----------------------------------------------


def test_caixa_pequena_recebe_menos_incerteza_absoluta():
    """Incerteza ancorada na altura da caixa: pessoa longe ocupa poucos pixels, e alguns
    pixels de erro ali são metros no chão. O filtro confia mais em quem está perto — que
    é quem está atravessando a linha de saída."""
    perto = projetar(*iniciar(medicao(altura=300.0)))[1]
    longe = projetar(*iniciar(medicao(altura=40.0)))[1]
    assert np.trace(longe) < np.trace(perto)


# --- a caixa não pode virar do avesso -------------------------------------------


def test_altura_nunca_fica_negativa_extrapolando():
    """**Bug encontrado em vídeo real, não hipotético.** Quem se afasta da câmera tem a
    altura da caixa diminuindo; o filtro aprende `vh` negativa e, extrapolando durante
    uma oclusão de segundos, a altura cruza o zero. A caixa sai com `y2 < y1` e
    atravessa o pipeline sem estourar — a IoU dá zero, o desenho some, e a
    `base_central` que o §3.4 testa contra a linha de saída aponta para o lugar errado.
    """
    media, covariancia = iniciar(medicao(altura=200.0))
    for i in range(8):  # pessoa se afastando: a caixa encolhe a cada frame
        media, covariancia = prever(media, covariancia, PASSO_S)
        media, covariancia = atualizar(media, covariancia, medicao(altura=200.0 - 20.0 * i))

    for _ in range(30):  # e some, com o filtro extrapolando sozinho
        media, covariancia = prever(media, covariancia, PASSO_S)
        assert media[3] > 0.0, "a altura virou negativa"

    x1, y1, x2, y2 = para_xyxy(media)
    assert x1 < x2 and y1 < y2


def test_velocidade_zera_quando_o_piso_e_atingido():
    """Sem isso, `vh` continuaria empurrando contra o limite a cada frame e a caixa
    ficaria colada no mínimo em vez de voltar a crescer quando a pessoa reaparecesse."""
    media, covariancia = iniciar(medicao(altura=200.0))
    media[3] = 2.0
    media[7] = -500.0  # encolhendo muito rápido

    media, _ = prever(media, covariancia, PASSO_S)
    assert media[3] > 0.0
    assert media[7] == 0.0
