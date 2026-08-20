"""As decisões que separam "fila que drena" de "fila que perde evento" (§5.4).

Os dois desastres que esta suíte guarda são simétricos e igualmente silenciosos:

- classificar demais como definitivo → a loja perde eventos numa instabilidade da
  nuvem, e ninguém percebe porque não há alerta sobre alerta que não chegou;
- classificar demais como temporário → um payload inválido fica reenviando para
  sempre, a fila nunca drena e os eventos válidos atrás dele vencem por TTL.
"""

from __future__ import annotations

import pytest

from lince_agent.config import RetryOptions
from lince_agent.outbox.policy import (
    ConfigOutcome,
    Outcome,
    classify_config_response,
    classify_response,
    clip_gave_up,
    is_expired,
    parse_retry_after,
    retry_delay,
)

SEM_JITTER = {"jitter": lambda: 0.0}
OPCOES = RetryOptions(base_s=2.0, cap_s=300.0, jitter_s=0.0, auth_floor_s=60.0)


@pytest.mark.parametrize("status", [200, 201, 202, 204])
def test_2xx_e_aceito(status):
    assert classify_response(status) is Outcome.ACCEPTED


def test_409_conta_como_aceito():
    """Com idempotência por `event_id`, "já existe" é a resposta esperada de um
    reenvio depois de um timeout — o POST chegou, a resposta é que se perdeu. Tratar
    como erro faria o agente insistir num evento que a nuvem já tem (§4.6)."""
    assert classify_response(409) is Outcome.ACCEPTED


@pytest.mark.parametrize("status", [400, 413, 422])
def test_erro_de_validacao_vai_para_a_fila_morta(status):
    assert classify_response(status) is Outcome.REJECTED


@pytest.mark.parametrize("status", [401, 403])
def test_credencial_recusada_e_retry_lento_nao_morte(status):
    """Uma rotação de credencial mal propagada apagaria os eventos de um dia inteiro
    se isto virasse fila morta."""
    assert classify_response(status) is Outcome.RETRY_SLOW


def test_404_manda_repostar_o_evento():
    """No PATCH, `404` significa que a nuvem não conhece o evento. Desistir do clipe
    aqui perderia o vídeo de um evento que só precisa ser reenviado."""
    assert classify_response(404) is Outcome.UNKNOWN_EVENT


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 418])
def test_o_resto_e_temporario(status):
    """Inclusive o `4xx` desconhecido: errar para o lado de insistir custa fila, errar
    para o lado de descartar custa evento."""
    assert classify_response(status) is Outcome.RETRY


def test_backoff_cresce_e_respeita_o_teto():
    esperas = [retry_delay(n, OPCOES, **SEM_JITTER) for n in range(1, 10)]
    assert esperas == [2, 4, 8, 16, 32, 64, 128, 256, 300]


def test_primeira_tentativa_sai_na_hora():
    """Evento recém-cortado não pode esperar um ciclo de backoff: o NFR-1 dá 15 s do
    evento ao celular, e 10 s já foram no pós-roll."""
    assert retry_delay(0, OPCOES, **SEM_JITTER) == 0.0


def test_credencial_recusada_tem_piso_de_espera():
    assert retry_delay(1, OPCOES, outcome=Outcome.RETRY_SLOW, **SEM_JITTER) == 60.0
    # Passado o piso, volta a valer o backoff normal.
    assert retry_delay(8, OPCOES, outcome=Outcome.RETRY_SLOW, **SEM_JITTER) == 256.0


def test_retry_after_e_piso_nunca_atalho():
    """Obedecer a um `Retry-After: 1` no lugar de um backoff de 5 min desfaria a
    proteção do backoff; ignorá-lo martelaria uma nuvem que pediu para esperar."""
    assert retry_delay(1, OPCOES, retry_after=30.0, **SEM_JITTER) == 30.0
    assert retry_delay(9, OPCOES, retry_after=1.0, **SEM_JITTER) == 300.0


def test_jitter_espalha_a_fila_represada():
    """Quando o link volta, todos os eventos da noite ficam prontos no mesmo instante.
    Sem jitter eles saem juntos e derrubam de novo o que acabou de voltar."""
    opcoes = RetryOptions(base_s=2.0, cap_s=300.0, jitter_s=5.0)
    esperas = {retry_delay(3, opcoes) for _ in range(50)}
    assert len(esperas) > 1
    assert all(8.0 <= espera <= 13.0 for espera in esperas)


def test_retry_after_em_segundos_e_em_data():
    agora = 1_700_000_000.0
    assert parse_retry_after("120", now_s=agora) == 120.0
    # Formato de data absoluta, que é o que alguns proxies e CDNs emitem.
    assert parse_retry_after("Tue, 14 Nov 2023 22:14:20 GMT", now_s=agora) == 60.0


def test_retry_after_ausente_ou_impossivel_nao_derruba_o_envio():
    """Cabeçalho malformado é do servidor; o agente não pode virar exceção por causa
    disso no meio de uma reconexão."""
    agora = 1_700_000_000.0
    assert parse_retry_after(None, now_s=agora) is None
    assert parse_retry_after("", now_s=agora) is None
    assert parse_retry_after("logo mais", now_s=agora) is None


def test_retry_after_no_passado_nao_vira_espera_negativa():
    assert parse_retry_after("Tue, 14 Nov 2023 22:00:00 GMT", now_s=1_700_000_000.0) == 0.0
    assert parse_retry_after("-5", now_s=1_700_000_000.0) == 0.0


def test_ttl_do_evento_e_de_24h():
    nascimento = 1_000_000
    dia = 24 * 3600 * 1000
    assert not is_expired(nascimento, now_ms=nascimento + dia - 1, ttl_s=24 * 3600)
    assert is_expired(nascimento, now_ms=nascimento + dia, ttl_s=24 * 3600)


def test_clipe_vence_antes_do_evento():
    """A ordem de descarte do §3.6 e do R-13: com a internet fora por muito tempo, o
    vídeo é sacrificado para o alerta sobreviver."""
    nascimento = 0
    oito_horas = 8 * 3600 * 1000
    assert is_expired(nascimento, now_ms=oito_horas, ttl_s=6 * 3600)
    assert not is_expired(nascimento, now_ms=oito_horas, ttl_s=24 * 3600)


def test_ttl_zero_desliga_o_vencimento():
    """Loja com link cronicamente ruim pode preferir fila infinita a evento perdido."""
    assert not is_expired(0, now_ms=10**12, ttl_s=0)


def test_so_o_clipe_desiste():
    opcoes = RetryOptions(max_clip_attempts=5)
    assert not clip_gave_up(4, opcoes)
    assert clip_gave_up(5, opcoes)


def test_opcoes_invalidas_falham_alto():
    with pytest.raises(ValueError, match="cap_s"):
        RetryOptions(base_s=10.0, cap_s=1.0)
    with pytest.raises(ValueError, match="max_clip_attempts"):
        RetryOptions(max_clip_attempts=0)


# --------------------------------------------------------------- §5.2 poll de configuração


@pytest.mark.parametrize(
    ("status", "esperado"),
    [
        (200, ConfigOutcome.NOVA),
        (304, ConfigOutcome.SEM_MUDANCA),
        (401, ConfigOutcome.RECUSADA),
        (403, ConfigOutcome.RECUSADA),
        (404, ConfigOutcome.RECUSADA),
        (408, ConfigOutcome.TENTAR_DEPOIS),
        (429, ConfigOutcome.TENTAR_DEPOIS),
        (500, ConfigOutcome.TENTAR_DEPOIS),
        (503, ConfigOutcome.TENTAR_DEPOIS),
    ],
)
def test_classificacao_do_poll_de_configuracao(status, esperado):
    assert classify_config_response(status, tem_corpo=True) is esperado


def test_o_poll_de_config_nao_pode_reusar_a_classificacao_do_envio():
    """As duas tabelas divergem em dois pontos, e os dois doem.

    `304` não existe no envio de evento e cairia em `RETRY`: o agente entraria em
    backoff exponencial contra uma nuvem que respondeu certo, e a loja pararia de
    receber calibração sem nenhum erro em lugar nenhum. E `404` no envio significa
    "evento desconhecido, reposte o evento", enquanto no poll significa "esta nuvem não
    conhece este agente" — reposta nenhuma resolve isso, e é um problema humano.
    """
    assert classify_response(304) is Outcome.RETRY
    assert classify_config_response(304, tem_corpo=False) is ConfigOutcome.SEM_MUDANCA

    assert classify_response(404) is Outcome.UNKNOWN_EVENT
    assert classify_config_response(404, tem_corpo=False) is ConfigOutcome.RECUSADA


def test_200_sem_corpo_nao_e_configuracao():
    """Proxy, redirecionamento capturado ou API meio implantada. Passá-lo ao loader
    contaria erro de contrato num problema que é de rede, e mandaria procurar o bug no
    documento em vez de no caminho até ele."""
    assert classify_config_response(200, tem_corpo=False) is ConfigOutcome.TENTAR_DEPOIS
