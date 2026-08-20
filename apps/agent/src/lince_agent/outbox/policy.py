"""Decisões de reenvio (§5.4), sem relógio, sem rede e sem Redis.

Este módulo é puro por desenho, não por elegância. O que ele decide — o que é falha
temporária, o que é recusa definitiva, quanto esperar, o que já venceu — é
exatamente a parte que produz o pior tipo de bug do estágio 6: uma fila que nunca
drena porque reenvia para sempre um payload inválido, ou uma fila que esvazia sozinha
porque tratou uma indisponibilidade da nuvem como recusa e jogou fora os eventos do
dia. Nenhum dos dois aparece rodando na mão.

Sendo puro, roda com `uv run pytest` numa máquina limpa — o Redis fica só na camada
de armazenamento, que é fina justamente para caber num marcador (regra 5 do CLAUDE.md).
"""

from __future__ import annotations

import random
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from enum import StrEnum

from lince_agent.backoff import backoff_delay
from lince_agent.config import RetryOptions


class Outcome(StrEnum):
    """O que fazer com um item da fila depois de uma tentativa de envio."""

    ACCEPTED = "accepted"
    """A nuvem tem o evento. Vale para `2xx` e também para `409`: com idempotência por
    `event_id`, "já existe" é sucesso de reenvio, não conflito (§4.6)."""

    RETRY = "retry"
    """Falha temporária: rede, timeout, `5xx`, `429`. O item volta para a fila com
    backoff. Nunca é motivo para descartar nada."""

    RETRY_SLOW = "retry_slow"
    """Credencial recusada (`401`/`403`). É retry, e não morte: uma rotação de
    credencial mal propagada apagaria os eventos de um dia inteiro se isto virasse
    fila morta. Mas é retry lento — só um humano ou a nuvem resolvem."""

    REJECTED = "rejected"
    """`4xx` de validação (`400`, `413`, `422`). Reenviar o mesmo payload inválido
    para sempre é uma fila que nunca drena; vai para a fila morta com registro."""

    UNKNOWN_EVENT = "unknown_event"
    """`404`: a nuvem não conhece este `event_id`. Acontece no `PATCH` quando o evento
    foi expurgado ou nunca chegou. O caminho é repostar o evento, não desistir do
    clipe."""


_RECUSAS_DE_VALIDACAO = frozenset({400, 413, 422})


def classify_response(status: int) -> Outcome:
    """Traduz o código HTTP em decisão de fila.

    A lista de recusas é fechada de propósito: o §5.4 fala em "`4xx` de validação", e
    tratar todo `4xx` como definitivo transformaria um `429` numa fila morta e um
    `401` num dia de eventos perdidos.
    """
    if 200 <= status < 300 or status == 409:
        return Outcome.ACCEPTED
    if status in (401, 403):
        return Outcome.RETRY_SLOW
    if status == 404:
        return Outcome.UNKNOWN_EVENT
    if status in _RECUSAS_DE_VALIDACAO:
        return Outcome.REJECTED
    # Todo o resto — 5xx, 429, 4xx desconhecido — é temporário. Errar para o lado de
    # insistir custa fila; errar para o lado de descartar custa evento.
    return Outcome.RETRY


class ConfigOutcome(StrEnum):
    """O que fazer com a resposta do `GET /v1/agents/config` (§5.2)."""

    NOVA = "nova"
    """`2xx` com corpo: documento novo para validar e, se passar, aplicar."""

    SEM_MUDANCA = "sem_mudanca"
    """`304`: o `ETag` que mandamos ainda vale. É o caso comum — 2 880 polls por dia,
    e todos menos um punhado caem aqui."""

    TENTAR_DEPOIS = "tentar_depois"
    """Falha temporária. O agente segue com a configuração em pé (§5.4)."""

    RECUSADA = "recusada"
    """`4xx`: a nuvem entende o pedido e não vai atendê-lo. Credencial errada
    (`401`/`403`) ou agente desconhecido (`404`). Continua sendo retry — lento, e é
    por isso que não vira exceção — mas merece log próprio, porque a ação é humana."""


def classify_config_response(status: int, *, tem_corpo: bool) -> ConfigOutcome:
    """Traduz o código do poll de configuração em decisão.

    Deliberadamente **não** é `classify_response`: aquela é do envio de evento, onde
    `304` não existe e cairia em `RETRY`, e onde `404` significa "evento desconhecido".
    Reusá-la aqui daria um agente que trata "nada mudou" como falha de rede e entra em
    backoff exponencial contra uma nuvem que respondeu certo — a loja pararia de receber
    calibração nova sem nenhum erro em lugar nenhum.

    `tem_corpo` existe porque um `200` sem corpo não é configuração: é um proxy, um
    redirecionamento capturado ou uma API meio implantada. Tratá-lo como documento
    faria o loader recusar e contar erro de contrato quando o problema é de transporte.
    """
    if status == 304:
        return ConfigOutcome.SEM_MUDANCA
    if 200 <= status < 300:
        return ConfigOutcome.NOVA if tem_corpo else ConfigOutcome.TENTAR_DEPOIS
    if 400 <= status < 500 and status not in (408, 429):
        return ConfigOutcome.RECUSADA
    return ConfigOutcome.TENTAR_DEPOIS


def parse_retry_after(header: str | None, *, now_s: float) -> float | None:
    """Lê o cabeçalho `Retry-After` nos dois formatos que o HTTP permite.

    Segundos é o comum; data absoluta é o que alguns proxies e CDNs emitem. Ignorar o
    segundo formato faria o agente tratar um "volte às 14h30" como ausência de
    orientação e martelar uma API que pediu para esperar.
    """
    if header is None:
        return None
    texto = header.strip()
    if not texto:
        return None
    try:
        return max(0.0, float(texto))
    except ValueError:
        pass
    try:
        alvo = parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        return None
    return max(0.0, alvo.timestamp() - now_s)


def retry_delay(
    attempts: int,
    options: RetryOptions,
    *,
    outcome: Outcome = Outcome.RETRY,
    retry_after: float | None = None,
    jitter: Callable[[], float] = random.random,
) -> float:
    """Quanto esperar antes da próxima tentativa, em segundos.

    `Retry-After` é **piso, nunca atalho**: o servidor sabe quando volta, e ignorá-lo
    martela uma nuvem que já está em apuros; mas obedecer a um `Retry-After: 1` no
    lugar de um backoff de 5 min desfaria a proteção que o backoff dá.
    """
    espera = backoff_delay(
        attempts,
        base_s=options.base_s,
        cap_s=options.cap_s,
        jitter_s=options.jitter_s,
        jitter=jitter,
    )
    if outcome is Outcome.RETRY_SLOW:
        espera = max(espera, options.auth_floor_s)
    if retry_after is not None:
        espera = max(espera, retry_after)
    return espera


def is_expired(created_at_ms: int, *, now_ms: int, ttl_s: float) -> bool:
    """Se o item já passou do prazo de vida (§3.6).

    Alertar sobre um furto de ontem tem valor operacional baixo e ocupa a fila de
    triagem, que é o recurso escasso do lado humano (NFR-9).
    """
    if ttl_s <= 0:
        return False
    return (now_ms - created_at_ms) >= ttl_s * 1000


def clip_gave_up(attempts: int, options: RetryOptions) -> bool:
    """Se o upload do clipe esgotou as tentativas.

    Só o clipe desiste. O evento fica na fila até subir ou vencer por TTL: o metadado
    é o que destrava o alerta, e o §3.6 é explícito em descartar clipe antes de evento.
    """
    return attempts >= options.max_clip_attempts
