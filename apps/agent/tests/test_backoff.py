"""A política de retry compartilhada entre a reconexão de câmera e o envio de eventos.

Dois números importam aqui, e os dois só doem em produção. O teto é o que impede
uma loja com link ruim de esperar horas entre tentativas depois de uma noite
offline. O jitter é o que impede as oito câmeras que caíram juntas de voltarem
juntas — e, no estágio 6, impede que a fila represada dispare tudo no mesmo
instante quando o link volta.
"""

from __future__ import annotations

from lince_agent.backoff import backoff_delay

SEM_JITTER = {"base_s": 1.0, "cap_s": 60.0, "jitter_s": 0.0, "jitter": lambda: 0.0}


def test_dobra_ate_o_teto():
    delays = [backoff_delay(n, **SEM_JITTER) for n in range(1, 9)]
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]


def test_sem_falha_nao_espera():
    """A primeira tentativa é imediata: um evento recém-enfileirado não pode esperar
    um ciclo de backoff para sair."""
    assert backoff_delay(0, **SEM_JITTER) == 0.0
    assert backoff_delay(-1, **SEM_JITTER) == 0.0


def test_jitter_e_aditivo_e_limitado():
    opcoes = {"base_s": 1.0, "cap_s": 60.0, "jitter_s": 2.0}
    assert backoff_delay(1, **opcoes, jitter=lambda: 0.0) == 1.0
    assert backoff_delay(1, **opcoes, jitter=lambda: 0.5) == 2.0
    assert backoff_delay(1, **opcoes, jitter=lambda: 1.0) == 3.0


def test_jitter_nao_encurta_a_espera_do_teto():
    """Aditivo, não multiplicativo. Um jitter que multiplicasse poderia devolver
    menos que o teto e furar justamente a proteção que o teto existe para dar."""
    for _ in range(20):
        assert backoff_delay(10, base_s=1.0, cap_s=60.0, jitter_s=5.0) >= 60.0
