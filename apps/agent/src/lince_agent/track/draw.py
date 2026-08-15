"""Desenhar tracks sobre o frame, para poder **ver** o estágio 3 funcionando.

Existe porque troca de ID — o R-2, o risco crítico deste estágio — não aparece em
contador nenhum: o número de tracks sobe igual quando o tracker acerta e quando erra.
A única forma de saber se está certo é olhar.

**Cor por ID, e rastro dos centros.** Sem número escrito, que sem OpenCV daria trabalho,
e sem perda: uma troca de ID aparece como a cor de uma pessoa mudando no meio do
corredor, que salta aos olhos mais do que um dígito trocando. O rastro mostra o percurso
que o §3.4 vai avaliar contra a linha de saída.

NumPy puro, como o letterbox do §3.2 — o agente inteiro depende de `numpy`, `redis`,
`onnxruntime` e `scipy`, e não vale acrescentar uma biblioteca de imagem para desenhar
retângulo.
"""

from __future__ import annotations

import numpy as np

from lince_agent.track.state import Track, TrackStatus

PALETA: tuple[tuple[int, int, int], ...] = (
    (66, 135, 245),
    (80, 200, 120),
    (0, 165, 255),
    (200, 80, 200),
    (255, 200, 0),
    (60, 60, 220),
    (180, 220, 60),
    (140, 100, 240),
)
"""Cores em BGR, que é o layout do frame. Oito bastam: uma loja de bairro raramente tem
mais que isso em quadro, e cores demais ficariam parecidas entre si — o que anularia o
propósito de detectar a troca pelo olho."""

ESPESSURA = 2
RAIO_DO_PONTO = 3


def cor_de(track_id: int) -> tuple[int, int, int]:
    """Sempre a mesma cor para o mesmo ID, em qualquer execução. Determinismo importa:
    comparar dois vídeos do mesmo trecho só faz sentido se as cores baterem."""
    return PALETA[track_id % len(PALETA)]


def desenha(
    imagem: np.ndarray,
    tracks: tuple[Track, ...],
    rastros: dict[int, list[tuple[float, float]]] | None = None,
) -> np.ndarray:
    """Devolve uma **cópia** do frame com as caixas, os pés e os rastros.

    Cópia, e não desenho no lugar: `Frame.as_array()` é uma vista somente leitura sobre
    os bytes do pipe, e o mesmo frame ainda pode ser lido por outro consumidor.
    """
    if imagem.ndim != 3 or imagem.shape[2] != 3:
        raise ValueError(f"esperava uma imagem (altura, largura, 3), recebi {imagem.shape}")

    tela = np.array(imagem, dtype=np.uint8, copy=True)
    altura, largura = tela.shape[0], tela.shape[1]

    for track in tracks:
        if track.status is TrackStatus.ENCERRADO:
            continue
        cor = cor_de(track.track_id)
        # Track perdido sai pontilhado — pela cor mais fraca —, porque ali a caixa é
        # previsão do Kalman e não observação. Confundir as duas na tela levaria alguém
        # a culpar o detector por um erro do tracker.
        intensidade = 0.45 if track.status is TrackStatus.PERDIDO else 1.0
        _retangulo(tela, track, tuple(int(c * intensidade) for c in cor))

        px, py = track.base_central
        _ponto(tela, px, py, cor, largura, altura)

        for x, y in (rastros or {}).get(track.track_id, ()):
            _ponto(tela, x, y, cor, largura, altura, raio=1)

    return tela


def _retangulo(tela: np.ndarray, track: Track, cor) -> None:
    altura, largura = tela.shape[0], tela.shape[1]
    x1 = int(np.clip(track.x1, 0, largura - 1))
    y1 = int(np.clip(track.y1, 0, altura - 1))
    x2 = int(np.clip(track.x2, x1 + 1, largura))
    y2 = int(np.clip(track.y2, y1 + 1, altura))

    for borda in range(ESPESSURA):
        if y1 + borda < y2:
            tela[y1 + borda, x1:x2] = cor
        if y2 - borda - 1 >= y1:
            tela[y2 - borda - 1, x1:x2] = cor
        if x1 + borda < x2:
            tela[y1:y2, x1 + borda] = cor
        if x2 - borda - 1 >= x1:
            tela[y1:y2, x2 - borda - 1] = cor


def _ponto(
    tela: np.ndarray,
    x: float,
    y: float,
    cor,
    largura: int,
    altura: int,
    *,
    raio: int = RAIO_DO_PONTO,
) -> None:
    cx = int(np.clip(x, 0, largura - 1))
    cy = int(np.clip(y, 0, altura - 1))
    tela[max(0, cy - raio) : cy + raio + 1, max(0, cx - raio) : cx + raio + 1] = cor


class Rastros:
    """Histórico curto do ponto de referência de cada track.

    Guarda a **base central** — os pés —, não o centro da caixa: é o ponto que o §3.4 vai
    testar contra a linha de saída, então é ele que precisa ser visto no vídeo.
    """

    def __init__(self, maximo: int = 24) -> None:
        self._maximo = maximo
        self._pontos: dict[int, list[tuple[float, float]]] = {}

    def registra(self, tracks: tuple[Track, ...]) -> dict[int, list[tuple[float, float]]]:
        vivos = set()
        for track in tracks:
            if track.status is TrackStatus.ENCERRADO:
                continue
            vivos.add(track.track_id)
            pontos = self._pontos.setdefault(track.track_id, [])
            pontos.append(track.base_central)
            del pontos[: -self._maximo]

        # Track que sumiu não deixa rastro pendurado: numa gravação longa isso viraria
        # uma teia de linhas de gente que já saiu.
        for track_id in list(self._pontos):
            if track_id not in vivos:
                del self._pontos[track_id]
        return self._pontos
