"""A fila local: o que ela promete e a implementação em RAM.

O `OutboxStore` é deliberadamente burro — guarda e devolve. Toda decisão mora em
`policy.py`, e é por isso que a política roda em máquina limpa enquanto só esta
camada exige Redis (regra 5 do CLAUDE.md).

A operação que carrega o peso é o `claim`: ele **reserva** o item movendo o horário
agendado para frente, em vez de tirá-lo da fila. Uma queda entre o claim e o `ack`
devolve o item sozinho quando o lease vence. Ler e remover em dois passos deixaria
uma janela em que o evento não está na fila nem enviado — e o evento sumiria sem
nenhum log dizendo que sumiu.

`MemoryOutbox` não é dublê de teste: é a fila usada por `--outbox memory` na CLI de
desenvolvimento, onde durabilidade não importa e subir Redis atrapalha. As duas
implementações respondem à mesma suíte de contrato (`tests/outbox_contrato.py`), que
é o que impede uma de ganhar correção que a outra não recebe.
"""

from __future__ import annotations

import json
import threading
from typing import Protocol

from lince_agent.config import OutboxOptions
from lince_agent.outbox.state import ClipState, ItemKind, ItemState, OutboxStats, QueueItem


class OutboxStore(Protocol):
    """Contrato da fila local. Chamado de duas threads: recorder e sender."""

    def enqueue(self, item: QueueItem) -> bool:
        """Guarda o evento. Devolve `False` se o `event_id` já existia.

        Idempotente porque o mesmo gatilho pode ser reprocessado depois de um
        restart, e criar duas entradas produziria dois alertas para uma ocorrência.
        """
        ...

    def claim(self, kind: ItemKind, *, now_ms: int, lease_ms: int) -> QueueItem | None:
        """Reserva o item pronto mais antigo daquela etapa, ou `None`."""
        ...

    def ack(self, event_id: str, kind: ItemKind) -> None:
        """Etapa concluída com sucesso."""
        ...

    def retry(self, event_id: str, kind: ItemKind, *, not_before_ms: int, error: str) -> None:
        """Devolve o item à fila, agendado, e conta a tentativa."""
        ...

    def to_dead(self, event_id: str, *, reason: str) -> None: ...

    def promote_clip(
        self, event_id: str, *, upload_url: str, expires_ms: int | None, not_before_ms: int
    ) -> None:
        """Coloca o clipe na fila de upload, depois de a nuvem aceitar o evento.

        É aqui que a ordem do §3.6 deixa de depender de disciplina do sender e passa a
        ser estrutural: sem a URL pré-assinada que vem na resposta do `POST`, não
        existe item de clipe para reclamar.
        """
        ...

    def mark_uploaded(self, event_id: str, *, object_key: str | None) -> None:
        """Bytes no R2; falta o `PATCH`. Zera as tentativas: a confirmação tem
        orçamento próprio e não pode herdar as falhas do upload."""
        ...

    def requeue_event(self, event_id: str, *, not_before_ms: int) -> None:
        """Devolve um evento já enviado à fila de eventos.

        Dois caminhos levam aqui, e os dois dependem de o `POST` ser idempotente: a
        URL pré-assinada venceu antes do upload (§5.4 manda pedir uma nova, e a nova
        vem na resposta do `POST`), ou o `PATCH` levou `404` porque a nuvem não
        conhece mais o evento.
        """
        ...

    def give_up_clip(self, event_id: str, *, reason: str) -> None:
        """Desiste do vídeo e mantém o evento. O item de clipe continua na fila para
        levar o `PATCH clip_failed` — a nuvem precisa saber que não deve esperar."""
        ...

    def drop(self, event_id: str, *, reason: str) -> None:
        """Remove tudo. Usado pelo TTL do evento."""
        ...

    def get(self, event_id: str) -> QueueItem | None: ...

    def born_before(self, *, cutoff_ms: int) -> tuple[QueueItem, ...]:
        """Itens nascidos antes do corte, para o TTL do §3.6."""
        ...

    def stats(self, *, now_ms: int) -> OutboxStats: ...

    def close(self) -> None: ...


def payload_bytes(payload: dict[str, object]) -> int:
    """Tamanho do payload serializado. Vai para `queue_bytes` do §5.3."""
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


class _Registro:
    """Estado mutável de um evento. Espelha o HASH do Redis, campo a campo."""

    __slots__ = (
        "attempts_clip",
        "attempts_event",
        "clip_path",
        "clip_size_bytes",
        "clip_state",
        "created_at_ms",
        "event_id",
        "last_error",
        "object_key",
        "payload",
        "payload_bytes",
        "state",
        "upload_expires_ms",
        "upload_url",
    )

    def __init__(self, item: QueueItem) -> None:
        self.event_id = item.event_id
        self.payload = item.payload
        self.payload_bytes = payload_bytes(item.payload)
        self.created_at_ms = item.created_at_ms
        self.state = item.state
        self.clip_state = item.clip_state
        self.clip_path = item.clip_path
        self.clip_size_bytes = item.clip_size_bytes
        self.upload_url = item.upload_url
        self.upload_expires_ms = item.upload_expires_ms
        self.object_key = item.object_key
        self.last_error = item.last_error
        self.attempts_event = 0
        self.attempts_clip = 0

    def view(self, kind: ItemKind) -> QueueItem:
        return QueueItem(
            event_id=self.event_id,
            kind=kind,
            payload=self.payload,
            created_at_ms=self.created_at_ms,
            attempts=self.attempts_event if kind is ItemKind.EVENT else self.attempts_clip,
            state=self.state,
            clip_state=self.clip_state,
            clip_path=self.clip_path,
            clip_size_bytes=self.clip_size_bytes,
            upload_url=self.upload_url,
            upload_expires_ms=self.upload_expires_ms,
            object_key=self.object_key,
            last_error=self.last_error,
        )


class MemoryOutbox:
    """Fila em RAM: some no restart, e é para isso que ela serve.

    Usada pelo `--outbox memory` da CLI, onde o objetivo é ver o pipeline inteiro
    funcionando sem depender de infraestrutura. Em produção seria uma armadilha —
    daí o aviso alto que a CLI emite ao escolhê-la.
    """

    def __init__(self, options: OutboxOptions | None = None) -> None:
        self._options = options or OutboxOptions()
        self._lock = threading.Lock()
        self._registros: dict[str, _Registro] = {}
        self._agenda: dict[ItemKind, dict[str, int]] = {
            ItemKind.EVENT: {},
            ItemKind.CLIP: {},
        }
        self._mortos: list[str] = []

    # --- escrita ------------------------------------------------------------

    def enqueue(self, item: QueueItem) -> bool:
        with self._lock:
            if item.event_id in self._registros:
                return False
            self._registros[item.event_id] = _Registro(item)
            self._agenda[ItemKind.EVENT][item.event_id] = item.created_at_ms
            return True

    def claim(self, kind: ItemKind, *, now_ms: int, lease_ms: int) -> QueueItem | None:
        with self._lock:
            agenda = self._agenda[kind]
            prontos = [
                (quando, event_id) for event_id, quando in agenda.items() if quando <= now_ms
            ]
            if not prontos:
                return None
            # Desempate pelo nascimento, não pelo id: dois eventos agendados para o
            # mesmo milissegundo têm que sair na ordem em que aconteceram.
            prontos.sort(key=lambda par: (par[0], self._registros[par[1]].created_at_ms))
            _, event_id = prontos[0]
            agenda[event_id] = now_ms + lease_ms
            return self._registros[event_id].view(kind)

    def ack(self, event_id: str, kind: ItemKind) -> None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None:
                return
            self._agenda[kind].pop(event_id, None)
            if kind is ItemKind.EVENT:
                registro.state = ItemState.SENT
            else:
                # Um `PATCH clip_failed` confirmado não vira `DONE`: o clipe continua
                # não existindo, e o estado precisa dizer isso para o heartbeat.
                if registro.clip_state is ClipState.UPLOADED:
                    registro.clip_state = ClipState.DONE
                registro.clip_path = None
            self._recolhe(event_id)

    def retry(self, event_id: str, kind: ItemKind, *, not_before_ms: int, error: str) -> None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None or event_id not in self._agenda[kind]:
                return
            self._agenda[kind][event_id] = not_before_ms
            registro.last_error = error
            if kind is ItemKind.EVENT:
                registro.attempts_event += 1
            else:
                registro.attempts_clip += 1

    def to_dead(self, event_id: str, *, reason: str) -> None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None:
                return
            registro.state = ItemState.DEAD
            registro.last_error = reason
            for agenda in self._agenda.values():
                agenda.pop(event_id, None)
            self._mortos.append(event_id)
            del self._registros[event_id]
            excedente = len(self._mortos) - self._options.dead_letter_max
            if excedente > 0:
                del self._mortos[:excedente]

    def promote_clip(
        self, event_id: str, *, upload_url: str, expires_ms: int | None, not_before_ms: int
    ) -> None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None or registro.clip_state is ClipState.NONE:
                return
            registro.upload_url = upload_url
            registro.upload_expires_ms = expires_ms
            self._agenda[ItemKind.CLIP][event_id] = not_before_ms

    def mark_uploaded(self, event_id: str, *, object_key: str | None) -> None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None:
                return
            registro.clip_state = ClipState.UPLOADED
            registro.object_key = object_key
            registro.attempts_clip = 0

    def requeue_event(self, event_id: str, *, not_before_ms: int) -> None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None:
                return
            registro.state = ItemState.PENDING
            registro.attempts_event = 0
            self._agenda[ItemKind.EVENT][event_id] = not_before_ms

    def give_up_clip(self, event_id: str, *, reason: str) -> None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None or registro.clip_state in (ClipState.NONE, ClipState.DONE):
                return
            registro.clip_state = ClipState.GIVEN_UP
            registro.clip_path = None
            registro.last_error = reason
            registro.attempts_clip = 0
            # Só agenda o `PATCH clip_failed` se a nuvem já conhece o evento. Desistir
            # do clipe com o evento ainda na fila (teto de disco durante um link caído)
            # não pode produzir um PATCH que chegaria antes do POST e levaria 404.
            if registro.state is ItemState.SENT:
                self._agenda[ItemKind.CLIP][event_id] = registro.created_at_ms

    def drop(self, event_id: str, *, reason: str) -> None:
        with self._lock:
            if self._registros.pop(event_id, None) is None:
                return
            for agenda in self._agenda.values():
                agenda.pop(event_id, None)

    # --- leitura ------------------------------------------------------------

    def get(self, event_id: str) -> QueueItem | None:
        with self._lock:
            registro = self._registros.get(event_id)
            if registro is None:
                return None
            kind = ItemKind.EVENT if registro.state is ItemState.PENDING else ItemKind.CLIP
            return registro.view(kind)

    def born_before(self, *, cutoff_ms: int) -> tuple[QueueItem, ...]:
        with self._lock:
            registros = [
                registro
                for registro in self._registros.values()
                if registro.created_at_ms < cutoff_ms
            ]
            registros.sort(key=lambda registro: registro.created_at_ms)
            return tuple(
                registro.view(
                    ItemKind.EVENT if registro.state is ItemState.PENDING else ItemKind.CLIP
                )
                for registro in registros
            )

    def stats(self, *, now_ms: int) -> OutboxStats:
        with self._lock:
            eventos = self._agenda[ItemKind.EVENT]
            clipes = self._agenda[ItemKind.CLIP]
            pendentes = set(eventos) | set(clipes)
            prontos = sum(
                1
                for agenda in self._agenda.values()
                for quando in agenda.values()
                if quando <= now_ms
            )
            nascimentos = [self._registros[event_id].created_at_ms for event_id in pendentes]
            return OutboxStats(
                depth=len(eventos) + len(clipes),
                events=len(eventos),
                clips=len(clipes),
                ready=prontos,
                oldest_age_s=(now_ms - min(nascimentos)) / 1000 if nascimentos else 0.0,
                payload_bytes=sum(
                    self._registros[event_id].payload_bytes for event_id in pendentes
                ),
                dead=len(self._mortos),
            )

    def close(self) -> None:
        """Nada a fechar; existe para o sender não precisar saber qual fila é qual."""

    # --- interno ------------------------------------------------------------

    def _recolhe(self, event_id: str) -> None:
        """Remove o registro quando as duas etapas terminaram.

        Guardar eventos já enviados encheria a RAM do box sem servir a ninguém: o
        histórico é da nuvem (§4.2), a borda só precisa do que ainda não subiu.
        """
        registro = self._registros.get(event_id)
        if registro is None:
            return
        falta_clipe = (
            event_id in self._agenda[ItemKind.CLIP] or registro.clip_state is ClipState.PENDING
        )
        if registro.state is ItemState.SENT and not falta_clipe:
            del self._registros[event_id]
