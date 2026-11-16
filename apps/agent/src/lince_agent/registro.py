"""O registro do agente (§5.1): trocar o token de bootstrap pela credencial.

Acontece uma vez na vida do box, na primeira subida. O token de bootstrap sai do
dashboard (`/box`), vale 24 h e é de **uso único** — e é essa última propriedade que
dá forma a este módulo, porque ela faz de cada tentativa uma aposta que não se repete:

- **Recusa da nuvem não se tenta de novo.** `401` (não reconhecido ou expirado) e `409`
  (já usado) não mudam com o tempo. Insistir só adia a mensagem que resolve: gere outro
  token.
- **Falha de rede se tenta de novo, poucas vezes.** O box acabou de ser ligado e o link
  pode estar subindo. Mas sem credencial e sem cache não há o que operar enquanto
  espera, então o agente desiste com erro e o `restart: unless-stopped` do compose
  cuida do resto — em vez de um processo de pé, mudo, que parece instalado.
- **Resposta que não dá para ler é o pior caso, e é dito como tal.** Um `201` sem os
  campos significa que a nuvem consumiu o token e a credencial se perdeu no caminho.

O que este módulo **não** faz: registrar de novo quando a credencial em uso é recusada.
Um `401` em regime é assunto humano (§5.4) — o token de bootstrap já foi gasto, e um
agente que tentasse se reprovisionar sozinho esconderia a causa.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum
from typing import Protocol

from lince_agent.backoff import backoff_delay
from lince_agent.credencial import Credencial
from lince_agent.outbox.http import CloudResponse, NetworkError

log = logging.getLogger(__name__)

TENTATIVAS = 5
"""Com o backoff abaixo, cerca de meio minuto antes de desistir. Mais que isso é
trabalho do Docker, que reinicia o container e deixa o motivo no log."""

_BACKOFF_BASE_S = 2.0
_BACKOFF_TETO_S = 15.0
_BACKOFF_JITTER_S = 1.0


class RegistroClient(Protocol):
    def post_register(self, bootstrap_token: str) -> CloudResponse: ...


class RegistroOutcome(StrEnum):
    REGISTRADO = "registrado"
    TENTAR_DEPOIS = "tentar_depois"
    RECUSADO = "recusado"


def classify_register_response(status: int, *, tem_corpo: bool) -> RegistroOutcome:
    """Traduz o código do `POST /v1/agents/register` em decisão.

    Não é `classify_response` nem `classify_config_response`, pelo mesmo motivo que
    separou aquelas duas: os números significam outra coisa aqui. `409` no envio de
    evento é sucesso ("já tenho"); no registro é "este token já virou a credencial de
    alguém", e tratá-lo como sucesso deixaria o agente subir sem credencial nenhuma.

    `2xx` sem corpo é proxy ou API meio implantada, como no poll de configuração: a
    rota de verdade nunca responde assim, então o token não foi gasto e vale insistir.
    """
    if 200 <= status < 300:
        return RegistroOutcome.REGISTRADO if tem_corpo else RegistroOutcome.TENTAR_DEPOIS
    if 400 <= status < 500 and status not in (408, 429):
        return RegistroOutcome.RECUSADO
    return RegistroOutcome.TENTAR_DEPOIS


class RegistroError(Exception):
    """O registro não aconteceu. A mensagem é para o instalador ler no log."""


class RegistroRecusado(RegistroError):
    """A nuvem respondeu que este token não serve. O conserto é gerar outro."""

    def __init__(self, status: int, motivo: str) -> None:
        super().__init__(
            f"a nuvem recusou o token de bootstrap ({status}: {motivo}). Ele é de uso "
            "único e vale 24 h: gere outro no dashboard, em /box, e troque o "
            "LINCE_BOOTSTRAP_TOKEN do .env"
        )
        self.status = status


class RegistroIndisponivel(RegistroError):
    """A nuvem não respondeu a tempo. O token **não** foi gasto, até onde se sabe."""


class RespostaDeRegistroInvalida(RegistroError):
    """A nuvem aceitou e a credencial não veio legível: o token foi gasto à toa."""


def registra(
    cliente: RegistroClient,
    bootstrap_token: str,
    *,
    tentativas: int = TENTATIVAS,
    dorme: Callable[[float], None] = time.sleep,
) -> Credencial:
    """Troca o token de bootstrap pela credencial, ou levanta `RegistroError`.

    `dorme` é injetável para a suíte não esperar o backoff de verdade (regra 6).
    """
    ultima_falha = "sem tentativa"
    for tentativa in range(1, tentativas + 1):
        try:
            resposta = cliente.post_register(bootstrap_token)
        except NetworkError as erro:
            ultima_falha = str(erro)
        else:
            desfecho = classify_register_response(
                resposta.status, tem_corpo=resposta.body is not None
            )
            if desfecho is RegistroOutcome.REGISTRADO:
                return _credencial_da_resposta(resposta)
            if desfecho is RegistroOutcome.RECUSADO:
                raise RegistroRecusado(resposta.status, _motivo(resposta))
            ultima_falha = f"a nuvem respondeu {resposta.status}"

        if tentativa < tentativas:
            espera = backoff_delay(
                tentativa,
                base_s=_BACKOFF_BASE_S,
                cap_s=_BACKOFF_TETO_S,
                jitter_s=_BACKOFF_JITTER_S,
            )
            log.warning(
                "registro: tentativa %d de %d falhou (%s); nova tentativa em %.0f s",
                tentativa,
                tentativas,
                ultima_falha,
                espera,
            )
            dorme(espera)

    # Um timeout não diz se o pedido chegou. Se chegou, a próxima subida recebe `409`
    # com este mesmo token — e a mensagem de lá já manda gerar outro.
    raise RegistroIndisponivel(
        f"não consegui registrar o agente em {tentativas} tentativas ({ultima_falha}). "
        "Confira LINCE_API_URL e o link da loja"
    )


def _credencial_da_resposta(resposta: CloudResponse) -> Credencial:
    corpo = resposta.body or {}
    campos = [corpo.get(nome) for nome in ("agent_id", "tenant_id", "store_id", "token")]
    if not all(isinstance(valor, str) and valor for valor in campos):
        # Sem o corpo na mensagem: se o `token` veio e outro campo não, ele estaria ali.
        raise RespostaDeRegistroInvalida(
            f"a nuvem respondeu {resposta.status} ao registro sem a credencial completa "
            "(agent-register-response.v1.json). O token de bootstrap provavelmente foi "
            "consumido: gere outro no dashboard, em /box"
        )
    return Credencial(*campos)  # type: ignore[arg-type]


def _motivo(resposta: CloudResponse) -> str:
    """O `erro` que a API manda no corpo (`server.ts`), ou um texto neutro.

    É ele que separa "expirado" de "já foi usado" de "não reconhecido" — três consertos
    parecidos, mas só o último manda conferir se o token foi copiado inteiro.
    """
    erro = (resposta.body or {}).get("erro")
    return erro if isinstance(erro, str) and erro else "sem detalhe"
