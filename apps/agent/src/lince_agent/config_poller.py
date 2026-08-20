"""O poll de configuração da §5.2: puxar a cada 30 s, e nunca parar por causa disso.

O ADR-003 escolheu configuração **puxada**: a nuvem nunca abre conexão para a loja, o
que elimina IP fixo, VPN e NAT traversal. O preço, escrito no próprio ADR, é este
módulo — um poll constante e até 30 s de latência para uma recalibração chegar.

A regra que governa tudo aqui é a última linha da §5.4: *nenhuma falha de rede pode
parar a detecção*. Nada neste módulo levanta para fora do laço; toda falha vira
contador e log, e o agente segue com a configuração que já tem. As quatro maneiras de
o poll dar errado, e o que cada uma faz:

| O que aconteceu | O que o agente faz |
|---|---|
| Rede caiu, `5xx`, timeout | Backoff em cima do intervalo; segue com a configuração em pé |
| `4xx` (credencial, agente desconhecido) | Igual, com log próprio: o conserto é humano |
| Documento que o loader recusa | **Não aplica e não cacheia**; conta como inválido |
| Documento válido mas estrutural | Aplica nada, grava no cache, marca reinício pendente |

O quarto caso é o único que precisa de explicação. Ele **vai** para o cache mesmo sem
ser aplicado, de propósito: é a configuração que a loja deve estar rodando, e o
reinício que a torna válida — watchtower, queda de energia, `docker restart` — pode
acontecer com o link caído. Guardá-la é o que faz o reinício pendente resolver-se
sozinho em vez de exigir que a nuvem esteja no ar no momento exato do boot.

O desenho é o do `OutboxSender`: `tick()` público, síncrono, uma requisição por
passagem, com `clock` injetável. É isso que torna a suíte testável sem `sleep` e sem
tempo real (regra 6 do CLAUDE.md) — o laço só existe para chamar `tick` e dormir.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from lince_agent.backoff import backoff_delay
from lince_agent.config import AgentConfig, RetryOptions
from lince_agent.config_cache import ConfigCache
from lince_agent.config_loader import ConfigError
from lince_agent.outbox.http import CloudResponse, NetworkError
from lince_agent.outbox.policy import ConfigOutcome, classify_config_response, parse_retry_after

log = logging.getLogger(__name__)

INTERVALO_S = 30.0
"""O 30 s do ADR-003, em um lugar só. Não é configurável pelo documento de propósito:
uma configuração ruim que aumentasse o próprio intervalo para uma hora seria irreversível
pela nuvem — a loja demoraria uma hora para receber o conserto."""


@dataclass(frozen=True, slots=True)
class ConfigPollerStats:
    """O que o transporte observa. O que está *em pé* é o `ConfigHealth` do runtime."""

    etag: str | None = None
    sem_mudanca: int = 0
    """`304`. O número saudável: em regime, praticamente todo poll cai aqui."""

    recebidos: int = 0
    """Documentos novos que chegaram e que o loader aceitou. **Não** é quantos foram
    aplicados: um documento válido com mudança estrutural é recebido, cacheado e
    recusado pelo runtime. Quem conta aplicação de verdade é o `ConfigHealth` do
    runtime, que é quem aplica — e as duas linhas ficam lado a lado no `--stats`
    justamente para a diferença entre elas ser visível."""

    invalidos: int = 0
    """Documentos que a nuvem serviu e o `config_loader` recusou. Diferente de zero é
    divergência de contrato entre a API e o agente — o que `test_contrato_config.py`
    existe para impedir, e o que este contador flagra quando ela escapa mesmo assim."""

    recusados: int = 0
    falhas: int = 0
    ultimo_sucesso_s: float | None = None
    """Instante monotônico do último `200` ou `304`. É a idade disto — e não a contagem
    de falhas — que diz se uma loja está operando com calibração velha."""


class ConfigPoller:
    """Busca a configuração da loja e entrega a quem sabe aplicá-la."""

    def __init__(
        self,
        client: Any,
        *,
        monta: Callable[[dict[str, Any]], AgentConfig],
        aplica: Callable[[AgentConfig], Any],
        cache: ConfigCache | None = None,
        etag: str | None = None,
        retry: RetryOptions | None = None,
        intervalo_s: float = INTERVALO_S,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._monta = monta
        """Do documento cru ao `AgentConfig`, já com o que é do box amarrado. Recebido
        pronto porque a divisão da §5.2 — o que é da loja e o que é da máquina — é
        decidida na subida, e este módulo não pode ter opinião sobre ela."""

        self._aplica = aplica
        self._cache = cache
        self._retry = retry or RetryOptions()
        self._intervalo_s = intervalo_s
        self._clock = clock
        self._jitter = jitter

        self._etag = etag
        self._falhas_seguidas = 0
        self._proximo_em = 0.0
        self._lock = threading.Lock()
        self._stats = ConfigPollerStats(etag=etag)
        self._acorda = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("o poller já está rodando")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="config-poller", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._acorda.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def stats(self) -> ConfigPollerStats:
        with self._lock:
            return self._stats

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - ver docstring do módulo
                # Uma exceção inesperada aqui mataria o poll da loja em silêncio: o
                # agente seguiria detectando (que é o que a §5.4 quer) e nunca mais
                # receberia calibração — e ninguém repara, porque nada quebra.
                log.exception("falha inesperada no poll de configuração; o laço continua")
                self._registra_falha()
            self._acorda.wait(self._espera())
            self._acorda.clear()

    # --- uma passagem -------------------------------------------------------

    def tick(self) -> ConfigOutcome:
        """Uma requisição ao `GET /v1/agents/config` e o que ela produziu.

        Síncrono e sem dormir: quem decide quando chamar é o laço, ou o teste.
        """
        try:
            resposta = self._client.get_config(etag=self._etag)
        except NetworkError as erro:
            # Sem resposta não dá para distinguir "a nuvem recusou" de "o link caiu", e
            # a única suposição segura é a segunda (§5.4).
            log.warning("poll de configuração sem resposta: %s", erro)
            self._registra_falha()
            return ConfigOutcome.TENTAR_DEPOIS

        desfecho = classify_config_response(resposta.status, tem_corpo=resposta.body is not None)

        if desfecho is ConfigOutcome.SEM_MUDANCA:
            self._sucesso(sem_mudanca=1)
            return desfecho

        if desfecho is ConfigOutcome.RECUSADA:
            log.warning(
                "nuvem recusou o poll de configuração (%d): o agente segue com a "
                "configuração atual, mas o conserto é humano",
                resposta.status,
            )
            self._registra_falha(recusa=True, retry_after=resposta.retry_after)
            return desfecho

        if desfecho is ConfigOutcome.TENTAR_DEPOIS:
            log.warning("poll de configuração falhou (%d)", resposta.status)
            self._registra_falha(retry_after=resposta.retry_after)
            return desfecho

        return self._recebe(resposta)

    def _recebe(self, resposta: CloudResponse) -> ConfigOutcome:
        """Documento novo: valida, aplica, e só então grava no cache."""
        documento = dict(resposta.body or {})
        try:
            config = self._monta(documento)
        except ConfigError as erro:
            # Não é falha de transporte: a nuvem respondeu, e o que veio o agente não
            # entende. Contar como falha de rede esconderia uma divergência de contrato
            # atrás de um "backoff, deve ser a internet".
            log.error("configuração servida pela nuvem é inválida e foi ignorada: %s", erro)
            self._registra_invalido()
            return ConfigOutcome.NOVA

        # O `ETag` avança mesmo quando a aplicação é recusada por ser estrutural: o
        # agente **tem** esta versão, ela está no cache, e o que falta é um reinício.
        # Não avançar faria a nuvem reenviar o documento inteiro a cada 30 s até
        # alguém reiniciar o box — que pode ser semanas.
        self._aplica(config)
        if self._cache is not None:
            self._cache.grava(documento, etag=resposta.etag)
        # Adota o `ETag` da resposta **inclusive quando ele é `None`**. Uma API que
        # serve o documento sem `ETag` não sabe responder condicionalmente, e continuar
        # mandando o `If-None-Match` da versão anterior é pedir um `304` que significaria
        # "você ainda tem a v1" quando o agente já está na v2 — e o poll pararia de ver
        # qualquer mudança seguinte.
        self._etag = resposta.etag
        self._sucesso(recebidos=1)
        return ConfigOutcome.NOVA

    # --- contadores e ritmo -------------------------------------------------

    def _espera(self) -> float:
        """Quanto dormir até a próxima passagem."""
        if self._falhas_seguidas == 0:
            return self._intervalo_s
        # O backoff soma ao intervalo em vez de substituí-lo: com a nuvem fora do ar
        # não há motivo para ir mais rápido que o regime normal, e o teto do
        # `RetryOptions` já limita o quanto uma loja pode ficar sem tentar.
        espera = backoff_delay(
            self._falhas_seguidas,
            base_s=self._retry.base_s,
            cap_s=self._retry.cap_s,
            jitter_s=self._retry.jitter_s,
            jitter=self._jitter,
        )
        return max(self._intervalo_s, min(espera, self._retry.cap_s), self._proximo_em)

    def _sucesso(self, *, sem_mudanca: int = 0, recebidos: int = 0) -> None:
        self._falhas_seguidas = 0
        self._proximo_em = 0.0
        with self._lock:
            self._stats = replace(
                self._stats,
                etag=self._etag,
                sem_mudanca=self._stats.sem_mudanca + sem_mudanca,
                recebidos=self._stats.recebidos + recebidos,
                ultimo_sucesso_s=self._clock(),
            )

    def _registra_falha(self, *, recusa: bool = False, retry_after: str | None = None) -> None:
        self._falhas_seguidas += 1
        espera = parse_retry_after(retry_after, now_s=time.time())
        # `Retry-After` é piso, nunca atalho — a mesma regra do envio de evento (§5.4).
        self._proximo_em = espera or 0.0
        with self._lock:
            self._stats = replace(
                self._stats,
                falhas=self._stats.falhas + 1,
                recusados=self._stats.recusados + (1 if recusa else 0),
            )

    def _registra_invalido(self) -> None:
        """Documento ilegível não é falha de rede: não entra no backoff.

        Se entrasse, um erro de contrato entre a API e o agente aumentaria o intervalo
        até o teto — e a loja demoraria minutos para receber o documento **corrigido**,
        que é exatamente o que se quer que chegue rápido.
        """
        with self._lock:
            self._stats = replace(self._stats, invalidos=self._stats.invalidos + 1)
