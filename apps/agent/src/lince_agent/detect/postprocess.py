"""Da saída crua do modelo às caixas que o estágio 3 consome.

Duas responsabilidades, separadas de propósito:

**Decodificar.** A saída do YOLOX não são caixas — são deslocamentos relativos a uma
grade de âncoras, em unidades de célula. Somar a grade errada, ou trocar a ordem dos
strides, produz caixas no lugar errado sem nenhum erro. A grade é a mesma para todo
frame, então é construída uma vez e memoizada.

**Suprimir.** O modelo emite várias caixas quase idênticas para a mesma pessoa. Sem
NMS, um cliente sozinho no corredor vira seis tracks no §3.3, e seis tracks viram
eventos repetidos no §3.4 — a rajada que o R-1 diz que mata o produto.

Tudo aqui é NumPy puro e função pura: nem torch nem torchvision entram no box só para
calcular interseção de retângulos.
"""

from __future__ import annotations

from functools import lru_cache

import numpy as np

STRIDES = (8, 16, 32)
"""Os três níveis da FPN do YOLOX. A ordem importa: é a ordem em que o modelo
concatena as saídas dos níveis, e trocá-la embaralha as caixas entre escalas."""

CLASSE_PESSOA = 0
"""Índice de `person` no COCO. O §3.2 limita o modelo a pessoas e objetos, e o MVP
só precisa de pessoas — o resto é inferência paga e descartada."""


class PostprocessError(ValueError):
    """Saída de modelo que não tem a forma esperada."""


@lru_cache(maxsize=8)
def _grade(input_size: int) -> tuple[np.ndarray, np.ndarray]:
    """Centros das células e o stride de cada âncora, para um tamanho de entrada.

    Memoizada porque não muda nunca: remontar 8.400 âncoras a 3 fps × 8 câmeras seria
    24 mil linhas de meshgrid por segundo para chegar sempre ao mesmo array.
    """
    centros: list[np.ndarray] = []
    strides: list[np.ndarray] = []
    for stride in STRIDES:
        if input_size % stride:
            raise PostprocessError(
                f"entrada de {input_size} não é divisível pelo stride {stride}: "
                f"a grade do YOLOX exige múltiplo de {max(STRIDES)}"
            )
        lado = input_size // stride
        x, y = np.meshgrid(np.arange(lado), np.arange(lado))
        centros.append(np.stack((x, y), axis=2).reshape(-1, 2).astype(np.float32))
        strides.append(np.full((lado * lado, 1), stride, dtype=np.float32))

    grade = np.concatenate(centros, axis=0)
    passos = np.concatenate(strides, axis=0)
    # Somente leitura porque o resultado é compartilhado por todas as chamadas: quem
    # escrevesse nele envenenaria a decodificação de todos os frames seguintes, de
    # todas as câmeras, sem nenhum sinal.
    grade.flags.writeable = False
    passos.flags.writeable = False
    return grade, passos


def decode_yolox(output: np.ndarray, *, input_size: int) -> tuple[np.ndarray, np.ndarray]:
    """Converte a saída crua em caixas `xyxy` no espaço do modelo e scores por classe.

    Espera `(1, âncoras, 5 + classes)` ou `(âncoras, 5 + classes)`, com as colunas na
    ordem do YOLOX: `cx, cy, w, h, objectness, classes...`. As duas primeiras estão em
    unidades de célula e as duas seguintes em log — daí a soma da grade e a
    exponencial.

    O score final é `objectness × probabilidade da classe`, e não uma das duas: a
    objectness sozinha aceita a caixa que o modelo tem certeza que contém *algo*, e a
    probabilidade sozinha aceita a caixa vazia cuja melhor aposta é "pessoa".
    """
    bruto = np.asarray(output, dtype=np.float32)
    if bruto.ndim == 3 and bruto.shape[0] == 1:
        bruto = bruto[0]
    if bruto.ndim != 2 or bruto.shape[1] < 6:
        raise PostprocessError(
            f"esperava (âncoras, 5 + classes) do YOLOX, recebi {np.asarray(output).shape}"
        )

    centros, strides = _grade(input_size)
    if bruto.shape[0] != centros.shape[0]:
        raise PostprocessError(
            f"o modelo devolveu {bruto.shape[0]} âncoras e uma entrada de {input_size} "
            f"produz {centros.shape[0]}. Modelo e `input_size` estão divergentes"
        )

    # Sem escrever em `bruto`: a saída da sessão ONNX pode ser reaproveitada pelo
    # runtime entre inferências, e mutá-la corromperia o frame seguinte.
    centro_xy = (bruto[:, 0:2] + centros) * strides
    tamanho_wh = np.exp(bruto[:, 2:4]) * strides

    metade = tamanho_wh / 2.0
    caixas = np.empty((bruto.shape[0], 4), dtype=np.float32)
    caixas[:, 0:2] = centro_xy - metade
    caixas[:, 2:4] = centro_xy + metade

    scores = bruto[:, 4:5] * bruto[:, 5:]
    return caixas, scores


def melhor_classe(scores: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Reduz a matriz de scores por classe ao par (classe vencedora, confiança).

    Uma caixa, uma classe: o §3.4 raciocina sobre pessoas, não sobre distribuições de
    probabilidade.
    """
    if scores.size == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float32)
    return np.argmax(scores, axis=1), np.max(scores, axis=1)


def filtra(
    caixas: np.ndarray,
    class_ids: np.ndarray,
    confiancas: np.ndarray,
    *,
    score_threshold: float,
    classes: tuple[int, ...] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Corta por confiança e por classe de interesse, nesta ordem.

    O corte por classe vem antes do NMS de propósito: suprimir contra caixas de
    classes que vão ser descartadas de qualquer jeito gasta trabalho e, pior, deixa
    uma caixa de "pessoa" ser suprimida por uma de "mochila" no mesmo lugar.
    """
    mantem = confiancas >= score_threshold
    if classes is not None:
        mantem &= np.isin(class_ids, np.asarray(classes))
    return caixas[mantem], class_ids[mantem], confiancas[mantem]


def iou(caixa: np.ndarray, outras: np.ndarray) -> np.ndarray:
    """Interseção sobre união de uma caixa contra várias, em `xyxy`."""
    x1 = np.maximum(caixa[0], outras[:, 0])
    y1 = np.maximum(caixa[1], outras[:, 1])
    x2 = np.minimum(caixa[2], outras[:, 2])
    y2 = np.minimum(caixa[3], outras[:, 3])

    intersecao = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area = (caixa[2] - caixa[0]) * (caixa[3] - caixa[1])
    areas = (outras[:, 2] - outras[:, 0]) * (outras[:, 3] - outras[:, 1])
    uniao = area + areas - intersecao
    # União zero é caixa degenerada (largura ou altura nula). Dividir daria `nan`, e
    # `nan >= limiar` é falso — a caixa sobreviveria à supressão em silêncio.
    return np.where(uniao > 0, intersecao / np.where(uniao > 0, uniao, 1.0), 0.0)


def nms(
    caixas: np.ndarray,
    confiancas: np.ndarray,
    class_ids: np.ndarray,
    *,
    iou_threshold: float,
) -> np.ndarray:
    """Supressão não-máxima **por classe**, devolvendo os índices que sobrevivem.

    Por classe, e não global, porque duas classes diferentes no mesmo lugar são o caso
    normal — uma pessoa carregando uma mochila ocupa o mesmo retângulo. Suprimir entre
    classes apagaria uma das duas.

    Os índices saem em ordem decrescente de confiança, que é a ordem em que o §3.3
    quer as detecções: o tracker associa primeiro o que o modelo tem mais certeza.
    """
    if caixas.shape[0] == 0:
        return np.zeros(0, dtype=np.int64)

    sobreviventes: list[int] = []
    for classe in np.unique(class_ids):
        (indices,) = np.nonzero(class_ids == classe)
        indices = indices[np.argsort(-confiancas[indices], kind="stable")]
        while indices.size:
            melhor, restantes = indices[0], indices[1:]
            sobreviventes.append(int(melhor))
            if restantes.size == 0:
                break
            indices = restantes[iou(caixas[melhor], caixas[restantes]) <= iou_threshold]

    ordem = np.array(sobreviventes, dtype=np.int64)
    return ordem[np.argsort(-confiancas[ordem], kind="stable")]
