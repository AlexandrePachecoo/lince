"""Filtro de Kalman de velocidade constante, no espaço `xyah`.

Estado de 8 dimensões: centro `(cx, cy)`, proporção `a = largura/altura`, altura `h`, e
as quatro velocidades. A medição são as quatro primeiras — o detector observa onde a
caixa está, nunca quão rápido ela vai.

**Por que isto existe, e não é ornamento.** A 3 fps passam ~333 ms entre frames
avaliados, e uma pessoa andando a 1,4 m/s se desloca bastante nesse tempo. Numa câmera de
corredor, a IoU entre duas caixas cruas consecutivas da *mesma* pessoa pode ficar baixa a
ponto de não passar pelo gate da associação. Quem sustenta o casamento é a previsão: o
tracker não compara a caixa nova com onde a pessoa **estava**, e sim com onde ela
**deveria estar agora**.

**O `dt` é tempo real, não "um frame".** Toda implementação de referência assume cadência
fixa e usa `dt = 1`. Aqui a fila do §3.2 descarta o frame mais antigo quando a inferência
não acompanha, então o intervalo entre dois frames que chegam ao tracker varia com a
carga do box. Com `dt` fixo, a extrapolação erraria exatamente quando o box está
saturado — que é quando o sistema já está mais frágil. O `dt` sai da diferença de
`Frame.received_at`, o monotônico da borda que o §3.1 garante.

Tudo aqui é função pura sobre arrays: nenhum estado, nenhum relógio, nenhum track. É o
que permite testar a física sozinha, sem montar um tracker.
"""

from __future__ import annotations

import numpy as np

DIM = 4
"""Dimensões observadas: cx, cy, a, h."""

PESO_POSICAO = 1.0 / 20.0
"""Ruído de **medição**, proporcional à altura da caixa. Não é arbitrário: pessoa longe
da câmera ocupa poucos pixels, e alguns pixels de erro ali são metros de erro no chão.
Ancorar no tamanho aparente faz o filtro confiar mais em quem está perto — que é
justamente quem está atravessando a linha de saída."""

PESO_ACELERACAO = 0.5
"""Densidade do ruído de **processo**, em alturas de caixa por segundo ao quadrado.

Uma pessoa muda de velocidade em torno de 1 m/s num terço de segundo ao começar ou parar
de andar. Com 1,70 m ocupando `h` pixels, isso é da ordem de meia altura por segundo ao
quadrado — que é de onde este número sai, e não de calibração cega.

**As implementações de referência usam outra coisa aqui**, e copiá-las quebra: elas
somam ruído diagonal fixo em posição e velocidade, calibrado por frame a 30 fps, onde o
deslocamento entre frames é de poucos pixels. A 3 fps esse atalho falha duas vezes — foi
medido, não suposto. Com o ruído por frame, o filtro recebia um décimo do ruído por
segundo e estimava 70 px/s para alguém a 90. Corrigindo só a escala, o ruído de posição
passava a explicar todo o movimento sozinho e a velocidade estimada caía para 18 px/s: o
filtro seguia a caixa perfeitamente e não aprendia nada sobre para onde a pessoa vai —
que é a única coisa que interessa para associar o frame seguinte."""

RUIDO_PROPORCAO = 1e-2
"""A proporção largura/altura de uma pessoa em pé quase não muda. Ruído pequeno aqui
impede o filtro de tratar o tremor da caixa do detector como deformação real."""

VELOCIDADE_INICIAL = 1.5
"""Incerteza inicial da velocidade, em alturas por segundo — larga de propósito. No
primeiro frame o filtro não sabe para onde a pessoa vai, e uma prior estreita em zero
faria os primeiros frames de um track resistirem ao movimento real."""

ALTURA_MINIMA = 1.0
PROPORCAO_MINIMA = 0.05
"""Piso do tamanho da caixa. O estado do filtro é livre: nada nas equações impede a
altura de cruzar o zero e ficar negativa.

Não é hipótese — apareceu em vídeo real. Quem se afasta da câmera tem a altura da caixa
diminuindo, o filtro aprende `vh` negativa, e um track perdido extrapolado por até
`max_perdido_s` continua encolhendo até virar do avesso. O resultado é uma caixa com
`x2 < x1`, que atravessa o pipeline sem estourar: a IoU dela dá zero (por sorte, porque o
§3.2 já trata união zero), o desenho some, e a `base_central` que o §3.4 vai testar contra
a linha de saída passa a apontar para o lugar errado.

As implementações de referência não precisam disto porque a 30 fps a extrapolação dura
poucos milissegundos. A 3 fps, com oclusão de segundos, precisa."""


def _transicao(dt: float) -> np.ndarray:
    """Matriz de movimento para um intervalo de `dt` segundos.

    A forma `F(dt1) @ F(dt2) == F(dt1 + dt2)` é o que garante que dois frames a 333 ms
    levem a média ao mesmo lugar que um frame a 666 ms — a propriedade que o descarte do
    §3.2 exige e que o teste verifica.
    """
    matriz = np.eye(2 * DIM, dtype=np.float64)
    for i in range(DIM):
        matriz[i, DIM + i] = dt
    return matriz


def iniciar(medicao: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Primeiro frame de um track: posição observada, velocidade desconhecida.

    A velocidade entra em zero com incerteza alta — o filtro não sabe para onde a pessoa
    vai, e fingir que sabe seria pior que admitir a ignorância.
    """
    medicao = np.asarray(medicao, dtype=np.float64)
    if medicao.shape != (DIM,):
        raise ValueError(f"esperava uma medição (cx, cy, a, h), recebi {medicao.shape}")

    media = np.concatenate([medicao, np.zeros(DIM)])
    altura = medicao[3]
    desvio = np.array(
        [
            2 * PESO_POSICAO * altura,
            2 * PESO_POSICAO * altura,
            2 * RUIDO_PROPORCAO,
            2 * PESO_POSICAO * altura,
            VELOCIDADE_INICIAL * altura,
            VELOCIDADE_INICIAL * altura,
            RUIDO_PROPORCAO,
            VELOCIDADE_INICIAL * altura,
        ]
    )
    return media, np.diag(np.square(desvio))


def prever(media: np.ndarray, covariancia: np.ndarray, dt: float) -> tuple[np.ndarray, np.ndarray]:
    """Avança o estado `dt` segundos. Sem medição nenhuma: só o modelo de movimento."""
    if dt < 0:
        raise ValueError(f"dt não pode ser negativo, recebi {dt}")

    transicao = _transicao(dt)
    ruido = _ruido_de_processo(media[3], dt)
    media = transicao @ media
    covariancia = transicao @ covariancia @ transicao.T + ruido
    return _restringe(media), covariancia


def _restringe(media: np.ndarray) -> np.ndarray:
    """Impede a caixa de virar do avesso. Ver `ALTURA_MINIMA`.

    Zerar a velocidade junto com o piso é o que evita o filtro insistir: sem isso, `vh`
    continuaria negativa empurrando contra o limite a cada frame, e a caixa ficaria
    colada no mínimo em vez de voltar a crescer quando a pessoa reaparecesse.
    """
    for indice, piso in ((2, PROPORCAO_MINIMA), (3, ALTURA_MINIMA)):
        if media[indice] < piso:
            media = media.copy()
            media[indice] = piso
            media[DIM + indice] = 0.0
    return media


def _ruido_de_processo(altura: float, dt: float) -> np.ndarray:
    """Ruído de processo do modelo de aceleração branca, discretizado em `dt`.

    A forma `[[dt³/3, dt²/2], [dt²/2, dt]]` por eixo é o que **acopla** posição e
    velocidade: a incerteza de posição cresce porque a velocidade é incerta, não por
    decreto. É essa diferença que faz o filtro atribuir o deslocamento observado à
    velocidade em vez de a um ruído de posição inventado — ver `PESO_ACELERACAO`.
    """
    densidade = np.array(
        [
            (PESO_ACELERACAO * altura) ** 2,
            (PESO_ACELERACAO * altura) ** 2,
            RUIDO_PROPORCAO**2,
            (PESO_ACELERACAO * altura) ** 2,
        ]
    )
    ruido = np.zeros((2 * DIM, 2 * DIM), dtype=np.float64)
    for i in range(DIM):
        ruido[i, i] = densidade[i] * dt**3 / 3
        ruido[i, DIM + i] = densidade[i] * dt**2 / 2
        ruido[DIM + i, i] = densidade[i] * dt**2 / 2
        ruido[DIM + i, DIM + i] = densidade[i] * dt
    return ruido


def projetar(media: np.ndarray, covariancia: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Leva o estado para o espaço da medição, somando o ruído do detector.

    O detector não entrega a caixa exata: ela treme entre frames mesmo com a pessoa
    parada. Este é o ruído que impede o filtro de tratar esse tremor como movimento.
    """
    altura = media[3]
    desvio = np.array([PESO_POSICAO * altura, PESO_POSICAO * altura, 1e-1, PESO_POSICAO * altura])
    return media[:DIM], covariancia[:DIM, :DIM] + np.diag(np.square(desvio))


def atualizar(
    media: np.ndarray, covariancia: np.ndarray, medicao: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Corrige a previsão com a caixa que o detector realmente entregou."""
    medicao = np.asarray(medicao, dtype=np.float64)
    if medicao.shape != (DIM,):
        raise ValueError(f"esperava uma medição (cx, cy, a, h), recebi {medicao.shape}")

    media_proj, cov_proj = projetar(media, covariancia)

    # Ganho de Kalman: K = P Hᵀ S⁻¹, resolvido em vez de invertido. Inverter uma matriz
    # de covariância mal condicionada — o que acontece quando um track fica muitos frames
    # sem medição — produz número sem sentido em vez de erro.
    cruzada = covariancia[:, :DIM]
    ganho = np.linalg.solve(cov_proj, cruzada.T).T

    inovacao = medicao - media_proj
    media = media + ganho @ inovacao
    covariancia = covariancia - ganho @ cov_proj @ ganho.T
    return _restringe(media), covariancia


def para_xyah(x1: float, y1: float, x2: float, y2: float) -> np.ndarray:
    """Caixa `xyxy` do detector para a medição do filtro."""
    largura = x2 - x1
    altura = y2 - y1
    if altura <= 0:
        raise ValueError(f"caixa de altura não positiva: ({x1}, {y1}, {x2}, {y2})")
    return np.array([x1 + largura / 2, y1 + altura / 2, largura / altura, altura])


def para_xyxy(media: np.ndarray) -> tuple[float, float, float, float]:
    """Estado do filtro de volta para `xyxy`, que é o que o §3.4 e o clipe entendem."""
    cx, cy, proporcao, altura = media[:DIM]
    largura = proporcao * altura
    return (
        float(cx - largura / 2),
        float(cy - altura / 2),
        float(cx + largura / 2),
        float(cy + altura / 2),
    )
