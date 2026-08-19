"""Cenário de loja compartilhado pelos testes do estágio 4.

A planta é sempre a mesma, e é a de uma câmera apontada para a porta de dentro da
loja: a rua fica em cima do frame, a loja embaixo, e o caixa é um retângulo à
esquerda. Fixar a planta em um lugar só é o que permite a cada teste ser sobre **uma**
coisa — o sentido da travessia, o tempo no caixa, a idade do track — em vez de sobre
a leitura de mais um punhado de coordenadas.
"""

from __future__ import annotations

from lince_agent.config import RuleOptions
from lince_agent.rules.geometry import LinhaOrientada, Poligono, Ponto
from lince_agent.track.state import Track, TrackingResult, TrackStatus

PASSO_S = 1 / 3
"""Intervalo entre frames avaliados, a 3 fps (§3.2)."""

LINHA = LinhaOrientada(origem=(0.0, 400.0), destino=(640.0, 400.0))
"""Desenhada da esquerda para a direita: dentro da loja é `y > 400`, a rua é `y < 400`."""

CAIXA = Poligono(((100.0, 420.0), (340.0, 420.0), (340.0, 478.0), (100.0, 478.0)))
"""Uma posição de caixa, dentro da loja e à esquerda da porta."""

NO_CAIXA: Ponto = (200.0, 450.0)
LONGE_DO_CAIXA: Ponto = (520.0, 450.0)
"""Mesma altura, do outro lado do corredor: quem fica aqui não passou pelo caixa."""

NA_RUA: Ponto = (520.0, 360.0)


def opcoes(**kwargs) -> RuleOptions:
    padrao = {
        "enabled": True,
        "linha_saida": LINHA,
        "zonas_caixa": (CAIXA,),
        "tempo_caixa_min_s": 5.0,
        "vida_min_s": 2.0,
        "hits_min": 3,
    }
    return RuleOptions(**{**padrao, **kwargs})


def track(
    track_id: int,
    ponto: Ponto,
    *,
    age_s: float,
    hits: int,
    camera_id: str = "cam1",
    status: TrackStatus = TrackStatus.CONFIRMADO,
    time_since_update_s: float = 0.0,
) -> Track:
    """Um track cuja `base_central` — os pés — cai exatamente em `ponto`.

    O motor de regras avalia os pés, não o centro do corpo (§3.4), então é o ponto que
    o teste quer controlar. A caixa é construída em volta dele com proporção de gente
    em pé.
    """
    x, y = ponto
    altura = 200.0
    largura = altura * 0.4
    return Track(
        track_id=track_id,
        camera_id=camera_id,
        status=status,
        x1=x - largura / 2,
        y1=y - altura,
        x2=x + largura / 2,
        y2=y,
        score=0.9,
        hits=hits,
        age_s=age_s,
        time_since_update_s=time_since_update_s,
        first_seen_at=0.0,
        last_seen_at=age_s,
    )


def frame(
    tracks: tuple[Track, ...],
    *,
    camera_id: str = "cam1",
    sequence: int = 1,
    received_at: float = 0.0,
) -> TrackingResult:
    return TrackingResult(
        camera_id=camera_id,
        sequence=sequence,
        received_at=received_at,
        tracks=tracks,
        tracking_ms=0.5,
    )


def caminho(de: Ponto, ate: Ponto, passos: int) -> list[Ponto]:
    """`passos` pontos de `de` até `ate`, inclusive nas duas pontas."""
    if passos < 2:
        raise ValueError("um caminho precisa de ao menos duas amostras")
    (x1, y1), (x2, y2) = de, ate
    return [
        (x1 + (x2 - x1) * i / (passos - 1), y1 + (y2 - y1) * i / (passos - 1))
        for i in range(passos)
    ]


def percorre(
    motor,
    pontos: list[Ponto],
    *,
    track_id: int = 1,
    camera_id: str = "cam1",
    inicio: float = 0.0,
    idade_inicial: float = 0.0,
    sequence_inicial: int = 1,
):
    """Anda uma pessoa pelo caminho, um frame a 3 fps por ponto.

    Idade e associações crescem junto com os frames, como acontece de verdade — é o que
    faz um caminho curto produzir um track jovem sem o teste precisar dizer isso.
    """
    gatilhos = []
    for i, ponto in enumerate(pontos):
        pessoa = track(
            track_id,
            ponto,
            age_s=idade_inicial + i * PASSO_S,
            hits=i + 1,
            camera_id=camera_id,
        )
        gatilhos.extend(
            motor.update(
                frame(
                    (pessoa,),
                    camera_id=camera_id,
                    sequence=sequence_inicial + i,
                    received_at=inicio + i * PASSO_S,
                )
            )
        )
    return tuple(gatilhos)
