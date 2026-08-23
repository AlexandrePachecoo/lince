"""O transporte do heartbeat (§5.3): o que ele soma, o que ele descarta, o que ele nunca faz.

O que se está protegendo aqui não é o envio — postar JSON é o pedaço trivial. É o
comportamento sob falha, que é onde o §5.4 mora: um heartbeat que se acumula em fila
entrega à nuvem, depois de uma noite offline, duzentas fotografias do passado; um
heartbeat que levanta mata a telemetria da loja em silêncio; um heartbeat que entra em
backoff por corpo inválido atrasa justamente o primeiro envio válido depois do deploy
que conserta.

Nada aqui dorme: `tick()` é síncrono e o relógio é injetado (regra 6 do CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from telemetria import camera, deteccao, saude

from lince_agent.config import RetryOptions
from lince_agent.heartbeat import HeartbeatSender, espaco_livre, monta_heartbeat
from lince_agent.ingest.state import CameraStatus
from lince_agent.outbox.http import CloudResponse, NetworkError
from lince_agent.outbox.state import OutboxStats


def _to_iso(monotonic_s: float) -> str:
    return f"2026-01-01T00:00:{monotonic_s:06.3f}Z"


class NuvemDeMentira:
    """Dublê no limite do que o `HeartbeatSender` usa: um método só."""

    def __init__(self, *respostas: CloudResponse | Exception) -> None:
        self.respostas = list(respostas)
        self.corpos: list[dict] = []

    def post_heartbeat(self, payload: dict) -> CloudResponse:
        self.corpos.append(payload)
        resposta = self.respostas.pop(0) if self.respostas else CloudResponse(status=204)
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


def _sender(cliente, *, health=None, **kwargs) -> HeartbeatSender:
    return HeartbeatSender(
        cliente,
        health=health or (lambda: saude()),
        agent_version="0.1.0",
        to_iso=_to_iso,
        **kwargs,
    )


# --- o corpo ---------------------------------------------------------------


def test_dropped_frames_soma_os_dois_pontos_de_descarte():
    """O schema tem um campo e o agente tem dois contadores de descarte.

    A ingestão descarta quando o consumidor não acompanha (§3.1); a fila do estágio 2
    descarta quando a GPU não acompanha (R-4). Os dois são frame decodificado que não
    virou inferência, que é o que o campo mede. Reportar só um deles esconderia metade
    da saturação de um box — e é a saturação que explica o alerta que não saiu.
    """
    corpo = monta_heartbeat(
        saude(
            cameras=(camera(frames_dropped=40),),
            deteccoes=(deteccao(dropped=17),),
        ),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    assert corpo["cameras"][0]["dropped_frames"] == 57


def test_queue_bytes_conta_o_clipe_em_disco():
    """A fila é dois números em lugares diferentes: o payload JSON no Redis e o MP4 no
    disco. O clipe é maior por três ordens de grandeza.

    Reportar só o JSON mostraria uma fila de alguns KB numa loja cujo disco está
    enchendo — e o teto do §3.6 começaria a despejar clipes sem que nada no heartbeat
    tivesse dado sinal antes.
    """
    corpo = monta_heartbeat(
        saude(outbox=OutboxStats(depth=3, payload_bytes=4_096), clip_disk_bytes=90_000_000),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    assert corpo["queue"]["bytes"] == 90_004_096
    assert corpo["queue"]["depth"] == 3


def test_a_versao_reportada_e_a_que_esta_valendo_nao_a_que_a_nuvem_serviu():
    """`config_version` sai do `ConfigHealth`, de quem **aplicou**.

    Um documento estrutural é recebido, validado, cacheado e recusado até o reinício. Um
    agente que reportasse a versão baixada declararia rodar uma calibração que nunca
    esteve em pé, e a investigação de um falso positivo partiria de limiares que não
    existiram (R-1). O `pendente_version` fica de fora do corpo de propósito: quem
    denuncia a divergência é a comparação da nuvem entre o que ela serve e o que o
    agente diz estar rodando.
    """
    corpo = monta_heartbeat(
        saude(config_version="v6"),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    assert corpo["config_version"] == "v6"


def test_restarts_soma_os_respawns_de_ffmpeg_das_cameras():
    """Com `uptime_s` ao lado, é o que distingue "o box reiniciou agora" de "uma câmera
    está em crash loop" (R-6/R-7) — duas situações com ações de operador diferentes."""
    corpo = monta_heartbeat(
        saude(cameras=(camera("cam1", restarts=2), camera("cam2", restarts=31))),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    assert corpo["restarts"] == 33


def test_espaco_livre_devolve_none_em_caminho_inexistente(tmp_path):
    """Métrica opcional nunca pode derrubar o envio.

    O `--clips-dir` pode não existir ainda no primeiro heartbeat de uma subida, e um
    `OSError` propagado daqui mataria justamente o heartbeat que ia contar que o box
    acabou de subir.
    """
    assert espaco_livre(tmp_path) is not None
    assert espaco_livre(tmp_path / "nao-existe" / "fundo") is None


# --- o transporte ----------------------------------------------------------


def test_o_envio_aceito_conta_e_marca_o_ultimo_sucesso():
    relogio = iter([100.0, 200.0])
    cliente = NuvemDeMentira(CloudResponse(status=204))
    sender = _sender(cliente, clock=lambda: next(relogio))

    assert sender.tick() is True

    stats = sender.stats()
    assert (stats.enviados, stats.falhas) == (1, 0)
    assert stats.ultimo_sucesso_s == 100.0


def test_o_heartbeat_que_falhou_nao_e_reenviado_o_seguinte_e_novo():
    """**A regra central deste módulo.** Um envio que falhou não deixa nada para trás.

    Se houvesse fila, a nuvem receberia depois de uma noite offline duzentos
    instantâneos de uma loja que já mudou — e a decisão que ela precisa tomar
    ("esta loja está com a fila represada agora?") seria tomada sobre o mais velho
    deles. Aqui a checagem é direta: a fila cresce entre os dois ticks, e o corpo que
    sai no segundo é o novo, não o que falhou.
    """
    saudes = iter(
        [
            saude(outbox=OutboxStats(depth=1)),
            saude(outbox=OutboxStats(depth=99)),
        ]
    )
    cliente = NuvemDeMentira(NetworkError("link caiu"), CloudResponse(status=204))
    sender = _sender(cliente, health=lambda: next(saudes))

    assert sender.tick() is False
    assert sender.tick() is True

    assert [corpo["queue"]["depth"] for corpo in cliente.corpos] == [1, 99]
    assert sender.stats().enviados == 1


def test_falha_de_rede_nao_levanta_e_o_pipeline_nao_percebe():
    """A última linha da §5.4: nenhuma falha de rede pode parar a detecção.

    Uma exceção escapando do `tick` mataria a thread de telemetria, e o sintoma na nuvem
    seria idêntico ao de um box desligado — alerta de agente offline para uma loja que
    está detectando normalmente.
    """
    sender = _sender(NuvemDeMentira(NetworkError("dns")))

    assert sender.tick() is False
    assert sender.stats().falhas == 1


def test_corpo_recusado_conta_invalido_e_nao_entra_em_backoff():
    """`400` é divergência de contrato entre `monta_heartbeat` e o schema — bug nosso.

    Não se resolve esperando, e alargar o intervalo até o teto atrasaria o primeiro
    heartbeat **válido** depois do deploy que conserta. Por isso o contador é separado
    do de falhas: `falhas` alto manda olhar a rede, `inválidos` alto manda olhar o
    código, e confundir os dois é mandar depurar no lugar errado.
    """
    sender = _sender(NuvemDeMentira(CloudResponse(status=400)))

    assert sender.tick() is False

    stats = sender.stats()
    assert (stats.invalidos, stats.falhas) == (1, 0)
    assert sender._espera() == pytest.approx(30.0)


def test_credencial_recusada_conta_separado_e_espera_o_retry_after():
    """`401` não é bug nem é rede: é rotação de credencial ou revogação, e o conserto é
    humano. Contador próprio para não se confundir com link ruim, e o `Retry-After` da
    nuvem é respeitado como **piso** — a mesma regra do §5.4 em todo o agente."""
    cliente = NuvemDeMentira(CloudResponse(status=401, retry_after="120"))
    sender = _sender(cliente, wall_clock=lambda: 0.0, jitter=lambda: 0.0)

    assert sender.tick() is False

    stats = sender.stats()
    assert (stats.recusados, stats.falhas) == (1, 1)
    assert sender._espera() == pytest.approx(120.0)


def test_o_backoff_soma_ao_intervalo_e_respeita_o_teto():
    """Com a nuvem fora do ar não há motivo para mandar telemetria mais rápido que em
    regime, nem para martelar: o backoff nunca desce abaixo dos 30 s e nunca passa do
    teto do `RetryOptions`."""
    cliente = NuvemDeMentira(*[CloudResponse(status=503) for _ in range(12)])
    sender = _sender(
        cliente,
        retry=RetryOptions(base_s=2.0, cap_s=300.0, jitter_s=0.0),
        jitter=lambda: 0.0,
    )

    esperas = []
    for _ in range(12):
        sender.tick()
        esperas.append(sender._espera())

    assert esperas[0] == pytest.approx(30.0)
    assert esperas == sorted(esperas), "o backoff nunca encurta enquanto a nuvem não volta"
    assert max(esperas) <= 300.0
    assert sender.stats().falhas == 12


def test_o_sucesso_zera_o_backoff():
    """Sem isto, uma loja com um pico de instabilidade ficaria em intervalo de minutos
    pelo resto do dia, e a nuvem a veria como meio-offline muito depois de o link ter
    voltado.

    Repare que são precisas várias falhas para o backoff sequer aparecer: enquanto o
    exponencial estiver abaixo dos 30 s de regime, o intervalo vence. Isso é o desenho,
    não um detalhe do teste — não existe motivo para mandar telemetria mais rápido que
    em regime só porque a última tentativa falhou.
    """
    cliente = NuvemDeMentira(*[CloudResponse(status=503) for _ in range(6)])
    sender = _sender(cliente, jitter=lambda: 0.0)

    for _ in range(6):
        sender.tick()
    assert sender._espera() > 30.0

    cliente.respostas.append(CloudResponse(status=204))
    sender.tick()
    assert sender._espera() == pytest.approx(30.0)


# --- o relógio -------------------------------------------------------------


def test_o_skew_sai_do_date_da_resposta_e_entra_no_heartbeat_seguinte():
    """`clock_skew_s` compara o relógio do box com o da nuvem, e a única fonte do
    relógio da nuvem é o cabeçalho `Date`.

    A medição só existe depois de a requisição ter partido, então ela carimba o envio
    **seguinte** — não há como pôr no corpo um número que ainda não foi medido. Um box
    com o RTC 45 s adiantado é o que explica um `occurred_at` que a triagem não consegue
    casar com o clipe.
    """
    # 2026-01-01T00:00:00Z na nuvem; o box acha que são 00:00:45.
    cliente = NuvemDeMentira(
        CloudResponse(status=204, date="Thu, 01 Jan 2026 00:00:00 GMT"),
        CloudResponse(status=204, date="Thu, 01 Jan 2026 00:00:30 GMT"),
    )
    sender = _sender(cliente, wall_clock=lambda: 1767225645.0)

    sender.tick()
    assert cliente.corpos[0]["clock_skew_s"] == 0.0, "o primeiro envio ainda não mediu nada"
    assert sender.stats().clock_skew_s == pytest.approx(45.0)

    sender.tick()
    assert cliente.corpos[1]["clock_skew_s"] == pytest.approx(45.0)


def test_o_skew_e_medido_tambem_na_resposta_de_erro():
    """Um `503` também carrega `Date`, e um box com o relógio errado precisa ser
    denunciado mesmo quando a API está ruim. Descartar a medição junto com o erro
    deixaria o skew congelado justamente durante uma janela de instabilidade."""
    cliente = NuvemDeMentira(CloudResponse(status=503, date="Thu, 01 Jan 2026 00:00:00 GMT"))
    sender = _sender(cliente, wall_clock=lambda: 1767225610.0)

    sender.tick()

    assert sender.stats().skew_medido is True
    assert sender.stats().clock_skew_s == pytest.approx(10.0)


def test_sem_date_o_skew_zero_se_declara_como_nao_medido():
    """`skew=0.0` e `skew=?` significam coisas opostas.

    Sem a distinção, um agente que nunca conseguiu falar com a nuvem reportaria relógio
    em dia com toda a convicção do mundo — e o `clock_skew` do §5.3, que existe
    justamente para desconfiar dos instantes daquele box, endossaria todos eles.
    """
    sender = _sender(NuvemDeMentira(CloudResponse(status=204)))

    sender.tick()

    stats = sender.stats()
    assert (stats.clock_skew_s, stats.skew_medido) == (0.0, False)


def test_date_ilegivel_nao_derruba_o_envio():
    """Proxy que reescreve cabeçalho existe, e um `Date` que o `email.utils` não parseia
    não pode transformar um heartbeat aceito em falha."""
    cliente = NuvemDeMentira(CloudResponse(status=204, date="ontem à tarde"))
    sender = _sender(cliente)

    assert sender.tick() is True
    assert sender.stats().skew_medido is False


# --- o laço ----------------------------------------------------------------


def test_a_saude_e_lida_a_cada_tick_nunca_guardada():
    """Um `AgentHealth` capturado na subida descreveria para sempre uma loja que já não
    existe: zero frames, nenhuma câmera em pé, fila vazia. O valor do heartbeat é ser
    recente."""
    chamadas = []

    def health():
        chamadas.append(1)
        return saude(uptime_s=float(len(chamadas)))

    sender = _sender(NuvemDeMentira(), health=health)
    sender.tick()
    sender.tick()

    assert len(chamadas) == 2
    assert [corpo["uptime_s"] for corpo in sender._client.corpos] == [1.0, 2.0]


def test_o_laco_sobrevive_a_uma_excecao_inesperada():
    """Nada previsto levanta no `tick`, mas "nada previsto" é a parte frágil da frase.

    Uma exceção escapando do laço mataria a telemetria da loja em silêncio — o agente
    seguiria detectando (que é o que a §5.4 quer) e a nuvem o declararia offline para
    sempre, sem um erro em lugar nenhum.
    """

    class NuvemQuebrada:
        def post_heartbeat(self, payload):
            raise RuntimeError("bug de verdade, não falha de rede")

    sender = _sender(NuvemQuebrada())
    sender.start()
    try:
        # `stop` espera a thread; o `join` é o que garante que ao menos um tick rodou
        # sem precisar de `sleep` nem de polling por tempo.
        sender.stop()
    finally:
        assert sender.stats().falhas >= 1


def test_nao_da_para_subir_dois_lacos_no_mesmo_sender():
    """Dois laços dobrariam a taxa de heartbeat da loja e, pior, escreveriam nos mesmos
    contadores — o `--stats` mentiria sobre o ritmo real."""
    sender = _sender(NuvemDeMentira())
    sender.start()
    try:
        with pytest.raises(RuntimeError):
            sender.start()
    finally:
        sender.stop()


def test_o_status_da_camera_sobe_como_o_literal_do_contrato():
    """`CameraStatus` é `StrEnum`, e `str()` sobre ela dá o literal — mas um `f"{...}"`
    descuidado sobre uma `Enum` comum daria `CameraStatus.OFFLINE`, que o schema recusa
    e a API devolve como `400`."""
    corpo = monta_heartbeat(
        saude(cameras=(camera(status=CameraStatus.RECONNECTING),)),
        agent_version="0.1.0",
        to_iso=_to_iso,
    )

    assert corpo["cameras"][0]["status"] == "reconnecting"


def test_o_corpo_nao_carrega_identidade_da_loja():
    """Quem o agente é vem do `Authorization: Bearer`, como no `GET /v1/agents/config`.

    Um `tenant_id` no corpo seria uma segunda fonte de verdade sobre identidade, e a
    primeira coisa que um agente comprometido tentaria trocar (NFR-6).
    """
    corpo = monta_heartbeat(saude(), agent_version="0.1.0", to_iso=_to_iso)

    assert "tenant_id" not in corpo
    assert "store_id" not in corpo
    assert "agent_id" not in corpo


def test_o_corpo_de_saida_e_o_mesmo_apos_um_replace_na_saude():
    """Guarda de regressão barata sobre a leitura do `AgentHealth`: os campos vêm de
    onde se pensa que vêm, não de um default que coincide."""
    base = saude(uptime_s=10.0)
    corpo = monta_heartbeat(replace(base, uptime_s=999.0), agent_version="9.9.9", to_iso=_to_iso)

    assert corpo["uptime_s"] == 999.0
    assert corpo["agent_version"] == "9.9.9"
