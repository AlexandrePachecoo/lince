"""Quem é quem: casar as caixas deste frame com os tracks que já existiam.

Duas decisões moram aqui, e as duas são sobre o R-2.

**Atribuição ótima, não gulosa.** O guloso casa o melhor par disponível e segue. Isso
acerta quase sempre e erra justamente onde dói: quando duas pessoas se cruzam, o melhor
par local pode roubar a detecção que era do outro track, e aí os dois trocam de ID —
"troca de ID em cruzamento de pessoas" é literalmente a primeira linha da tabela de
falhas do §3.3. O ótimo global minimiza o custo do conjunto, e é o que o teste do guloso
neste módulo demonstra.

**O gate vem antes do casamento, e é eliminatório.** Um par com IoU abaixo do mínimo não
é um casamento ruim que o solver deva evitar — é um não-casamento. Deixá-lo competir faz
o solver, na falta de alternativa, casar um track com uma pessoa do outro lado do quadro,
o que teleporta o ID. Por isso o gate é aplicado **depois** de resolver: o solver acha o
melhor arranjo, e só então os pares que não passam do mínimo são desfeitos.

A associação é só geometria. Nenhuma característica de aparência entra aqui, nem pode: o
§3.3 e o NFR-4 proíbem dado biométrico, e um descritor visual de pessoa é exatamente
isso.
"""

from __future__ import annotations

import numpy as np
from scipy.optimize import linear_sum_assignment

from lince_agent.detect.postprocess import iou

SEM_PAR = 1.0
"""Custo de um par sem nenhuma sobreposição. Como o custo é `1 - IoU`, este é o teto."""


def custo_por_iou(caixas_track: np.ndarray, caixas_det: np.ndarray) -> np.ndarray:
    """Matriz `(tracks, detecções)` com `1 - IoU`.

    Reusa o `iou` do §3.2 (`detect/postprocess.py`), que já é testado contra caixa
    degenerada — união zero ali vira `0.0` em vez de `nan`, e `nan` numa matriz de custo
    faz o solver escolher qualquer coisa.
    """
    if caixas_track.size == 0 or caixas_det.size == 0:
        return np.zeros((len(caixas_track), len(caixas_det)), dtype=np.float64)

    custo = np.empty((len(caixas_track), len(caixas_det)), dtype=np.float64)
    for i, caixa in enumerate(caixas_track):
        custo[i] = SEM_PAR - iou(caixa, caixas_det)
    return custo


def resolve(
    custo: np.ndarray, *, custo_maximo: float
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Atribuição ótima sobre uma matriz de custo, com gate.

    Separado de `associa` porque a propriedade que importa — o ótimo global vencer o
    guloso — é sobre a matriz, não sobre retângulos. O teste fica sobre números
    escolhidos a dedo em vez de sobre caixas construídas para dar a IoU certa.
    """
    linhas_total, colunas_total = custo.shape
    if linhas_total == 0 or colunas_total == 0:
        return [], list(range(linhas_total)), list(range(colunas_total))

    linhas, colunas = linear_sum_assignment(custo)
    pares = [
        (int(linha), int(coluna))
        for linha, coluna in zip(linhas, colunas, strict=True)
        if custo[linha, coluna] <= custo_maximo
    ]

    casadas_linha = {linha for linha, _ in pares}
    casadas_coluna = {coluna for _, coluna in pares}
    return (
        pares,
        [i for i in range(linhas_total) if i not in casadas_linha],
        [j for j in range(colunas_total) if j not in casadas_coluna],
    )


def associa(
    caixas_track: np.ndarray, caixas_det: np.ndarray, *, iou_min: float
) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Casa tracks com detecções e devolve `(pares, tracks_livres, dets_livres)`.

    Os pares vêm como índices `(track, detecção)` nos arrays recebidos.
    """
    if len(caixas_track) == 0 or len(caixas_det) == 0:
        return [], list(range(len(caixas_track))), list(range(len(caixas_det)))

    custo = custo_por_iou(caixas_track, caixas_det)
    return resolve(custo, custo_maximo=SEM_PAR - iou_min)
