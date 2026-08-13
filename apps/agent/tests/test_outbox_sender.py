"""O estágio 6 inteiro: o que acontece entre o clipe cortado e o disco vazio.

A suíte é dirigida por `tick()`, nunca pela thread. Um teste de rede caída que
dependesse de thread e de tempo real seria instável em CI e, pior, esconderia o que
está sendo verificado — que é sempre a mesma coisa: **nenhum evento pode sumir sem
deixar rastro, e nenhuma falha de rede pode parar o pipeline** (§5.4).

Os clipes são arquivos de verdade num `ClipStore` de verdade. O que interessa medir
no fim de quase todo teste é o disco: o NFR-3 diz que o único arquivo de vídeo do
sistema é temporário, e "temporário" só é verificável olhando se ele sumiu.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from eventos import IDENTIDADE, INSTANTE, rascunho, resultado
from nuvem_falsa import Resposta as RespostaHttp
from nuvem_falsa import ServidorFalso

from lince_agent.clip.state import ClipStatus
from lince_agent.clip.store import ClipStore
from lince_agent.config import OutboxOptions, RetryOptions
from lince_agent.outbox.event import build_event_payload, new_event_id
from lince_agent.outbox.http import CloudResponse, HttpCloudClient, NetworkError
from lince_agent.outbox.sender import OutboxSender
from lince_agent.outbox.state import ClipState, ItemKind, QueueItem
from lince_agent.outbox.store import MemoryOutbox

AGORA_S = 1_700_000_000.0
URL = "https://r2.exemplo/loja/clipe.mp4?sig=abc"
OPCOES = OutboxOptions(
    key_prefix="teste",
    retry=RetryOptions(base_s=2.0, cap_s=60.0, jitter_s=0.0, max_clip_attempts=3),
)


class RelogioFalso:
    def __init__(self, inicio: float = AGORA_S) -> None:
        self.agora = inicio

    def __call__(self) -> float:
        return self.agora

    def avanca(self, segundos: float) -> None:
        self.agora += segundos


@dataclass
class R:
    """Uma resposta roteirizada, ou uma falha de rede."""

    status: int = 202
    body: dict | None = None
    retry_after: str | None = None
    erro: Exception | None = None


ACEITE = R(status=202, body={"schema_version": 1, "clip_upload_url": URL})


class ClienteRoteirizado:
    """Dublê no limite do Protocol `CloudClient`.

    O que o HTTP de verdade faz já está provado em `test_cloud_client.py`, contra um
    socket. Aqui o que importa é a **sequência de decisões** do sender, e amarrá-la a
    um servidor real só acrescentaria tempo e instabilidade sem cobrir nada novo — com
    uma exceção deliberada: `test_caminho_completo_contra_http_de_verdade`.
    """

    chamadas: list[tuple[str, str]]

    def __init__(self, *, post=None, put=None, patch=None) -> None:
        self.roteiros = {
            "POST": list(post or []),
            "PUT": list(put or []),
            "PATCH": list(patch or []),
        }
        self.padroes = {"POST": ACEITE, "PUT": R(status=200), "PATCH": R(status=200)}
        self.chamadas = []
        self.corpos: list[dict] = []
        self.bytes_enviados: list[int] = []

    def _responde(self, verbo: str, event_id: str) -> CloudResponse:
        self.chamadas.append((verbo, event_id))
        roteiro = self.roteiros[verbo]
        resposta = roteiro.pop(0) if roteiro else self.padroes[verbo]
        if resposta.erro is not None:
            raise resposta.erro
        return CloudResponse(
            status=resposta.status, body=resposta.body, retry_after=resposta.retry_after
        )

    def post_event(self, payload, *, event_id):
        self.corpos.append(payload)
        return self._responde("POST", event_id)

    def put_clip(self, url, path, *, content_type="video/mp4"):
        self.bytes_enviados.append(path.stat().st_size)
        return self._responde("PUT", path.stem)

    def patch_event(self, event_id, payload):
        self.corpos.append(payload)
        return self._responde("PATCH", event_id)

    @property
    def verbos(self) -> list[str]:
        return [verbo for verbo, _ in self.chamadas]


@dataclass
class Montagem:
    sender: OutboxSender
    store: MemoryOutbox
    cliente: ClienteRoteirizado
    clipes: ClipStore
    relogio: RelogioFalso
    opcoes: OutboxOptions = field(default=OPCOES)


def monta(tmp_path, cliente=None, *, opcoes: OutboxOptions = OPCOES) -> Montagem:
    relogio = RelogioFalso()
    store = MemoryOutbox(opcoes)
    clipes = ClipStore(tmp_path / "clipes")
    cliente = cliente or ClienteRoteirizado()
    sender = OutboxSender(
        store,
        cliente,
        options=opcoes,
        clip_store=clipes,
        wall_clock=relogio,
        jitter=lambda: 0.0,
    )
    return Montagem(sender, store, cliente, clipes, relogio, opcoes)


def enfileira(m: Montagem, *, com_clipe: bool = True, nascido_em: float | None = None) -> str:
    """Coloca um evento na fila como o runtime faria, com clipe real em disco."""
    event_id = new_event_id()
    corte = (
        resultado(event_id)
        if com_clipe
        else resultado(event_id, status=ClipStatus.CLIP_FAILED, path=None, error="sem init segment")
    )
    payload = build_event_payload(
        rascunho(event_id), corte, identity=IDENTIDADE, reported_at=INSTANTE
    )
    caminho = None
    if com_clipe:
        caminho = m.clipes.path_for(event_id)
        caminho.write_bytes(b"mp4" * 1000)
        m.clipes.register(caminho)

    m.store.enqueue(
        QueueItem(
            event_id=event_id,
            kind=ItemKind.EVENT,
            payload=payload,
            created_at_ms=int((nascido_em or m.relogio.agora) * 1000),
            clip_state=ClipState.PENDING if com_clipe else ClipState.NONE,
            clip_path=str(caminho) if caminho else None,
            clip_size_bytes=3000 if com_clipe else 0,
        )
    )
    return event_id


def drena(m: Montagem, limite: int = 10) -> int:
    """Roda `tick()` até não haver mais trabalho. Devolve quantas passagens fizeram algo."""
    for feitas in range(limite):
        if not m.sender.tick():
            return feitas
    raise AssertionError(f"o sender não parou em {limite} passagens")


# --- caminho feliz ----------------------------------------------------------


def test_evento_sobe_antes_do_clipe(tmp_path):
    """A ordem do §3.6: o metadado destrava o alerta no celular, o vídeo pode esperar.
    Invertida, o gerente recebe a notificação depois do upload — e o NFR-1 dá 15 s."""
    m = monta(tmp_path)
    event_id = enfileira(m)

    drena(m)

    assert m.cliente.verbos == ["POST", "PUT", "PATCH"]
    assert all(chamado[1] == event_id for chamado in m.cliente.chamadas)


def test_o_clipe_some_do_disco_depois_do_upload_confirmado(tmp_path):
    """NFR-3, o teste-âncora do estágio: o único arquivo de vídeo do sistema é
    temporário. Um clipe que fica no disco depois de confirmado transforma o box numa
    gravação contínua por acúmulo — exatamente o que a arquitetura proíbe."""
    m = monta(tmp_path)
    event_id = enfileira(m)
    caminho = m.clipes.path_for(event_id)
    assert caminho.exists()

    drena(m)

    assert not caminho.exists()
    assert m.clipes.usage_bytes() == 0


def test_evento_aceito_sai_da_fila_uma_vez_so(tmp_path):
    m = monta(tmp_path)
    enfileira(m, com_clipe=False)

    drena(m)

    assert m.cliente.verbos == ["POST"]
    assert m.store.stats(now_ms=int(m.relogio.agora * 1000)).depth == 0
    assert m.sender.stats().sent == 1


def test_evento_sem_clipe_nao_tenta_upload(tmp_path):
    """Corte que falhou sobe como `clip_failed` e acabou. Um `PUT` de arquivo
    inexistente gastaria as tentativas e atrasaria a fila atrás dele."""
    m = monta(tmp_path)
    enfileira(m, com_clipe=False)

    drena(m)

    assert "PUT" not in m.cliente.verbos
    assert m.cliente.corpos[0]["clip"]["status"] == "clip_failed"


def test_patch_confirma_o_upload_com_a_chave_do_objeto(tmp_path):
    m = monta(
        tmp_path,
        ClienteRoteirizado(
            post=[
                R(
                    status=202,
                    body={
                        "schema_version": 1,
                        "clip_upload_url": URL,
                        "clip_object_key": "loja-01/clipe.mp4",
                    },
                )
            ]
        ),
    )
    enfileira(m)

    drena(m)

    patch = m.cliente.corpos[-1]
    assert patch["clip"]["status"] == "ok"
    assert patch["clip"]["pre_roll_s"] == 4.1, "o PATCH preserva o que o corte mediu"


# --- rede ruim --------------------------------------------------------------


def test_com_a_rede_caida_a_fila_fica_intacta_e_nada_trava(tmp_path):
    """A regra geral do §5.4: nenhuma falha de rede pode parar a detecção. A fila
    acumula, o clipe continua em disco, e nenhuma chamada bloqueia."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(erro=NetworkError("link caído"))] * 5))
    event_id = enfileira(m)

    for _ in range(5):
        m.sender.tick()
        m.relogio.avanca(3600)

    assert m.store.get(event_id) is not None
    assert m.clipes.path_for(event_id).exists()
    assert m.sender.stats().sent == 0
    assert m.sender.stats().failures == 5


def test_reenvio_depois_da_falha_usa_o_mesmo_event_id(tmp_path):
    """A idempotência do §5.4 só funciona se o id não mudar entre tentativas. Um id
    novo a cada reenvio criaria uma duplicata por tentativa na nuvem (§4.6)."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(status=503), R(status=503), ACEITE]))
    event_id = enfileira(m, com_clipe=False)

    for _ in range(3):
        m.sender.tick()
        m.relogio.avanca(3600)

    posts = [chamado for chamado in m.cliente.chamadas if chamado[0] == "POST"]
    assert posts == [("POST", event_id)] * 3
    assert m.sender.stats().sent == 1


def test_409_conta_como_entregue(tmp_path):
    """O POST chegou e a resposta se perdeu. Insistir num evento que a nuvem já tem
    seguraria a fila inteira atrás dele."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(status=409, body={"schema_version": 1})]))
    enfileira(m, com_clipe=False)

    drena(m)

    assert m.sender.stats().sent == 1


def test_backoff_afasta_a_proxima_tentativa(tmp_path):
    """Sem espera crescente, uma nuvem em apuros recebe uma tentativa por `tick` de
    cada loja da rede — e não se recupera."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(status=503)] * 3))
    enfileira(m, com_clipe=False)

    assert m.sender.tick() is True
    assert m.sender.tick() is False, "o item reagendado não pode voltar na passagem seguinte"

    m.relogio.avanca(2.0)
    assert m.sender.tick() is True


def test_credencial_recusada_nao_esvazia_a_fila(tmp_path):
    """Rotação de credencial mal propagada não pode custar um dia de eventos: `401` é
    retry lento, nunca fila morta."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(status=401)] * 3))
    event_id = enfileira(m, com_clipe=False)

    for _ in range(3):
        m.sender.tick()
        m.relogio.avanca(3600)

    assert m.store.get(event_id) is not None
    assert m.store.stats(now_ms=int(m.relogio.agora * 1000)).dead == 0


def test_erro_de_validacao_vai_para_a_fila_morta_e_nao_volta(tmp_path):
    """Reenviar um payload que a API recusa é a fila que nunca drena — e os eventos
    válidos atrás dele vencem por TTL enquanto isso."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(status=422, body={"erro": "schema"})]))
    event_id = enfileira(m)

    drena(m)
    m.relogio.avanca(86_400)
    assert drena(m) == 0

    assert m.store.get(event_id) is None
    assert m.store.stats(now_ms=int(m.relogio.agora * 1000)).dead == 1
    assert not m.clipes.path_for(event_id).exists(), "clipe de evento morto não fica no disco"


def test_uma_falha_inesperada_nao_derruba_a_thread(tmp_path):
    """A thread de envio é uma só. Se ela morrer, a fila cresce em silêncio até o TTL e
    o heartbeat que avisaria disso sobe pela mesma thread.

    O evento que provocou a exceção fica reservado até o lease vencer, e isso é o
    desejado: uma exceção no meio do envio não diz se o `POST` chegou ou não, e o lease
    é justamente o que dá tempo de a dúvida se resolver sem duplicar. O que este teste
    exige é que o **laço** continue atendendo o resto da fila.
    """
    m = monta(tmp_path, ClienteRoteirizado(post=[R(erro=RuntimeError("bug nosso"))]))
    enfileira(m, com_clipe=False)

    m.sender.start()
    try:
        assert _espera(lambda: m.sender.stats().failures >= 1)

        enfileira(m, com_clipe=False)
        m.sender.notify()

        assert _espera(lambda: m.sender.stats().sent == 1), "o laço não sobreviveu à exceção"
    finally:
        m.sender.stop(timeout=5.0)


def _espera(condicao, prazo_s: float = 5.0) -> bool:
    """Polling com prazo. `sleep` fixo seria instável em CI (regra 6 do CLAUDE.md)."""
    import time

    limite = time.monotonic() + prazo_s
    while time.monotonic() < limite:
        if condicao():
            return True
        time.sleep(0.01)
    return condicao()


# --- clipe ------------------------------------------------------------------


def test_clipe_nao_e_apagado_se_o_put_falhou(tmp_path):
    """O arquivo é a única cópia: o buffer circular já o descartou da RAM. Apagar antes
    da confirmação transformaria uma falha de rede em vídeo perdido."""
    m = monta(tmp_path, ClienteRoteirizado(put=[R(erro=NetworkError("timeout"))]))
    event_id = enfileira(m)

    m.sender.tick()
    m.sender.tick()

    assert m.clipes.path_for(event_id).exists()
    assert m.store.get(event_id).clip_state is ClipState.PENDING


def test_upload_esgotado_vira_clip_failed_e_o_evento_sobrevive(tmp_path):
    """§3.6: clipes são descartados antes dos eventos. O alerta sem vídeo ainda é
    triável; o vídeo sem alerta não serve para nada."""
    m = monta(tmp_path, ClienteRoteirizado(put=[R(status=500)] * 5))
    event_id = enfileira(m)

    for _ in range(6):
        m.sender.tick()
        m.relogio.avanca(3600)

    patch = m.cliente.corpos[-1]
    assert patch["clip"]["status"] == "clip_failed"
    assert "esgotou" in patch["clip"]["error"]
    assert not m.clipes.path_for(event_id).exists()
    assert m.sender.stats().sent == 1, "o evento subiu do mesmo jeito"
    assert m.sender.stats().clips_given_up == 1


def test_url_expirada_repoe_o_evento_em_vez_de_gastar_tentativa(tmp_path):
    """§5.4: o retry do upload usa uma URL nova, e a URL nova vem na resposta do POST —
    que só pode ser repetido porque é idempotente."""
    curta = {"schema_version": 1, "clip_upload_url": URL, "clip_upload_expires_in_s": 60}
    m = monta(
        tmp_path,
        ClienteRoteirizado(
            post=[R(status=202, body=curta), R(status=202, body={"clip_upload_url": URL})]
        ),
    )
    event_id = enfileira(m)

    m.sender.tick()  # POST, promove o clipe
    m.relogio.avanca(120)  # a URL vence antes de o upload acontecer
    m.sender.tick()  # percebe o vencimento e repõe o evento

    assert m.cliente.verbos == ["POST"], "não gastou tentativa num PUT que já falharia"

    drena(m)
    assert m.cliente.verbos == ["POST", "POST", "PUT", "PATCH"]
    assert not m.clipes.path_for(event_id).exists()


def test_patch_com_404_repoe_o_evento_sem_reenviar_os_bytes(tmp_path):
    """A nuvem não conhece o evento: expurgo, banco restaurado, ou o POST nunca chegou.
    Desistir aqui perderia o vídeo de um evento que só precisa ser reenviado.

    O segundo `PUT` não acontece de propósito: a chave do objeto no R2 vem de
    `tenant_id` e `event_id` (§4.4), é a mesma depois do novo POST, e os bytes já estão
    lá. Reenviá-los gastaria o link da loja para reescrever o mesmo objeto.
    """
    m = monta(tmp_path, ClienteRoteirizado(patch=[R(status=404)]))
    event_id = enfileira(m)

    drena(m)

    assert m.cliente.verbos == ["POST", "PUT", "PATCH", "POST", "PATCH"]
    assert not m.clipes.path_for(event_id).exists()


def test_clipe_sumido_do_disco_vira_clip_failed_sem_travar(tmp_path):
    """Teto de disco, expurgo manual ou reinício do container no meio do caminho. O
    sender não pode ficar tentando subir um arquivo que não existe."""
    m = monta(tmp_path)
    event_id = enfileira(m)
    m.sender.tick()  # POST
    m.clipes.path_for(event_id).unlink()

    drena(m)

    assert "PUT" not in m.cliente.verbos
    assert m.cliente.corpos[-1]["clip"]["status"] == "clip_failed"
    assert m.sender.stats().clips_given_up == 1


def test_nuvem_sem_url_de_upload_nao_deixa_o_clipe_preso(tmp_path):
    """Uma API que aceita o evento e não devolve URL deixaria o clipe na fila para
    sempre, ocupando o teto de disco até o TTL."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(status=202, body={"schema_version": 1})]))
    event_id = enfileira(m)

    drena(m)

    assert m.cliente.verbos == ["POST", "PATCH"]
    assert not m.clipes.path_for(event_id).exists()


# --- prioridade e vencimento ------------------------------------------------


def test_fila_de_eventos_represada_nao_deixa_clipe_furar(tmp_path):
    """Com o link ruim, o que precisa passar primeiro é o metadado de todos os eventos
    — cada um deles é um alerta esperando. Os clipes vão depois, na sobra."""
    m = monta(tmp_path)
    for _ in range(3):
        enfileira(m)

    verbos = []
    for _ in range(6):
        m.sender.tick()
        verbos.append(m.cliente.verbos[-1])

    assert verbos[:3] == ["POST", "POST", "POST"]
    assert set(verbos[3:]) <= {"PUT", "PATCH"}


def test_evento_vencido_e_descartado_com_registro(tmp_path, caplog):
    """Alertar sobre um furto de ontem ocupa a fila de triagem sem valor operacional
    (§3.6). Mas o descarte tem que aparecer no log: evento que some em silêncio é
    indistinguível de evento que nunca existiu."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(erro=NetworkError("link caído"))] * 3))
    event_id = enfileira(m)
    m.sender.tick()

    m.relogio.avanca(25 * 3600)
    with caplog.at_level("WARNING"):
        m.sender.tick()

    assert m.store.get(event_id) is None
    assert m.sender.stats().expired == 1
    assert event_id in caplog.text
    assert not m.clipes.path_for(event_id).exists()


def test_clipe_vence_antes_do_evento(tmp_path):
    """R-13: com o link fora por muito tempo, o vídeo é sacrificado para o alerta
    sobreviver. Sete horas depois o clipe já era; o evento ainda tem 17 h de prazo."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(erro=NetworkError("link caído"))] * 5))
    event_id = enfileira(m)
    m.sender.tick()

    m.relogio.avanca(7 * 3600)
    m.sender.tick()

    assert m.store.get(event_id) is not None, "o evento continua na fila"
    assert not m.clipes.path_for(event_id).exists()
    assert m.sender.stats().clips_expired == 1


def test_stats_da_fila_alimentam_o_heartbeat(tmp_path):
    """São `queue_depth`, `queue_oldest_age` e `queue_bytes` do §5.3 — o que permite
    ver de longe uma loja com o link caído, antes de o TTL começar a descartar."""
    m = monta(tmp_path, ClienteRoteirizado(post=[R(erro=NetworkError("caiu"))] * 3))
    enfileira(m, nascido_em=AGORA_S - 300)
    m.sender.tick()

    estado = m.store.stats(now_ms=int(m.relogio.agora * 1000))

    assert estado.depth == 1
    assert estado.oldest_age_s == pytest.approx(300.0, abs=1.0)
    assert estado.payload_bytes > 0


# --- ponta a ponta com HTTP de verdade --------------------------------------


def test_caminho_completo_contra_http_de_verdade(tmp_path):
    """O mesmo caminho feliz, mas com socket, `urllib` e bytes no fio.

    Os testes acima param no Protocol `CloudClient`, que é o nível certo para
    exercitar decisão. Este aqui existe para que a montagem inteira — sender, cliente,
    serialização, `Content-Length`, remoção do arquivo — seja exercitada junta ao menos
    uma vez, que é onde moram os erros de integração que nenhum dos dois lados vê.
    """
    nuvem = ServidorFalso()
    try:
        nuvem.roteiro = [
            RespostaHttp(
                status=202,
                corpo={
                    "schema_version": 1,
                    "clip_upload_url": nuvem.url + "upload/clipe.mp4",
                    "clip_object_key": "loja-01/clipe.mp4",
                },
            ),
            RespostaHttp(status=200),
            RespostaHttp(status=200),
        ]
        cliente = HttpCloudClient(nuvem.url, token="tok", timeout_s=5.0, upload_timeout_s=5.0)
        m = monta(tmp_path, cliente)
        event_id = enfileira(m)

        drena(m)

        assert [recebida.metodo for recebida in nuvem.recebidas] == ["POST", "PUT", "PATCH"]
        assert nuvem.recebidas[0].cabecalhos["Idempotency-Key"] == event_id
        assert nuvem.recebidas[1].corpo == b"mp4" * 1000, "os bytes do clipe chegaram inteiros"
        assert not m.clipes.path_for(event_id).exists()
    finally:
        nuvem.encerra()
