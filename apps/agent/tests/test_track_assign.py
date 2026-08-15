"""A associação, e a razão de ela ser ótima em vez de gulosa.

O teste central deste arquivo não afirma que o ótimo é melhor — ele **mostra**, rodando
um guloso ao lado e provando que ele perde um track no cenário que o §3.3 nomeia como a
primeira falha esperada: cruzamento de pessoas.
"""

from __future__ import annotations

import numpy as np
import pytest

from lince_agent.track.assign import SEM_PAR, associa, custo_por_iou, resolve


def caixa(x: float, y: float = 0.0, lado: float = 100.0) -> list[float]:
    return [x, y, x + lado, y + lado]


def guloso(custo: np.ndarray, *, custo_maximo: float) -> list[tuple[int, int]]:
    """Um guloso de referência: casa o melhor par disponível e segue.

    Existe só para o teste do cruzamento. É a implementação que não foi escolhida, e
    tê-la aqui é o que transforma "o ótimo é melhor" de afirmação em demonstração.
    """
    pares: list[tuple[int, int]] = []
    linhas_livres = set(range(custo.shape[0]))
    colunas_livres = set(range(custo.shape[1]))

    ordem = sorted(
        ((custo[i, j], i, j) for i in range(custo.shape[0]) for j in range(custo.shape[1])),
        key=lambda item: item[0],
    )
    for valor, linha, coluna in ordem:
        if valor > custo_maximo:
            break
        if linha in linhas_livres and coluna in colunas_livres:
            pares.append((linha, coluna))
            linhas_livres.discard(linha)
            colunas_livres.discard(coluna)
    return pares


# --- a matriz de custo ----------------------------------------------------------


def test_custo_e_um_menos_iou():
    tracks = np.array([caixa(0.0)], dtype=np.float64)
    dets = np.array([caixa(0.0), caixa(1000.0)], dtype=np.float64)

    custo = custo_por_iou(tracks, dets)
    assert custo[0, 0] == pytest.approx(0.0), "caixa idêntica tem custo zero"
    assert custo[0, 1] == pytest.approx(SEM_PAR), "caixa distante tem custo máximo"


def test_custo_de_matriz_vazia_tem_a_forma_certa():
    """Frame sem ninguém é o caso comum numa loja de bairro. A forma precisa continuar
    coerente para quem for indexar."""
    assert custo_por_iou(np.zeros((0, 4)), np.array([caixa(0.0)])).shape == (0, 1)
    assert custo_por_iou(np.array([caixa(0.0)]), np.zeros((0, 4))).shape == (1, 0)


# --- o solver -------------------------------------------------------------------


def test_o_otimo_salva_o_track_que_o_guloso_perde():
    """**O teste que justifica o scipy.**

    Duas pessoas se cruzando. O track A sobrepõe bem as duas detecções; o track B só
    sobrepõe a primeira. O guloso pega A↔1, que é o melhor par isolado, e deixa B sem
    nada — B vira `PERDIDO`, e quando reaparecer no frame seguinte pode nascer com ID
    novo. É a troca de ID do R-2, produzida por uma escolha de algoritmo.

    O ótimo aceita um par pior para A e casa os dois.
    """
    #            det 1   det 2
    custo = np.array(
        [
            [0.10, 0.50],  # track A
            [0.15, SEM_PAR],  # track B
        ]
    )
    teto = SEM_PAR - 0.3

    pares_gulosos = guloso(custo, custo_maximo=teto)
    pares_otimos, tracks_livres, _ = resolve(custo, custo_maximo=teto)

    assert len(pares_gulosos) == 1, "o guloso casa só um par"
    assert sorted(pares_otimos) == [(0, 1), (1, 0)], "o ótimo casa os dois, trocando"
    assert tracks_livres == [], "nenhum track fica órfão"


def test_gate_desfaz_o_par_ruim_que_o_solver_foi_obrigado_a_fazer():
    """Sem alternativa, o solver casa o que sobrou — inclusive um track com alguém do
    outro lado do quadro. O gate é o que desfaz isso: par abaixo do mínimo não é
    casamento ruim, é não-casamento, e aceitá-lo teleportaria o ID."""
    custo = np.array([[SEM_PAR]])
    pares, tracks_livres, dets_livres = resolve(custo, custo_maximo=SEM_PAR - 0.3)

    assert pares == []
    assert (tracks_livres, dets_livres) == ([0], [0])


def test_par_exatamente_no_limiar_e_aceito():
    """O limiar é configuração por câmera (§3.4). Um `<` no lugar de `<=` faria o valor
    configurado significar outra coisa."""
    custo = np.array([[SEM_PAR - 0.3]])
    pares, _, _ = resolve(custo, custo_maximo=SEM_PAR - 0.3)
    assert pares == [(0, 0)]


def test_mais_tracks_que_deteccoes_deixa_tracks_livres():
    """Alguém saiu do quadro, ou o detector perdeu a pessoa. Os tracks órfãos precisam
    voltar identificados para virarem `PERDIDO`, não sumir."""
    custo = np.array([[0.1], [SEM_PAR], [SEM_PAR]])
    pares, tracks_livres, dets_livres = resolve(custo, custo_maximo=SEM_PAR - 0.3)

    assert pares == [(0, 0)]
    assert tracks_livres == [1, 2]
    assert dets_livres == []


def test_mais_deteccoes_que_tracks_deixa_deteccoes_livres():
    """Alguém entrou no quadro. As detecções livres são candidatas a track novo."""
    custo = np.array([[0.1, SEM_PAR, SEM_PAR]])
    pares, tracks_livres, dets_livres = resolve(custo, custo_maximo=SEM_PAR - 0.3)

    assert pares == [(0, 0)]
    assert tracks_livres == []
    assert dets_livres == [1, 2]


# --- a fachada ------------------------------------------------------------------


def test_associa_casa_caixas_sobrepostas():
    tracks = np.array([caixa(0.0), caixa(500.0)], dtype=np.float64)
    dets = np.array([caixa(505.0), caixa(5.0)], dtype=np.float64)

    pares, tracks_livres, dets_livres = associa(tracks, dets, iou_min=0.3)
    assert sorted(pares) == [(0, 1), (1, 0)]
    assert (tracks_livres, dets_livres) == ([], [])


def test_associa_sem_tracks_devolve_todas_as_deteccoes_livres():
    """O primeiro frame de uma câmera: não há track nenhum, e toda detecção é candidata
    a nascer."""
    dets = np.array([caixa(0.0), caixa(200.0)], dtype=np.float64)
    pares, tracks_livres, dets_livres = associa(np.zeros((0, 4)), dets, iou_min=0.3)

    assert pares == []
    assert tracks_livres == []
    assert dets_livres == [0, 1]


def test_associa_sem_deteccoes_devolve_todos_os_tracks_livres():
    tracks = np.array([caixa(0.0)], dtype=np.float64)
    pares, tracks_livres, dets_livres = associa(tracks, np.zeros((0, 4)), iou_min=0.3)

    assert pares == []
    assert tracks_livres == [0]
    assert dets_livres == []


def test_caixas_distantes_nao_se_casam():
    """A 3 fps a pessoa anda bastante entre frames, mas não atravessa a loja. Um par sem
    sobreposição nenhuma é gente diferente."""
    tracks = np.array([caixa(0.0)], dtype=np.float64)
    dets = np.array([caixa(600.0)], dtype=np.float64)

    pares, tracks_livres, dets_livres = associa(tracks, dets, iou_min=0.3)
    assert pares == []
    assert (tracks_livres, dets_livres) == ([0], [0])
