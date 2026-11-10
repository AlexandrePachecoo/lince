"""A régua do tronco: onde está o pulso em relação ao corpo, na escala do corpo.

Pixel não serve de régua. A mesma pessoa a 2,5 m e a 5 m da câmera tem o dobro e a
metade dos pixels, e um limiar em pixel dispararia num caso e não no outro para o
mesmo gesto. O tronco — do meio dos ombros ao meio dos quadris — encolhe junto com
o resto do corpo, e é a parte que a câmera oblíqua do plano costuma ver inteira.

Um ombro e um quadril bastam. De perfil, o lado de trás some, e exigir os quatro
deixaria a regra cega justamente no ângulo que o plano pede.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from lince_agent.pose.onnx import JUNTAS_COCO17, PosePessoa

_J = JUNTAS_COCO17.index
OMBROS = (_J("ombro_esquerdo"), _J("ombro_direito"))
QUADRIS = (_J("quadril_esquerdo"), _J("quadril_direito"))

ESCALA_MINIMA_PX = 1.0
"""Ombro e quadril a menos de um pixel um do outro é erro do modelo. Dividir por
quase zero faria todo pulso ficar infinitamente longe — e todo frame, um alcance."""


@dataclass(frozen=True, slots=True)
class Referencia:
    centro_x: float
    centro_y: float
    escala: float
    """Comprimento do tronco em pixels: do meio dos ombros ao meio dos quadris."""


def _media_dos_vistos(pose: PosePessoa, indices: tuple[int, ...], limiar: float):
    vistos = [
        pose.juntas[i]
        for i in indices
        if i < len(pose.juntas)
        and math.isfinite(pose.juntas[i].confianca)
        and math.isfinite(pose.juntas[i].x)
        and math.isfinite(pose.juntas[i].y)
        and pose.juntas[i].confianca >= limiar
    ]
    if not vistos:
        return None
    return (
        sum(j.x for j in vistos) / len(vistos),
        sum(j.y for j in vistos) / len(vistos),
    )


def referencia_do_tronco(pose: PosePessoa, *, limiar: float) -> Referencia | None:
    """O centro e o comprimento do tronco, ou `None` quando não há como medir."""
    ombros = _media_dos_vistos(pose, OMBROS, limiar)
    quadris = _media_dos_vistos(pose, QUADRIS, limiar)
    if ombros is None or quadris is None:
        return None
    escala = math.hypot(quadris[0] - ombros[0], quadris[1] - ombros[1])
    if escala < ESCALA_MINIMA_PX:
        return None
    return Referencia(
        centro_x=(ombros[0] + quadris[0]) / 2,
        centro_y=(ombros[1] + quadris[1]) / 2,
        escala=escala,
    )


def normaliza(x: float, y: float, ref: Referencia) -> tuple[float, float]:
    """O ponto em troncos, com origem no meio do tronco e y crescendo para baixo.

    Os ombros ficam perto de y = −0,5 e os quadris perto de y = +0,5. O eixo x segue
    o da imagem — a regra olha distância e faixa simétrica, então não precisa saber
    para que lado a pessoa está virada.
    """
    return (x - ref.centro_x) / ref.escala, (y - ref.centro_y) / ref.escala
