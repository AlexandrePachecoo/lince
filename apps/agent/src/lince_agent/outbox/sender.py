"""O laço de envio: tira da fila, fala com a nuvem, aplica a política (§3.6, §5.4).

**`tick()` é público e síncrono de propósito.** Cada passagem faz no máximo um envio e
devolve se houve trabalho. Isso é o que permite testar rede caída, URL expirada, fila
morta e teto de tentativas chamando um método, sem thread e sem `sleep` — e a thread
de produção vira três linhas em volta dele, com pouco a errar.

A ordem de trabalho é a do §3.6, e é estrita: **enquanto houver evento pronto, nenhum
clipe sobe.** O metadado é o que destrava o alerta no celular; o vídeo é o que ocupa a
banda da loja. Numa loja com o link ruim, essa prioridade é a diferença entre o
gerente receber o aviso em segundos ou depois do upload de doze megabytes.

Uma thread para o agente inteiro, como no estágio 5. A consequência aceita é bloqueio
de cabeça de fila: um `PUT` lento atrasa o próximo `POST`. O timeout por requisição
limita o estrago, e a prioridade acima garante que os eventos já drenaram antes de
qualquer clipe começar.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path

from lince_agent.clip.state import ClipStatus
from lince_agent.clip.store import ClipStore
from lince_agent.config import OutboxOptions
from lince_agent.outbox.clock import MonotonicAnchor
from lince_agent.outbox.event import build_clip_patch
from lince_agent.outbox.http import CloudClient, CloudResponse, NetworkError
from lince_agent.outbox.policy import (
    Outcome,
    classify_response,
    clip_gave_up,
    is_expired,
    parse_retry_after,
    retry_delay,
)
from lince_agent.outbox.state import ClipState, ItemKind, QueueItem, SenderStats
from lince_agent.outbox.store import OutboxStore

log = logging.getLogger(__name__)


class OutboxSender:
    """Drena a fila local para a nuvem, sem nunca bloquear o pipeline."""

    def __init__(
        self,
        store: OutboxStore,
        client: CloudClient,
        *,
        options: OutboxOptions | None = None,
        clip_store: ClipStore | None = None,
        anchor: MonotonicAnchor | None = None,
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._store = store
        self._client = client
        self._options = options or OutboxOptions()
        self._clip_store = clip_store
        self._anchor = anchor
        self._wall_clock = wall_clock
        self._jitter = jitter

        self._lock = threading.Lock()
        self._stats = SenderStats()
        self._acorda = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._ultima_varredura_ms = 0

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("o sender já está rodando")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="outbox-sender", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> None:
        self._stop_event.set()
        self._acorda.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def notify(self) -> None:
        """Acorda o laço. Chamado por quem enfileira, para o alerta não esperar o
        próximo poll ocioso — o NFR-1 dá 15 s e 10 já foram no pós-roll."""
        self._acorda.set()

    def stats(self) -> SenderStats:
        with self._lock:
            return self._stats

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                trabalhou = self.tick()
            except Exception:  # noqa: BLE001 - ver docstring abaixo
                # Uma exceção inesperada aqui não pode matar o envio da loja inteira.
                # O pipeline continua detectando de qualquer forma (§5.4), mas uma
                # thread morta faria a fila crescer em silêncio até o TTL.
                log.exception("falha inesperada no envio; o laço continua")
                self._conta(failures=1)
                trabalhou = False
            if not trabalhou:
                self._acorda.wait(self._options.idle_poll_s)
                self._acorda.clear()

    # --- uma passagem -------------------------------------------------------

    def tick(self) -> bool:
        """Envia no máximo um item. Devolve se houve trabalho."""
        agora_ms = self._agora_ms()
        self._varre_vencidos(agora_ms)

        item = self._store.claim(
            ItemKind.EVENT, now_ms=agora_ms, lease_ms=int(self._options.lease_s * 1000)
        )
        if item is not None:
            self._envia_evento(item, agora_ms)
            return True

        item = self._store.claim(
            ItemKind.CLIP, now_ms=agora_ms, lease_ms=int(self._options.lease_s * 1000)
        )
        if item is not None:
            self._envia_clipe(item, agora_ms)
            return True

        return False

    # --- evento -------------------------------------------------------------

    def _envia_evento(self, item: QueueItem, agora_ms: int) -> None:
        try:
            resposta = self._client.post_event(item.payload, event_id=item.event_id)
        except NetworkError as erro:
            self._reagenda(item, Outcome.RETRY, agora_ms, erro=str(erro))
            return

        desfecho = classify_response(resposta.status)
        if desfecho is Outcome.ACCEPTED:
            self._store.ack(item.event_id, ItemKind.EVENT)
            self._conta(sent=1, sucesso=True)
            self._encaminha_clipe(item, resposta, agora_ms)
            return

        if desfecho is Outcome.REJECTED:
            motivo = f"POST recusado com {resposta.status}"
            log.error(
                "evento %s recusado pela nuvem (%s): vai para a fila morta. "
                "Fila morta crescendo é sinal de agente e API em versões incompatíveis.",
                item.event_id,
                resposta.status,
            )
            self._store.to_dead(item.event_id, reason=motivo)
            self._descarta_arquivo(item.event_id)
            self._conta(rejected=1, erro=motivo)
            return

        # `404` no POST não deveria acontecer (a rota é fixa); tratar como temporário
        # é o lado seguro do erro.
        self._reagenda(item, desfecho, agora_ms, resposta=resposta)

    def _encaminha_clipe(self, item: QueueItem, resposta: CloudResponse, agora_ms: int) -> None:
        """Decide o que fazer com o vídeo depois de a nuvem aceitar o evento."""
        if item.clip_state is ClipState.GIVEN_UP:
            # O clipe já tinha sido perdido antes de o evento subir (teto de disco com
            # o link caído). Agora que a nuvem conhece o evento, o aviso pode ir.
            self._store.give_up_clip(item.event_id, reason=item.last_error or "clipe indisponível")
            return
        if item.clip_state is not ClipState.PENDING:
            return

        url = resposta.body.get("clip_upload_url") if resposta.body else None
        if not isinstance(url, str) or not url:
            log.warning(
                "nuvem aceitou o evento %s sem oferecer URL de upload; clipe descartado",
                item.event_id,
            )
            self._descarta_arquivo(item.event_id)
            self._store.give_up_clip(item.event_id, reason="nuvem não ofereceu URL de upload")
            return

        expira_em = (resposta.body or {}).get("clip_upload_expires_in_s")
        expira_ms = (
            agora_ms + int(float(expira_em) * 1000) if isinstance(expira_em, int | float) else None
        )
        self._store.promote_clip(
            item.event_id, upload_url=url, expires_ms=expira_ms, not_before_ms=agora_ms
        )

    # --- clipe --------------------------------------------------------------

    def _envia_clipe(self, item: QueueItem, agora_ms: int) -> None:
        if item.clip_state is ClipState.PENDING:
            self._sobe_bytes(item, agora_ms)
            return
        self._confirma_clipe(item, agora_ms)

    def _sobe_bytes(self, item: QueueItem, agora_ms: int) -> None:
        if item.upload_expires_ms is not None and agora_ms >= item.upload_expires_ms:
            # Gastar uma tentativa num `PUT` que já vai falhar só atrasa o clipe. A URL
            # nova vem na resposta do POST, e o POST é idempotente (§5.4).
            log.info("URL de upload de %s expirou; repondo o evento para renovar", item.event_id)
            self._store.requeue_event(item.event_id, not_before_ms=agora_ms)
            self._store.retry(
                item.event_id, ItemKind.CLIP, not_before_ms=agora_ms, error="URL expirada"
            )
            return

        caminho = Path(item.clip_path) if item.clip_path else None
        if caminho is None or not caminho.exists():
            self._desiste_do_clipe(item, "clipe não está mais em disco")
            return

        try:
            resposta = self._client.put_clip(item.upload_url or "", caminho)
        except FileNotFoundError:
            # Corrida com o teto de disco: o arquivo sumiu entre o `exists` e o `open`.
            self._desiste_do_clipe(item, "clipe removido durante o upload")
            return
        except NetworkError as erro:
            self._reagenda_clipe(item, Outcome.RETRY, agora_ms, erro=str(erro))
            return

        desfecho = classify_response(resposta.status)
        if desfecho is Outcome.ACCEPTED:
            self._store.mark_uploaded(item.event_id, object_key=item.object_key)
            self._conta(clips_uploaded=1, sucesso=True)
            # A confirmação vai na mesma passagem: o item já está reservado e um
            # segundo round-trip de fila só adiaria o clipe na tela do triador.
            confirmado = self._store.get(item.event_id)
            if confirmado is not None:
                self._confirma_clipe(confirmado, agora_ms)
            return

        if desfecho is Outcome.REJECTED:
            self._desiste_do_clipe(item, f"R2 recusou o upload com {resposta.status}")
            return

        self._reagenda_clipe(item, desfecho, agora_ms, resposta=resposta)

    def _confirma_clipe(self, item: QueueItem, agora_ms: int) -> None:
        upload_ok = item.clip_state is ClipState.UPLOADED
        patch = build_clip_patch(
            item.payload.get("clip", {}),
            status=ClipStatus.OK if upload_ok else ClipStatus.CLIP_FAILED,
            object_key=item.object_key if upload_ok else None,
            error=None if upload_ok else item.last_error,
        )
        try:
            resposta = self._client.patch_event(item.event_id, patch)
        except NetworkError as erro:
            self._reagenda_clipe(item, Outcome.RETRY, agora_ms, erro=str(erro))
            return

        desfecho = classify_response(resposta.status)
        if desfecho is Outcome.ACCEPTED:
            self._store.ack(item.event_id, ItemKind.CLIP)
            # Só agora o arquivo sai do disco: enquanto a nuvem não confirmou o
            # desfecho, ele ainda pode precisar ser reenviado (NFR-3 manda apagar, não
            # manda apagar cedo).
            self._descarta_arquivo(item.event_id)
            self._conta(sucesso=True)
            return

        if desfecho is Outcome.UNKNOWN_EVENT:
            # Repõe o evento, mas **não** reenvia os bytes: a chave do objeto no R2 é
            # derivada de `tenant_id` e `event_id` (§4.4), então ela é a mesma depois
            # do novo `POST` e o que já subiu continua no lugar certo. Reenviar
            # megabytes para reescrever o mesmo objeto seria desperdício de link de
            # loja — que é o recurso escasso aqui.
            log.warning(
                "nuvem não conhece o evento %s no PATCH; repondo o evento inteiro",
                item.event_id,
            )
            self._store.requeue_event(item.event_id, not_before_ms=agora_ms)
            self._store.retry(
                item.event_id, ItemKind.CLIP, not_before_ms=agora_ms, error="404 no PATCH"
            )
            return

        if desfecho is Outcome.REJECTED:
            # Insistir num PATCH inválido é a fila de clipes que nunca drena. O evento
            # já está na nuvem; o que se perde é a atualização do estado do vídeo.
            log.error(
                "PATCH do clipe de %s recusado com %s; desistindo da confirmação",
                item.event_id,
                resposta.status,
            )
            self._store.ack(item.event_id, ItemKind.CLIP)
            self._descarta_arquivo(item.event_id)
            self._conta(rejected=1, erro=f"PATCH recusado com {resposta.status}")
            return

        self._reagenda_clipe(item, desfecho, agora_ms, resposta=resposta)

    def _desiste_do_clipe(self, item: QueueItem, motivo: str) -> None:
        """Abre mão do vídeo e mantém o evento (§3.6).

        O `PATCH clip_failed` continua agendado: sem ele a nuvem esperaria para sempre
        um upload que não vem, e a triagem mostraria "processando" eternamente.
        """
        log.warning("clipe do evento %s perdido: %s", item.event_id, motivo)
        self._descarta_arquivo(item.event_id)
        self._store.give_up_clip(item.event_id, reason=motivo)
        self._conta(clips_given_up=1, erro=motivo)

    # --- vencimento ---------------------------------------------------------

    def _varre_vencidos(self, agora_ms: int) -> None:
        """TTL do §3.6, na ordem do R-13: clipes vencem antes dos eventos.

        Não usa `EXPIRE` do Redis: uma chave que some sozinha deixaria membro órfão nos
        índices e, pior, tornaria o descarte invisível — sem log, sem contador, sem
        `queue_oldest_age`. Um evento descartado em silêncio é indistinguível de um
        evento que nunca existiu.
        """
        intervalo_ms = int(self._options.idle_poll_s * 1000)
        if agora_ms - self._ultima_varredura_ms < max(intervalo_ms, 1000):
            return
        self._ultima_varredura_ms = agora_ms

        if self._anchor is not None:
            self._anchor.check_drift()

        for item in self._store.born_before(
            cutoff_ms=agora_ms - int(self._options.clip_ttl_s * 1000)
        ):
            if item.clip_state is ClipState.PENDING and is_expired(
                item.created_at_ms, now_ms=agora_ms, ttl_s=self._options.clip_ttl_s
            ):
                self._desiste_do_clipe(item, f"clipe venceu o TTL de {self._options.clip_ttl_s}s")
                self._conta(clips_expired=1)

        for item in self._store.born_before(
            cutoff_ms=agora_ms - int(self._options.event_ttl_s * 1000)
        ):
            idade_h = (agora_ms - item.created_at_ms) / 3_600_000
            log.warning(
                "evento %s descartado com %.1f h na fila (TTL do §3.6). "
                "Fila vencendo significa link da loja fora do ar por muito tempo.",
                item.event_id,
                idade_h,
            )
            self._descarta_arquivo(item.event_id)
            self._store.drop(item.event_id, reason="TTL do evento")
            self._conta(expired=1)

    # --- auxiliares ---------------------------------------------------------

    def _agora_ms(self) -> int:
        """Relógio de **parede** em ms, e não monotônico: o TTL precisa sobreviver a um
        restart do agente, e o monotônico zera junto com o processo."""
        return int(self._wall_clock() * 1000)

    def _reagenda(
        self,
        item: QueueItem,
        desfecho: Outcome,
        agora_ms: int,
        *,
        resposta: CloudResponse | None = None,
        erro: str | None = None,
    ) -> None:
        atraso = self._atraso(item, desfecho, agora_ms, resposta)
        motivo = erro or f"HTTP {resposta.status if resposta else '?'}"
        # Sem `stats()` aqui: ele percorre a fila inteira, e numa noite de link caído
        # este é o caminho mais quente do sender. A profundidade já aparece na linha
        # de status da CLI e no heartbeat.
        log.info(
            "evento %s não subiu (%s); tentativa %d, nova em %.0fs",
            item.event_id,
            motivo,
            item.attempts + 1,
            atraso,
        )
        self._store.retry(
            item.event_id,
            ItemKind.EVENT,
            not_before_ms=agora_ms + int(atraso * 1000),
            error=motivo,
        )
        self._conta(failures=1, erro=motivo)

    def _reagenda_clipe(
        self,
        item: QueueItem,
        desfecho: Outcome,
        agora_ms: int,
        *,
        resposta: CloudResponse | None = None,
        erro: str | None = None,
    ) -> None:
        motivo = erro or f"HTTP {resposta.status if resposta else '?'}"
        # O teto de tentativas vale só para o upload. A confirmação insiste até o TTL:
        # ela é barata, e sem ela o evento fica "processando" na triagem para sempre.
        if item.clip_state is ClipState.PENDING and clip_gave_up(
            item.attempts + 1, self._options.retry
        ):
            self._desiste_do_clipe(item, f"upload esgotou as tentativas ({motivo})")
            return

        atraso = self._atraso(item, desfecho, agora_ms, resposta)
        self._store.retry(
            item.event_id,
            ItemKind.CLIP,
            not_before_ms=agora_ms + int(atraso * 1000),
            error=motivo,
        )
        self._conta(failures=1, erro=motivo)

    def _atraso(
        self,
        item: QueueItem,
        desfecho: Outcome,
        agora_ms: int,
        resposta: CloudResponse | None,
    ) -> float:
        cabecalho = resposta.retry_after if resposta else None
        return retry_delay(
            item.attempts + 1,
            self._options.retry,
            outcome=desfecho,
            retry_after=parse_retry_after(cabecalho, now_s=agora_ms / 1000),
            jitter=self._jitter,
        )

    def _descarta_arquivo(self, event_id: str) -> None:
        """Apaga o clipe do disco. É o que cumpre o NFR-3 na prática: o único arquivo
        de vídeo do sistema é temporário, e some quando deixa de ser necessário."""
        if self._clip_store is None:
            return
        try:
            self._clip_store.discard(event_id)
        except (OSError, ValueError):
            log.warning("não consegui apagar o clipe de %s", event_id, exc_info=True)

    def _conta(
        self,
        *,
        sent: int = 0,
        clips_uploaded: int = 0,
        rejected: int = 0,
        expired: int = 0,
        clips_expired: int = 0,
        clips_given_up: int = 0,
        failures: int = 0,
        sucesso: bool = False,
        erro: str | None = None,
    ) -> None:
        with self._lock:
            atual = self._stats
            self._stats = SenderStats(
                sent=atual.sent + sent,
                clips_uploaded=atual.clips_uploaded + clips_uploaded,
                rejected=atual.rejected + rejected,
                expired=atual.expired + expired,
                clips_expired=atual.clips_expired + clips_expired,
                clips_given_up=atual.clips_given_up + clips_given_up,
                failures=atual.failures + failures,
                last_success_at=self._wall_clock() if sucesso else atual.last_success_at,
                last_error=erro or atual.last_error,
            )
