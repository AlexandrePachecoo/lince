"""Backoff exponencial com jitter, compartilhado por quem tenta de novo.

Duas áreas do agente reconectam contra o mundo lá fora e precisam exatamente da
mesma política: a supervisão de câmera do §3.1 e o envio para a nuvem do §5.4.
Manter uma cópia em cada uma garantiria que só uma delas ganhasse a correção do
próximo bug de teto ou de jitter.
"""

from __future__ import annotations

import random
from collections.abc import Callable


def backoff_delay(
    consecutive_failures: int,
    *,
    base_s: float,
    cap_s: float,
    jitter_s: float,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Espera até a próxima tentativa, em segundos.

    O jitter não é enfeite: quando o switch da loja cai, as oito câmeras falham no
    mesmo instante. Sem jitter elas voltam no mesmo instante, batem no mesmo
    instante e repetem o padrão a cada tentativa. Vale igual para a fila de envio
    quando o link volta e todos os eventos represados disparam juntos.

    O jitter é **aditivo**, não multiplicativo: ele só espalha as tentativas, sem
    encurtar a espera que o teto acabou de impor.
    """
    if consecutive_failures <= 0:
        return 0.0
    exponential = base_s * (2 ** (consecutive_failures - 1))
    return min(exponential, cap_s) + jitter() * jitter_s
