"""Poses sintéticas para os testes do gesto, em coordenadas do tronco.

O tronco é desenhado com centro `(cx, cy)` e comprimento `escala` (do meio dos
ombros ao meio dos quadris). Os pulsos são dados já normalizados — `(1.5, 0.0)`
é um tronco e meio para o lado da imagem em que x cresce — e convertidos para
pixel aqui. É assim que o teste diz a forma do gesto sem fazer conta de pixel.
"""

from __future__ import annotations

from lince_agent.pose.onnx import JUNTAS_COCO17, Junta, PosePessoa
from lince_agent.pose.worker import PessoaEstimada, ResultadoPose

_J = JUNTAS_COCO17.index


def pose_com(
    *,
    pulso_esquerdo: tuple[float, float] | None = None,
    pulso_direito: tuple[float, float] | None = None,
    cx: float = 320.0,
    cy: float = 240.0,
    escala: float = 100.0,
    tronco: bool = True,
) -> PosePessoa:
    """`None` num pulso é pulso não visto (confiança 0,05). `tronco=False` apaga
    ombros e quadris, que é o caso em que não há referência nenhuma."""
    juntas = [[cx, cy, 0.05] for _ in range(17)]

    def poe(nome: str, nx: float, ny: float, conf: float = 0.9) -> None:
        juntas[_J(nome)] = [cx + nx * escala, cy + ny * escala, conf]

    if tronco:
        poe("ombro_esquerdo", -0.35, -0.5)
        poe("ombro_direito", 0.35, -0.5)
        poe("quadril_esquerdo", -0.3, 0.5)
        poe("quadril_direito", 0.3, 0.5)
    if pulso_esquerdo is not None:
        poe("pulso_esquerdo", *pulso_esquerdo)
    if pulso_direito is not None:
        poe("pulso_direito", *pulso_direito)
    return PosePessoa(
        juntas=tuple(Junta(indice=i, x=x, y=y, confianca=c) for i, (x, y, c) in enumerate(juntas))
    )


def resultado(
    instante: float,
    *pessoas: tuple[int, PosePessoa],
    camera_id: str = "cam1",
    sequence: int | None = None,
) -> ResultadoPose:
    return ResultadoPose(
        camera_id=camera_id,
        received_at=instante,
        sequence=round(instante * 8) if sequence is None else sequence,
        pessoas=tuple(
            PessoaEstimada(track_id=t, x1=0.0, y1=0.0, x2=1.0, y2=1.0, pose=p) for t, p in pessoas
        ),
    )


def sequencia(
    motor,
    trajeto: list[tuple[float, float] | None],
    *,
    inicio: float = 0.0,
    fps: float = 8.0,
    track_id: int = 1,
    lado: str = "pulso_direito",
    **kwargs,
) -> list:
    """Alimenta o motor com um pulso seguindo `trajeto`, uma posição por frame."""
    disparos = []
    for i, posicao in enumerate(trajeto):
        pose = pose_com(**{lado: posicao}, **kwargs)
        disparos += motor.observa(resultado(inicio + i / fps, (track_id, pose)))
    return disparos
