"""A fila local em Redis (ADR-004, §3.6).

Estruturas, e por que cada uma:

| Chave                        | Tipo   | Papel                                     |
|------------------------------|--------|-------------------------------------------|
| `…:event:{id}`               | HASH   | payload congelado e todo o estado do item |
| `…:events` / `…:clips`       | ZSET   | agenda: `event_id` → quando pode sair     |
| `…:births`                   | ZSET   | `event_id` → nascimento, para o TTL       |
| `…:dead`                     | ZSET   | fila morta, aparada por posto             |

**ZSET e não LIST porque o backoff é um agendamento.** Com lista, adiar um item
exigiria tirá-lo da fila e segurá-lo na memória do processo — e aí uma queda perderia
justamente o evento que estava sendo reenviado. Com score, "pronto para enviar" é uma
consulta (`ZRANGEBYSCORE -inf agora`) e o estado nunca sai do Redis.

**`births` é um índice separado** porque o score da agenda anda com o backoff: depois
de três tentativas ele não diz mais quando o evento nasceu, e o TTL do §3.6 é medido
a partir do nascimento.

**O `claim` é um script Lua** por um motivo específico: sem atomicidade entre "achar o
mais antigo pronto" e "empurrar o score para frente", duas passagens do sender podem
reclamar o mesmo evento e a nuvem recebe o mesmo `POST` duas vezes. A idempotência por
`event_id` salvaria o banco, mas o trabalho duplicado (upload de megabytes) não.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from lince_agent.config import OutboxOptions
from lince_agent.outbox.state import ClipState, ItemKind, ItemState, OutboxStats, QueueItem
from lince_agent.outbox.store import payload_bytes

if TYPE_CHECKING:  # pragma: no cover - só para tipagem
    from redis import Redis

log = logging.getLogger(__name__)

_CLAIM_LUA = """
local pronto = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', ARGV[1], 'LIMIT', 0, 1)
if #pronto == 0 then
  return nil
end
redis.call('ZADD', KEYS[1], ARGV[2], pronto[1])
return pronto[1]
"""
"""Reserva sem remover: o item continua na agenda, só adiado pelo lease. Se o processo
morrer entre o claim e o `ack`, o lease vence e o evento volta sozinho."""


class RedisOutbox:
    """Fila durável. Sobrevive a restart do agente; perde ~1 s numa queda de energia.

    Esse último segundo é consequência do `appendfsync everysec` do Redis e está
    assumido no ADR-004: na prática é o último evento antes do apagão, e um apagão na
    loja já derruba as câmeras junto.
    """

    def __init__(
        self,
        store_id: str,
        options: OutboxOptions | None = None,
        *,
        client: Redis | None = None,
    ) -> None:
        if not store_id:
            raise ValueError("store_id é obrigatório: chave sem loja colide entre agentes (NFR-6)")
        self._options = options or OutboxOptions()
        self._prefix = f"{self._options.key_prefix}:{store_id}:outbox"
        self._client = client if client is not None else self._connect()
        self._claim = self._client.register_script(_CLAIM_LUA)

    def _connect(self) -> Redis:
        # Importado aqui e não no topo para o `redis` não virar dependência de import
        # de quem só usa a fila em memória — a CLI de desenvolvimento e os testes de
        # política rodam sem tocar no cliente.
        from redis import Redis

        return Redis.from_url(
            self._options.redis_url,
            socket_timeout=self._options.socket_timeout_s,
            socket_connect_timeout=self._options.socket_timeout_s,
            decode_responses=True,
        )

    # --- chaves -------------------------------------------------------------

    def _hash(self, event_id: str) -> str:
        return f"{self._prefix}:event:{event_id}"

    def _agenda(self, kind: ItemKind) -> str:
        return f"{self._prefix}:events" if kind is ItemKind.EVENT else f"{self._prefix}:clips"

    @property
    def _births(self) -> str:
        return f"{self._prefix}:births"

    @property
    def _dead(self) -> str:
        return f"{self._prefix}:dead"

    # --- escrita ------------------------------------------------------------

    def enqueue(self, item: QueueItem) -> bool:
        campos = {
            "payload": json.dumps(item.payload, separators=(",", ":")),
            "payload_bytes": payload_bytes(item.payload),
            "created_at_ms": item.created_at_ms,
            "state": item.state.value,
            "clip_state": item.clip_state.value,
            "clip_path": item.clip_path or "",
            "clip_size_bytes": item.clip_size_bytes,
            "upload_url": item.upload_url or "",
            "upload_expires_ms": "" if item.upload_expires_ms is None else item.upload_expires_ms,
            "object_key": item.object_key or "",
            "last_error": item.last_error or "",
            "attempts_event": 0,
            "attempts_clip": 0,
        }
        chave = self._hash(item.event_id)
        with self._client.pipeline() as pipe:
            # `HSETNX` num campo âncora é o que torna o enfileiramento idempotente sem
            # um round-trip de leitura: quem perder a corrida não sobrescreve o
            # payload de quem chegou primeiro.
            pipe.hsetnx(chave, "created_at_ms", item.created_at_ms)
            resultado = pipe.execute()
        if not resultado[0]:
            return False

        with self._client.pipeline() as pipe:
            pipe.hset(chave, mapping=campos)
            pipe.zadd(self._births, {item.event_id: item.created_at_ms})
            pipe.zadd(self._agenda(ItemKind.EVENT), {item.event_id: item.created_at_ms})
            pipe.execute()
        return True

    def claim(self, kind: ItemKind, *, now_ms: int, lease_ms: int) -> QueueItem | None:
        event_id = self._claim(keys=[self._agenda(kind)], args=[now_ms, now_ms + lease_ms])
        if event_id is None:
            return None
        item = self._ler(event_id, kind)
        if item is None:
            # HASH sumiu mas a agenda ficou: só acontece com interferência externa
            # (FLUSHDB, expurgo manual). Limpar aqui evita reclamar um fantasma a cada
            # tick para sempre.
            self._client.zrem(self._agenda(kind), event_id)
            return None
        return item

    def ack(self, event_id: str, kind: ItemKind) -> None:
        chave = self._hash(event_id)
        with self._client.pipeline() as pipe:
            pipe.zrem(self._agenda(kind), event_id)
            if kind is ItemKind.EVENT:
                pipe.hset(chave, "state", ItemState.SENT.value)
            else:
                pipe.hset(chave, "clip_path", "")
            pipe.execute()

        if kind is ItemKind.CLIP and self._campo(event_id, "clip_state") == ClipState.UPLOADED:
            self._client.hset(chave, "clip_state", ClipState.DONE.value)
        self._recolhe(event_id)

    def retry(self, event_id: str, kind: ItemKind, *, not_before_ms: int, error: str) -> None:
        agenda = self._agenda(kind)
        # `is None`, e não valor-verdade: um item agendado para o instante 0 tem score
        # falsy e sumiria da fila em silêncio na primeira falha de rede.
        if self._client.zscore(agenda, event_id) is None:
            return
        campo = "attempts_event" if kind is ItemKind.EVENT else "attempts_clip"
        with self._client.pipeline() as pipe:
            pipe.zadd(agenda, {event_id: not_before_ms})
            pipe.hincrby(self._hash(event_id), campo, 1)
            pipe.hset(self._hash(event_id), "last_error", error[:500])
            pipe.execute()

    def to_dead(self, event_id: str, *, reason: str) -> None:
        nascimento = self._client.zscore(self._births, event_id)
        with self._client.pipeline() as pipe:
            pipe.zrem(self._agenda(ItemKind.EVENT), event_id)
            pipe.zrem(self._agenda(ItemKind.CLIP), event_id)
            pipe.zrem(self._births, event_id)
            pipe.delete(self._hash(event_id))
            pipe.zadd(self._dead, {f"{event_id}|{reason[:200]}": nascimento or 0})
            # A fila morta é diagnóstico, não retenção: guarda os mais recentes e
            # descarta o resto por posto.
            pipe.zremrangebyrank(self._dead, 0, -(self._options.dead_letter_max + 1))
            pipe.execute()

    def promote_clip(
        self, event_id: str, *, upload_url: str, expires_ms: int | None, not_before_ms: int
    ) -> None:
        if self._campo(event_id, "clip_state") in (None, ClipState.NONE):
            return
        with self._client.pipeline() as pipe:
            pipe.hset(
                self._hash(event_id),
                mapping={
                    "upload_url": upload_url,
                    "upload_expires_ms": "" if expires_ms is None else expires_ms,
                },
            )
            pipe.zadd(self._agenda(ItemKind.CLIP), {event_id: not_before_ms})
            pipe.execute()

    def mark_uploaded(self, event_id: str, *, object_key: str | None) -> None:
        self._client.hset(
            self._hash(event_id),
            mapping={
                "clip_state": ClipState.UPLOADED.value,
                "object_key": object_key or "",
                "attempts_clip": 0,
            },
        )

    def requeue_event(self, event_id: str, *, not_before_ms: int) -> None:
        if not self._client.exists(self._hash(event_id)):
            return
        with self._client.pipeline() as pipe:
            pipe.hset(
                self._hash(event_id),
                mapping={"state": ItemState.PENDING.value, "attempts_event": 0},
            )
            pipe.zadd(self._agenda(ItemKind.EVENT), {event_id: not_before_ms})
            pipe.execute()

    def give_up_clip(self, event_id: str, *, reason: str) -> None:
        estado = self._campo(event_id, "clip_state")
        if estado in (None, ClipState.NONE, ClipState.DONE):
            return
        with self._client.pipeline() as pipe:
            pipe.hset(
                self._hash(event_id),
                mapping={
                    "clip_state": ClipState.GIVEN_UP.value,
                    "clip_path": "",
                    "attempts_clip": 0,
                    "last_error": reason[:500],
                },
            )
            pipe.execute()
        # Ver `MemoryOutbox.give_up_clip`: o PATCH só é agendado se a nuvem já conhece
        # o evento, senão ele chegaria antes do POST e levaria 404.
        if self._campo(event_id, "state") == ItemState.SENT:
            nascimento = self._client.zscore(self._births, event_id) or 0
            self._client.zadd(self._agenda(ItemKind.CLIP), {event_id: int(nascimento)})

    def drop(self, event_id: str, *, reason: str) -> None:
        with self._client.pipeline() as pipe:
            pipe.zrem(self._agenda(ItemKind.EVENT), event_id)
            pipe.zrem(self._agenda(ItemKind.CLIP), event_id)
            pipe.zrem(self._births, event_id)
            pipe.delete(self._hash(event_id))
            pipe.execute()

    # --- leitura ------------------------------------------------------------

    def get(self, event_id: str) -> QueueItem | None:
        estado = self._campo(event_id, "state")
        kind = ItemKind.EVENT if estado == ItemState.PENDING else ItemKind.CLIP
        return self._ler(event_id, kind)

    def born_before(self, *, cutoff_ms: int) -> tuple[QueueItem, ...]:
        ids = self._client.zrangebyscore(self._births, "-inf", f"({cutoff_ms}")
        itens = [self.get(event_id) for event_id in ids]
        return tuple(item for item in itens if item is not None)

    def stats(self, *, now_ms: int) -> OutboxStats:
        with self._client.pipeline() as pipe:
            pipe.zcard(self._agenda(ItemKind.EVENT))
            pipe.zcard(self._agenda(ItemKind.CLIP))
            pipe.zcount(self._agenda(ItemKind.EVENT), "-inf", now_ms)
            pipe.zcount(self._agenda(ItemKind.CLIP), "-inf", now_ms)
            pipe.zrange(self._births, 0, 0, withscores=True)
            pipe.zcard(self._dead)
            eventos, clipes, ev_prontos, cl_prontos, mais_antigo, mortos = pipe.execute()

        idade = 0.0
        if mais_antigo:
            idade = max(0.0, (now_ms - mais_antigo[0][1]) / 1000)

        return OutboxStats(
            depth=eventos + clipes,
            events=eventos,
            clips=clipes,
            ready=ev_prontos + cl_prontos,
            oldest_age_s=idade,
            payload_bytes=self._soma_payload_bytes(),
            dead=mortos,
        )

    def close(self) -> None:
        self._client.close()

    # --- interno ------------------------------------------------------------

    def _campo(self, event_id: str, nome: str) -> str | None:
        return self._client.hget(self._hash(event_id), nome)

    def _soma_payload_bytes(self) -> int:
        """`queue_bytes` do §5.3.

        Percorre os pendentes em vez de manter um contador incremental: contador
        incremental é o tipo de estado que sai de sincronia num restart no meio de uma
        operação e passa a mentir para sempre no heartbeat.
        """
        total = 0
        for event_id in self._client.zrange(self._births, 0, -1):
            valor = self._campo(event_id, "payload_bytes")
            total += int(valor) if valor else 0
        return total

    def _ler(self, event_id: str, kind: ItemKind) -> QueueItem | None:
        campos: dict[str, Any] = self._client.hgetall(self._hash(event_id))
        if not campos:
            return None
        attempts = campos.get("attempts_event" if kind is ItemKind.EVENT else "attempts_clip", "0")
        expira = campos.get("upload_expires_ms") or ""
        return QueueItem(
            event_id=event_id,
            kind=kind,
            payload=json.loads(campos["payload"]),
            created_at_ms=int(campos["created_at_ms"]),
            attempts=int(attempts or 0),
            state=ItemState(campos.get("state", ItemState.PENDING.value)),
            clip_state=ClipState(campos.get("clip_state", ClipState.NONE.value)),
            clip_path=campos.get("clip_path") or None,
            clip_size_bytes=int(campos.get("clip_size_bytes") or 0),
            upload_url=campos.get("upload_url") or None,
            upload_expires_ms=int(expira) if expira else None,
            object_key=campos.get("object_key") or None,
            last_error=campos.get("last_error") or None,
        )

    def _recolhe(self, event_id: str) -> None:
        """Apaga o registro quando não há mais trabalho. O histórico é da nuvem (§4.2);
        a borda só guarda o que ainda não subiu."""
        with self._client.pipeline() as pipe:
            pipe.hmget(self._hash(event_id), "state", "clip_state")
            pipe.zscore(self._agenda(ItemKind.CLIP), event_id)
            (estado, clip_state), agendado = pipe.execute()

        if estado != ItemState.SENT:
            return
        if agendado is not None or clip_state == ClipState.PENDING:
            return
        with self._client.pipeline() as pipe:
            pipe.delete(self._hash(event_id))
            pipe.zrem(self._births, event_id)
            pipe.zrem(self._agenda(ItemKind.EVENT), event_id)
            pipe.execute()
