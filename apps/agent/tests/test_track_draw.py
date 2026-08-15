"""O desenho dos tracks — a única forma de verificar o estágio 3 com o olho.

Troca de ID não aparece em contador: o número de tracks sobe igual quando o tracker
acerta e quando erra. No vídeo anotado, cada pessoa tem uma cor, e uma troca aparece como
a cor mudando no meio do corredor. Estes testes guardam as propriedades de que essa
leitura depende — cor estável por ID, rastro no ponto certo, e o frame original intacto.
"""

from __future__ import annotations

import numpy as np
import pytest

from lince_agent.track.draw import PALETA, Rastros, cor_de, desenha
from lince_agent.track.state import Track, TrackStatus

ALTURA, LARGURA = 480, 640


def tela() -> np.ndarray:
    return np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)


def track(
    track_id: int = 1,
    *,
    status: TrackStatus = TrackStatus.CONFIRMADO,
    x1: float = 100.0,
    y1: float = 50.0,
    x2: float = 180.0,
    y2: float = 250.0,
) -> Track:
    return Track(
        track_id=track_id,
        camera_id="cam1",
        status=status,
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        score=0.9,
        hits=5,
        age_s=1.6,
        time_since_update_s=0.0,
        first_seen_at=0.0,
        last_seen_at=1.6,
    )


# --- cores ----------------------------------------------------------------------


def test_mesmo_id_sempre_recebe_a_mesma_cor():
    """Determinismo importa: comparar dois vídeos do mesmo trecho só faz sentido se as
    cores baterem entre execuções."""
    assert cor_de(7) == cor_de(7)
    assert cor_de(7) == cor_de(7 + len(PALETA))


def test_ids_vizinhos_recebem_cores_diferentes():
    """Duas pessoas em quadro ao mesmo tempo costumam ter IDs consecutivos. Se caíssem
    na mesma cor, uma troca entre elas ficaria invisível — que é o único erro que este
    vídeo existe para revelar."""
    assert len({cor_de(i) for i in range(len(PALETA))}) == len(PALETA)


# --- o desenho ------------------------------------------------------------------


def test_o_frame_original_nao_e_alterado():
    """`Frame.as_array()` é vista somente leitura sobre os bytes do pipe, e o mesmo frame
    ainda pode ser lido por outro consumidor. Desenhar no lugar quebraria os dois."""
    original = tela()
    copia = original.copy()
    desenha(original, (track(),))
    assert np.array_equal(original, copia)


def test_a_caixa_aparece_na_cor_do_track():
    pintada = desenha(tela(), (track(track_id=1),))
    assert tuple(pintada[50, 120]) == cor_de(1), "a borda de cima devia estar pintada"
    assert tuple(pintada[400, 500]) == (0, 0, 0), "o resto do quadro fica intocado"


def test_os_pes_sao_marcados():
    """A base central é o ponto que o §3.4 vai testar contra a linha de saída. Vê-lo no
    vídeo é o que permite conferir se a travessia seria detectada onde se espera."""
    marcada = desenha(tela(), (track(),))
    assert tuple(marcada[249, 140]) == cor_de(1)


def test_track_perdido_sai_mais_apagado():
    """Ali a caixa é previsão do Kalman, não observação. Desenhar igual levaria alguém a
    culpar o detector por um comportamento do tracker."""
    vivo = desenha(tela(), (track(track_id=1),))
    perdido = desenha(tela(), (track(track_id=1, status=TrackStatus.PERDIDO),))
    assert tuple(perdido[50, 120]) < tuple(vivo[50, 120])


def test_track_encerrado_nao_e_desenhado():
    limpa = desenha(tela(), (track(status=TrackStatus.ENCERRADO),))
    assert not limpa.any()


def test_caixa_que_transborda_e_recortada():
    """O tracker prevê a posição de quem está saindo do quadro, e a previsão pode passar
    da borda. Indexar fora do array com número negativo escreveria do outro lado da
    imagem em vez de estourar."""
    pintada = desenha(tela(), (track(x1=-50.0, y1=-30.0, x2=700.0, y2=520.0),))
    assert pintada.shape == (ALTURA, LARGURA, 3)
    assert pintada.any()


def test_imagem_com_forma_errada_e_recusada():
    with pytest.raises(ValueError, match=r"\(altura, largura, 3\)"):
        desenha(np.zeros((ALTURA, LARGURA), dtype=np.uint8), ())


def test_sem_tracks_devolve_o_frame_como_estava():
    original = tela()
    original[10, 10] = (1, 2, 3)
    assert np.array_equal(desenha(original, ()), original)


# --- rastros --------------------------------------------------------------------


def test_rastro_acumula_o_ponto_dos_pes():
    rastros = Rastros()
    for i in range(3):
        pontos = rastros.registra((track(y2=250.0 + 10 * i),))

    assert len(pontos[1]) == 3
    assert pontos[1][-1][1] == 270.0


def test_rastro_tem_teto():
    """Numa gravação de minutos, um rastro sem limite viraria uma linha atravessando o
    quadro inteiro e esconderia o movimento recente."""
    rastros = Rastros(maximo=5)
    for i in range(20):
        pontos = rastros.registra((track(y2=250.0 + i),))
    assert len(pontos[1]) == 5


def test_track_que_sumiu_nao_deixa_rastro_pendurado():
    """Sem isso, uma gravação longa acumularia uma teia de linhas de gente que já saiu."""
    rastros = Rastros()
    rastros.registra((track(track_id=1), track(track_id=2)))
    pontos = rastros.registra((track(track_id=1),))

    assert set(pontos) == {1}


def test_rastro_de_ids_diferentes_nao_se_mistura():
    rastros = Rastros()
    pontos = rastros.registra((track(track_id=1, y2=100.0), track(track_id=2, y2=400.0)))

    assert pontos[1][-1][1] == 100.0
    assert pontos[2][-1][1] == 400.0
