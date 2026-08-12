"""Backoff, watchdog e a fila que nunca pode bloquear o ffmpeg."""

from __future__ import annotations

import threading

import pytest

from lince_agent.config import SupervisionOptions
from lince_agent.ffmpeg.queues import DropOldestQueue
from lince_agent.ingest.supervisor import backoff_delay

SEM_JITTER = SupervisionOptions(backoff_base_s=1.0, backoff_cap_s=60.0, backoff_jitter_s=0.0)


def test_backoff_dobra_ate_o_teto():
    delays = [backoff_delay(n, SEM_JITTER, jitter=lambda: 0.0) for n in range(1, 9)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]


def test_sem_falha_nao_espera():
    assert backoff_delay(0, SEM_JITTER, jitter=lambda: 0.0) == 0.0


def test_jitter_e_aditivo_e_limitado():
    """Sem jitter, oito câmeras que caem juntas (queda do switch) voltam juntas e
    repetem a colisão a cada tentativa."""
    options = SupervisionOptions(backoff_base_s=1.0, backoff_jitter_s=2.0)
    assert backoff_delay(1, options, jitter=lambda: 0.0) == 1.0
    assert backoff_delay(1, options, jitter=lambda: 1.0) == 3.0
    assert backoff_delay(1, options, jitter=lambda: 0.5) == 2.0


def test_fila_descarta_o_mais_antigo_em_vez_de_bloquear():
    """A propriedade que sustenta o estágio inteiro: `put` nunca bloqueia. Se
    bloqueasse, a thread leitora pararia de drenar o pipe e o ffmpeg travaria —
    inclusive a outra saída e a leitura do RTSP."""
    fila: DropOldestQueue[int] = DropOldestQueue(3)
    for item in range(10):
        fila.put(item)

    assert len(fila) == 3
    assert fila.dropped == 7
    assert [fila.get(timeout=0) for _ in range(3)] == [7, 8, 9]


def test_put_nao_bloqueia_com_a_fila_cheia():
    fila: DropOldestQueue[int] = DropOldestQueue(1)
    fila.put(1)
    concluido = threading.Event()

    def escreve() -> None:
        for item in range(1000):
            fila.put(item)
        concluido.set()

    thread = threading.Thread(target=escreve, daemon=True)
    thread.start()
    assert concluido.wait(timeout=5.0), "put bloqueou com a fila cheia"


def test_get_devolve_none_no_timeout():
    fila: DropOldestQueue[int] = DropOldestQueue(2)
    assert fila.get(timeout=0.01) is None


def test_close_acorda_quem_espera_mas_preserva_o_que_sobrou():
    fila: DropOldestQueue[int] = DropOldestQueue(4)
    fila.put(1)
    fila.close()
    assert fila.get(timeout=0) == 1
    assert fila.get(timeout=0) is None


def test_maxsize_invalido():
    with pytest.raises(ValueError, match="maxsize"):
        DropOldestQueue(0)
