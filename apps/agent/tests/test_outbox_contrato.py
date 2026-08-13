"""O que toda fila local precisa prometer, valendo para as duas implementações.

Esta suíte roda duas vezes: contra `MemoryOutbox` sempre, e contra `RedisOutbox`
quando há Redis (marcador `redis`). É o antídoto contra a deriva entre elas — sem
isso, a correção de um bug de ordenação entraria só na que o autor estava usando
naquele dia, e a diferença apareceria em produção, onde só roda a de Redis.

As promessas, todas com a mesma consequência prática: **nenhum evento pode sumir sem
deixar rastro.** Um evento que some não gera alarme nenhum — ninguém sente falta de um
alerta que nunca chegou.
"""

from __future__ import annotations

import uuid

import pytest
from eventos import IDENTIDADE, INSTANTE, rascunho, resultado

from lince_agent.config import OutboxOptions
from lince_agent.outbox.event import build_event_payload, new_event_id
from lince_agent.outbox.state import ClipState, ItemKind, ItemState, QueueItem
from lince_agent.outbox.store import MemoryOutbox

AGORA = 1_700_000_000_000
LEASE_MS = 30_000


@pytest.fixture(
    params=[
        pytest.param("memoria", id="memoria"),
        pytest.param("redis", id="redis", marks=pytest.mark.redis),
    ]
)
def outbox(request):
    """A mesma fila, nas duas implementações.

    O prefixo é único por teste e o teardown apaga só ele. `FLUSHDB` estouraria os
    dados de desenvolvimento de quem estiver com a stack no ar.
    """
    opcoes = OutboxOptions(key_prefix=f"lince-test:{uuid.uuid4().hex}", dead_letter_max=3)
    if request.param == "memoria":
        fila = MemoryOutbox(opcoes)
        yield fila
        fila.close()
        return

    import redis as redis_lib

    from lince_agent.outbox.redis_store import RedisOutbox

    url = request.getfixturevalue("redis_url")
    cliente = redis_lib.Redis.from_url(url, decode_responses=True, socket_timeout=5.0)
    fila = RedisOutbox("loja-01", opcoes, client=cliente)
    yield fila
    for chave in cliente.scan_iter(match=f"{opcoes.key_prefix}:*", count=500):
        cliente.delete(chave)
    fila.close()


def item(*, event_id: str | None = None, criado_em: int = AGORA, com_clipe: bool = True):
    event_id = event_id or new_event_id()
    payload = build_event_payload(
        rascunho(event_id), resultado(event_id), identity=IDENTIDADE, reported_at=INSTANTE
    )
    return QueueItem(
        event_id=event_id,
        kind=ItemKind.EVENT,
        payload=payload,
        created_at_ms=criado_em,
        clip_state=ClipState.PENDING if com_clipe else ClipState.NONE,
        clip_path=f"/clipes/{event_id}.mp4" if com_clipe else None,
        clip_size_bytes=1_234_567 if com_clipe else 0,
    )


def entrega_evento(outbox, event_id: str) -> None:
    """Atalho para os testes que começam com o evento já aceito pela nuvem."""
    reclamado = outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
    assert reclamado is not None and reclamado.event_id == event_id
    outbox.ack(event_id, ItemKind.EVENT)


def test_evento_enfileirado_pode_ser_reclamado(outbox):
    guardado = item()
    assert outbox.enqueue(guardado) is True

    reclamado = outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)

    assert reclamado is not None
    assert reclamado.event_id == guardado.event_id
    assert reclamado.payload == guardado.payload
    assert reclamado.attempts == 0


def test_enfileirar_o_mesmo_id_duas_vezes_nao_duplica(outbox):
    """O mesmo gatilho pode ser reprocessado depois de um restart. Duas entradas
    virariam dois alertas para uma ocorrência — e o gerente que recebe alerta repetido
    desliga a notificação (R-1)."""
    guardado = item()
    assert outbox.enqueue(guardado) is True
    assert outbox.enqueue(guardado) is False

    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS) is not None
    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS) is None


def test_claim_devolve_o_mais_antigo_primeiro(outbox):
    """FIFO por nascimento. Fora de ordem, a fila de triagem mostra a tarde antes da
    manhã depois de um represamento — e quem tria perde a sequência dos fatos."""
    primeiro = item(criado_em=AGORA - 5000)
    segundo = item(criado_em=AGORA - 3000)
    terceiro = item(criado_em=AGORA - 1000)
    for guardado in (terceiro, primeiro, segundo):
        outbox.enqueue(guardado)

    ordem = []
    for _ in range(3):
        reclamado = outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
        ordem.append(reclamado.event_id)

    assert ordem == [primeiro.event_id, segundo.event_id, terceiro.event_id]


def test_item_reclamado_nao_sai_duas_vezes(outbox):
    """Sem isto, duas passagens do sender subiriam o mesmo clipe de megabytes duas
    vezes — a idempotência salva o banco da nuvem, não a banda da loja."""
    outbox.enqueue(item())

    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS) is not None
    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS) is None


def test_lease_vencido_devolve_o_item(outbox):
    """A razão de o claim reservar em vez de remover: se o agente cair no meio de uma
    tentativa, o evento volta sozinho. Removido, ele não estaria nem na fila nem na
    nuvem, e nada no sistema saberia que ele existiu."""
    guardado = item()
    outbox.enqueue(guardado)
    outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)

    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA + LEASE_MS - 1, lease_ms=LEASE_MS) is None

    devolvido = outbox.claim(ItemKind.EVENT, now_ms=AGORA + LEASE_MS, lease_ms=LEASE_MS)
    assert devolvido is not None
    assert devolvido.event_id == guardado.event_id


def test_retry_agenda_para_frente_e_conta_a_tentativa(outbox):
    guardado = item()
    outbox.enqueue(guardado)
    outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
    outbox.retry(guardado.event_id, ItemKind.EVENT, not_before_ms=AGORA + 10_000, error="503")

    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA + 9_999, lease_ms=LEASE_MS) is None

    reclamado = outbox.claim(ItemKind.EVENT, now_ms=AGORA + 10_000, lease_ms=LEASE_MS)
    assert reclamado.attempts == 1
    assert reclamado.last_error == "503"


def test_item_com_score_zero_ainda_e_reagendado(outbox):
    """Score falsy é a armadilha clássica de quem testa valor-verdade em vez de
    `is None`. Um item nesse estado sairia da fila na primeira falha de rede, sem
    log, sem contador — o evento simplesmente não subiria nunca."""
    guardado = item(criado_em=0)
    outbox.enqueue(guardado)
    # Lease zero deixa o score em 0 — é o estado que o valor-verdade lê como "não
    # está na fila". `give_up_clip` chega ao mesmo lugar por outro caminho, usando o
    # nascimento como score.
    outbox.claim(ItemKind.EVENT, now_ms=0, lease_ms=0)

    outbox.retry(guardado.event_id, ItemKind.EVENT, not_before_ms=0, error="503")

    devolvido = outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
    assert devolvido is not None
    assert devolvido.attempts == 1


def test_evento_aceito_some_da_fila(outbox):
    guardado = item(com_clipe=False)
    outbox.enqueue(guardado)
    entrega_evento(outbox, guardado.event_id)

    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA + 10**6, lease_ms=LEASE_MS) is None
    assert outbox.get(guardado.event_id) is None, "evento resolvido não ocupa a fila da borda"


def test_clipe_so_entra_na_fila_depois_do_evento_aceito(outbox):
    """A ordem do §3.6 é estrutural, não disciplina do sender: sem a URL pré-assinada
    que vem na resposta do POST, não existe item de clipe para reclamar."""
    guardado = item()
    outbox.enqueue(guardado)

    assert outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS) is None

    entrega_evento(outbox, guardado.event_id)
    outbox.promote_clip(
        guardado.event_id, upload_url="https://r2.exemplo/x", expires_ms=None, not_before_ms=AGORA
    )

    clipe = outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS)
    assert clipe is not None
    assert clipe.kind is ItemKind.CLIP
    assert clipe.upload_url == "https://r2.exemplo/x"
    assert clipe.clip_path == f"/clipes/{guardado.event_id}.mp4"


def test_evento_sem_clipe_nao_cria_item_de_upload(outbox):
    """Corte que falhou não tem o que subir. Um item de clipe fantasma ficaria
    tentando `PUT` de um arquivo que não existe até esgotar as tentativas."""
    guardado = item(com_clipe=False)
    outbox.enqueue(guardado)
    entrega_evento(outbox, guardado.event_id)
    outbox.promote_clip(
        guardado.event_id, upload_url="https://r2.exemplo/x", expires_ms=None, not_before_ms=AGORA
    )

    assert outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS) is None


def test_upload_confirmado_zera_o_orcamento_do_patch(outbox):
    """O `PATCH` tem orçamento próprio de tentativas. Herdando as falhas do upload, um
    clipe que subiu na quinta tentativa desistiria da confirmação na primeira."""
    guardado = item()
    outbox.enqueue(guardado)
    entrega_evento(outbox, guardado.event_id)
    outbox.promote_clip(
        guardado.event_id, upload_url="https://r2.exemplo/x", expires_ms=None, not_before_ms=AGORA
    )
    outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS)
    outbox.retry(guardado.event_id, ItemKind.CLIP, not_before_ms=AGORA, error="timeout")

    outbox.mark_uploaded(guardado.event_id, object_key="rede-abc/x.mp4")

    depois = outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS)
    assert depois.attempts == 0
    assert depois.clip_state is ClipState.UPLOADED
    assert depois.object_key == "rede-abc/x.mp4"


def test_desistir_do_clipe_mantem_o_evento_e_agenda_o_aviso(outbox):
    """O alerta sobrevive sem o vídeo; o contrário não existe (§3.6)."""
    guardado = item()
    outbox.enqueue(guardado)
    entrega_evento(outbox, guardado.event_id)
    outbox.promote_clip(
        guardado.event_id, upload_url="https://r2.exemplo/x", expires_ms=None, not_before_ms=AGORA
    )
    outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS)

    outbox.give_up_clip(guardado.event_id, reason="upload esgotou as tentativas")

    aviso = outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS)
    assert aviso is not None
    assert aviso.clip_state is ClipState.GIVEN_UP
    assert aviso.clip_path is None, "o arquivo já não conta como pendente"

    outbox.ack(guardado.event_id, ItemKind.CLIP)
    assert outbox.get(guardado.event_id) is None


def test_desistir_do_clipe_antes_do_post_nao_agenda_patch(outbox):
    """Teto de disco mordendo com o link caído: o PATCH chegaria antes do POST e
    levaria 404. O aviso vai junto quando o evento finalmente subir."""
    guardado = item()
    outbox.enqueue(guardado)

    outbox.give_up_clip(guardado.event_id, reason="teto de disco")

    assert outbox.claim(ItemKind.CLIP, now_ms=AGORA, lease_ms=LEASE_MS) is None
    ainda_na_fila = outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
    assert ainda_na_fila is not None, "o evento não pode ir embora junto com o clipe"
    assert ainda_na_fila.clip_state is ClipState.GIVEN_UP


def test_evento_pode_voltar_para_a_fila_para_renovar_a_url(outbox):
    """A URL pré-assinada vence enquanto o link está caído. O §5.4 manda pedir uma
    nova, e a nova vem na resposta do `POST` — que só pode ser repetido porque é
    idempotente. Sem este caminho, o clipe fica preso com uma URL morta até o TTL."""
    guardado = item()
    outbox.enqueue(guardado)
    entrega_evento(outbox, guardado.event_id)
    outbox.promote_clip(
        guardado.event_id, upload_url="https://r2/expirada", expires_ms=AGORA, not_before_ms=AGORA
    )

    outbox.requeue_event(guardado.event_id, not_before_ms=AGORA)

    revivido = outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
    assert revivido is not None
    assert revivido.event_id == guardado.event_id
    assert revivido.attempts == 0, "a renovação não herda as falhas do envio anterior"


def test_fila_morta_guarda_o_recusado_e_nao_o_devolve(outbox):
    """Fila morta crescendo é o sintoma de agente e API em versões incompatíveis.
    Apagar a evidência esconderia justamente isso."""
    guardado = item()
    outbox.enqueue(guardado)
    outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)

    outbox.to_dead(guardado.event_id, reason="422 schema_version desconhecida")

    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA + 10**6, lease_ms=LEASE_MS) is None
    assert outbox.get(guardado.event_id) is None
    assert outbox.stats(now_ms=AGORA).dead == 1


def test_fila_morta_tem_teto(outbox):
    """Diagnóstico, não retenção: sem teto ela vira o depósito permanente de uma
    incompatibilidade que ninguém corrigiu."""
    for indice in range(5):
        guardado = item(criado_em=AGORA + indice)
        outbox.enqueue(guardado)
        outbox.to_dead(guardado.event_id, reason="422")

    assert outbox.stats(now_ms=AGORA).dead == 3


def test_born_before_encontra_os_vencidos(outbox):
    velho = item(criado_em=AGORA - 25 * 3600 * 1000)
    novo = item(criado_em=AGORA - 60 * 1000)
    outbox.enqueue(velho)
    outbox.enqueue(novo)

    vencidos = outbox.born_before(cutoff_ms=AGORA - 24 * 3600 * 1000)

    assert [encontrado.event_id for encontrado in vencidos] == [velho.event_id]


def test_drop_remove_tudo(outbox):
    guardado = item()
    outbox.enqueue(guardado)

    outbox.drop(guardado.event_id, reason="TTL de 24h")

    assert outbox.get(guardado.event_id) is None
    assert outbox.claim(ItemKind.EVENT, now_ms=AGORA + 10**6, lease_ms=LEASE_MS) is None
    assert outbox.born_before(cutoff_ms=AGORA + 10**6) == ()


def test_stats_reporta_profundidade_idade_e_bytes(outbox):
    """São os campos `queue_depth`, `queue_oldest_age` e `queue_bytes` do §5.3. A idade
    é o número que denuncia um link caído antes de o TTL começar a descartar:
    profundidade sozinha não distingue rajada de represamento."""
    antigo = item(criado_em=AGORA - 300_000)
    recente = item(criado_em=AGORA - 1_000)
    outbox.enqueue(antigo)
    outbox.enqueue(recente)

    estado = outbox.stats(now_ms=AGORA)

    assert estado.depth == 2
    assert estado.events == 2
    assert estado.clips == 0
    assert estado.ready == 2
    assert estado.oldest_age_s == pytest.approx(300.0, abs=0.01)
    assert estado.payload_bytes > 0


def test_stats_distingue_pronto_de_esperando(outbox):
    guardado = item()
    outbox.enqueue(guardado)
    outbox.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
    outbox.retry(guardado.event_id, ItemKind.EVENT, not_before_ms=AGORA + 60_000, error="503")

    estado = outbox.stats(now_ms=AGORA)

    assert estado.depth == 1
    assert estado.ready == 0, "item em backoff não é trabalho disponível"


def test_operacoes_sobre_evento_inexistente_nao_estouram(outbox):
    """O sender chama estas operações a partir de estado lido antes; um TTL que
    apagou o item no meio do caminho não pode derrubar a thread de envio."""
    fantasma = new_event_id()

    outbox.ack(fantasma, ItemKind.EVENT)
    outbox.retry(fantasma, ItemKind.EVENT, not_before_ms=AGORA, error="x")
    outbox.to_dead(fantasma, reason="x")
    outbox.promote_clip(fantasma, upload_url="u", expires_ms=None, not_before_ms=AGORA)
    outbox.give_up_clip(fantasma, reason="x")
    outbox.drop(fantasma, reason="x")

    assert outbox.get(fantasma) is None


@pytest.mark.redis
def test_a_fila_sobrevive_a_troca_de_cliente(redis_url):
    """A promessa que só o Redis faz, e a razão de ele ter sido escolhido: o estado
    está fora do processo. Um restart do agente — atualização por watchtower, crash,
    reboot do box — não pode custar os eventos que ainda não subiram.

    O que este teste **não** prova é a durabilidade contra queda de energia: com
    `appendfsync everysec`, até ~1 s de fila se perde, e isso está assumido no ADR-004.
    """
    import redis as redis_lib

    from lince_agent.outbox.redis_store import RedisOutbox

    opcoes = OutboxOptions(key_prefix=f"lince-test:{uuid.uuid4().hex}")
    guardado = item()

    primeiro = redis_lib.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5.0)
    fila = RedisOutbox("loja-01", opcoes, client=primeiro)
    fila.enqueue(guardado)
    fila.close()

    segundo = redis_lib.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5.0)
    outra_fila = RedisOutbox("loja-01", opcoes, client=segundo)
    try:
        reclamado = outra_fila.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS)
        assert reclamado is not None
        assert reclamado.payload == guardado.payload
    finally:
        for chave in segundo.scan_iter(match=f"{opcoes.key_prefix}:*", count=500):
            segundo.delete(chave)
        outra_fila.close()


@pytest.mark.redis
def test_agente_de_outra_loja_nao_enxerga_a_fila(redis_url):
    """NFR-6 no nível da chave: dois agentes apontados para o mesmo Redis — o que
    acontece em desenvolvimento e num piloto com duas lojas — não podem consumir a
    fila um do outro."""
    import redis as redis_lib

    from lince_agent.outbox.redis_store import RedisOutbox

    opcoes = OutboxOptions(key_prefix=f"lince-test:{uuid.uuid4().hex}")
    cliente = redis_lib.Redis.from_url(redis_url, decode_responses=True, socket_timeout=5.0)
    uma = RedisOutbox("loja-01", opcoes, client=cliente)
    outra = RedisOutbox("loja-02", opcoes, client=cliente)
    try:
        uma.enqueue(item())

        assert outra.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS) is None
        assert uma.claim(ItemKind.EVENT, now_ms=AGORA, lease_ms=LEASE_MS) is not None
    finally:
        for chave in cliente.scan_iter(match=f"{opcoes.key_prefix}:*", count=500):
            cliente.delete(chave)
        cliente.close()


def test_estado_lido_de_volta_e_o_mesmo_que_entrou(outbox):
    """Na fila de Redis isto atravessa serialização: um campo que volta como string
    onde o sender espera int quebra só na hora do envio, com a fila já cheia."""
    guardado = item()
    outbox.enqueue(guardado)

    lido = outbox.get(guardado.event_id)

    assert lido.payload == guardado.payload
    assert lido.created_at_ms == guardado.created_at_ms
    assert lido.clip_size_bytes == guardado.clip_size_bytes
    assert lido.clip_state is ClipState.PENDING
    assert lido.state is ItemState.PENDING
    assert lido.attempts == 0
