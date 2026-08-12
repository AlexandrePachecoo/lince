"""A orquestração do corte: gatilho, espera do pós-roll e desfechos.

Dois testes aqui são estruturais e valem mais que os outros juntos: o gatilho não
pode bloquear quem o chama, e a espera não pode acontecer na thread do fragmento.
Se qualquer um dos dois regredir, o sintoma não é clipe ruim — é o ffmpeg
bloqueando, e com ele a ingestão de todas as câmeras daquele processo.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from lince_agent.clip.buffer import ClipBuffer
from lince_agent.clip.recorder import ClipRecorder
from lince_agent.clip.remux import RemuxError
from lince_agent.clip.state import ClipResult, ClipStatus
from lince_agent.clip.store import ClipStore
from lince_agent.config import ClipOptions
from lince_agent.ffmpeg.fmp4 import Fmp4Parser, Fragment, InitSegment

GOP_S = 1.0
PRAZO_S = 5.0

OPCOES = ClipOptions(
    window_s=30.0,
    pre_roll_s=3.0,
    post_roll_s=4.0,
    post_roll_grace_s=2.0,
)
"""Rolls curtos: a fixture tem fragmentos de 1 s, então 3 s de pré-roll e 4 s de
pós-roll exercitam a mesma aritmética dos 5 s + 10 s do §3.5 sem precisar de dois
minutos de vídeo."""

PRAZO_TOTAL_S = OPCOES.post_roll_s + OPCOES.post_roll_grace_s
"""Quanto o recorder espera pelo pós-roll, contado **do gatilho**."""

FRESTA_S = 0.2
"""Prazo que sobra nos testes de expiração. Espera real, mas curta: nada vai chegar,
então a expiração é certa — o prazo pode demorar um pouco mais, nunca ficar instável.
Encurtar a espera pelo relógio é o que evita o `sleep` de 6 s."""


class RelogioFalso:
    """Relógio monotônico controlável.

    Em produção as duas réguas coincidem: o `triggered_at` vem do `Frame`, que
    carrega `time.monotonic()`. No teste o gatilho é um instante fictício (`at=10.0`)
    e o relógio de verdade marca o uptime do box — sem ancorar os dois na mesma
    origem, o recorder calcula um `decorrido` gigante, conclui que o prazo já venceu
    e corta sem nunca esperar o pós-roll.
    """

    def __init__(self, agora: float = 0.0) -> None:
        self.agora = agora

    def __call__(self) -> float:
        return self.agora


class RemuxerFalso:
    """Dublê do ffmpeg. Grava o que receberia e obedece a um roteiro de falhas.

    O remux de verdade já é exercitado com ffmpeg em `test_clip_remux.py`; aqui o
    que precisa ser exercitado é a orquestração — e o caminho de falha, que um
    ffmpeg de verdade não produz sob demanda.
    """

    def __init__(self, *, falha: Exception | None = None) -> None:
        self.falha = falha
        self.chamadas: list[tuple[bytes, Path]] = []
        self.threads: list[str] = []

    def __call__(self, data: bytes, destination: Path, *, timeout_s: float) -> None:
        self.threads.append(threading.current_thread().name)
        self.chamadas.append((data, destination))
        if self.falha is not None:
            raise self.falha
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)


def sessao(dados: bytes) -> tuple[InitSegment, list[Fragment]]:
    itens = Fmp4Parser().feed(dados, received_at=0.0)
    init = next(item for item in itens if isinstance(item, InitSegment))
    fragmentos = [
        replace(item, received_at=item.start_seconds + GOP_S)
        for item in itens
        if isinstance(item, Fragment)
    ]
    return init, fragmentos


@pytest.fixture
def montado(tmp_path, fmp4_sessao_longa: bytes):
    """Recorder ligado a uma câmera, já rodando, com resultados coletados."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", OPCOES)
    store = ClipStore(tmp_path / "clipes")
    remuxer = RemuxerFalso()
    relogio = RelogioFalso()
    resultados: list[ClipResult] = []
    chegou = threading.Event()

    def coleta(resultado: ClipResult) -> None:
        resultados.append(resultado)
        chegou.set()

    recorder = ClipRecorder(OPCOES, store, remuxer=remuxer, clock=relogio, on_result=coleta)
    recorder.attach("cam1", buffer)
    recorder.start()
    try:
        yield {
            "recorder": recorder,
            "buffer": buffer,
            "store": store,
            "remuxer": remuxer,
            "relogio": relogio,
            "init": init,
            "fragmentos": fragmentos,
            "resultados": resultados,
            "chegou": chegou,
        }
    finally:
        recorder.stop(timeout=PRAZO_S)


def alimenta(buffer: ClipBuffer, fragmentos: list[Fragment]) -> None:
    for fragmento in fragmentos:
        buffer.on_fragment(fragmento)


def dispara(montado, *, at: float, event_id: str = "evt-1", decorrido: float = 0.0) -> bool:
    """Põe o relógio no lugar certo e dispara o gatilho.

    `decorrido` é quanto tempo real já passou desde o instante do gatilho — o que o
    pedido esperou na fila atrás de outro corte. O recorder desconta isso do prazo do
    pós-roll de propósito: o §3.7 não devolve orçamento gasto na fila.
    """
    montado["relogio"].agora = at + decorrido
    return montado["recorder"].trigger("cam1", event_id=event_id, at=at)


def espera_resultado(montado) -> ClipResult:
    assert montado["chegou"].wait(PRAZO_S), "o recorder não produziu resultado no prazo"
    return montado["resultados"][-1]


def test_gatilho_devolve_clipe_com_pre_e_pos_roll_pedidos(montado):
    """Caminho feliz: o buffer já tem pré e pós-roll, o corte sai inteiro."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])

    # O fragmento 10 começa em t=10 na mídia e chegou em t=11 no relógio monotônico.
    assert dispara(montado, at=10.0)
    resultado = espera_resultado(montado)

    assert resultado.status is ClipStatus.OK
    assert resultado.pre_roll_s == pytest.approx(3.0, abs=GOP_S)
    assert resultado.post_roll_s == pytest.approx(4.0, abs=GOP_S)
    assert resultado.truncated_pre_roll is False
    assert resultado.truncated_post_roll is False
    assert resultado.session_lost is False
    assert resultado.path is not None and resultado.path.exists()
    assert resultado.size_bytes > 0
    assert resultado.duration_s == pytest.approx(7.0, abs=2 * GOP_S)


def test_o_clipe_comeca_pelo_init_segment(montado):
    """Sem o `moov` na frente nenhum fragmento é reproduzível. É o erro mais fácil
    de cometer montando os bytes e o mais fácil de não notar em teste unitário."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])

    dispara(montado, at=10.0)
    espera_resultado(montado)

    dados, _ = montado["remuxer"].chamadas[0]
    assert dados.startswith(init.data)


def test_gatilho_nao_bloqueia_a_thread_que_o_chamou(montado):
    """O motor de regras chamará isto da thread despachante do `FfmpegIngest`.
    Bloquear ali enche a `DropOldestQueue` e vira contrapressão no ffmpeg — o que
    trava a leitura do RTSP de todas as câmeras daquele processo."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:5])

    # Pede pós-roll que ainda não existe: o corte vai ter que esperar de verdade, e o
    # prazo inteiro (6 s) fica pendente. É isso que dá dente à asserção — se o
    # `trigger` esperasse junto, o `join` de 1 s abaixo não teria como terminar.
    inicio = threading.Event()

    def chama_o_gatilho() -> None:
        dispara(montado, at=4.0)
        inicio.set()

    thread = threading.Thread(target=chama_o_gatilho, name="motor-de-regras")
    thread.start()
    thread.join(timeout=1.0)

    assert not thread.is_alive(), "o gatilho segurou a thread do chamador"
    assert inicio.is_set()

    # Solta a espera que ficou pendente na thread do recorder: sem isto o corte só
    # terminaria no fim do prazo, e o encerramento da fixture pagaria os 6 s.
    alimenta(buffer, fragmentos[5:12])
    assert espera_resultado(montado).status is ClipStatus.OK


def test_espera_do_pos_roll_nao_roda_na_thread_do_fragmento(montado):
    """A proteção estrutural do teste anterior: o remux — e portanto tudo que veio
    antes dele, inclusive a espera — acontece na thread do recorder."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])

    dispara(montado, at=10.0)
    espera_resultado(montado)

    assert montado["remuxer"].threads == ["clip-recorder"]


def test_espera_o_pos_roll_antes_de_cortar(montado):
    """Cortar no instante do gatilho entregaria um clipe que termina exatamente na
    travessia — justamente a parte que o gerente precisa ver. Os fragmentos do
    pós-roll chegam **depois** do gatilho, e o corte tem que esperar por eles."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:11])

    dispara(montado, at=10.0)
    # Só agora chega o pós-roll, como aconteceria numa câmera de verdade.
    alimenta(buffer, fragmentos[11:20])
    resultado = espera_resultado(montado)

    assert resultado.status is ClipStatus.OK
    assert resultado.truncated_post_roll is False
    assert resultado.post_roll_s == pytest.approx(4.0, abs=GOP_S)


def test_pos_roll_que_nao_chega_vira_clipe_curto_marcado(montado):
    """Câmera de GOP longo, ou câmera que parou de entregar entre o gatilho e o
    corte. O alerta não pode esperar para sempre: o §3.7 tem 15 s de orçamento
    inteiro, e 10 deles já são o pós-roll."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:11])

    # Nada mais chega: o prazo tem que expirar sozinho. O relógio adiantado deixa só
    # uma fresta dele de pé, para a espera ser real sem custar os 6 s inteiros.
    dispara(montado, at=10.0, decorrido=PRAZO_TOTAL_S - FRESTA_S)
    resultado = espera_resultado(montado)

    assert resultado.status is ClipStatus.OK, "clipe curto ainda é clipe"
    assert resultado.truncated_post_roll is True
    assert resultado.post_roll_s < 4.0


def test_pedido_que_esperou_na_fila_corta_sem_esperar_mais(montado):
    """O prazo conta do gatilho, não do momento em que o pedido sai da fila.

    Com dois eventos quase simultâneos o segundo espera o corte do primeiro, e esse
    tempo já consumiu parte do pós-roll. Reiniciar a contagem aqui somaria a espera
    da fila ao pós-roll e estouraria o orçamento de 15 s do §3.7 — o alerta chegaria
    tarde justamente na rajada, que é quando ele mais importa.
    """
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:11])

    # O pedido saiu da fila depois do prazo inteiro: corta o que houver, na hora. Se
    # a contagem recomeçasse aqui, a espera seria de `PRAZO_TOTAL_S` — mais que o
    # `PRAZO_S` do `espera_resultado`, e é ele quem detecta a regressão.
    assert PRAZO_S < PRAZO_TOTAL_S, "o prazo do teste precisa ser menor que o do recorder"
    dispara(montado, at=10.0, decorrido=PRAZO_TOTAL_S + 1.0)
    resultado = espera_resultado(montado)

    assert resultado.status is ClipStatus.OK
    assert resultado.truncated_post_roll is True
    assert resultado.post_roll_s < 4.0


def test_pre_roll_curto_e_registrado_no_evento(montado):
    """§3.5: "pré-roll entregue menor que 5 s; registrado no evento". O triador que
    recebe 1 s de contexto precisa saber que foi limitação da câmera, não recorte."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:8])

    # Gatilho em t=1: só existe 1 s de vídeo antes dele, não os 3 s pedidos.
    dispara(montado, at=1.0)
    resultado = espera_resultado(montado)

    assert resultado.status is ClipStatus.OK
    assert resultado.truncated_pre_roll is True
    assert resultado.pre_roll_s < 3.0
    assert resultado.pre_roll_requested_s == 3.0


def test_reconexao_entre_gatilho_e_corte_nao_mistura_sessoes(montado, fmp4_outra_resolucao):
    """O bug central, agora no nível da orquestração.

    Os fragmentos que chegam depois da reconexão pertencem a outra execução do
    ffmpeg. Concatená-los com o init congelado no gatilho produz um arquivo que
    abre e mostra lixo — e o ffmpeg **não** reclama, como
    `test_misturar_sessoes_produz_lixo_que_o_ffmpeg_aceita` mede. Sobra o pré-roll,
    marcado."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:11])
    init_novo, fragmentos_novos = sessao(fmp4_outra_resolucao)

    dispara(montado, at=10.0)
    buffer.on_init_segment(init_novo)
    alimenta(buffer, fragmentos_novos[:6])
    resultado = espera_resultado(montado)

    assert resultado.session_lost is True
    dados, _ = montado["remuxer"].chamadas[0]
    for intruso in fragmentos_novos[:6]:
        assert intruso.data not in dados, "mídia da sessão nova entrou no clipe da antiga"


def test_buffer_vazio_no_gatilho_vira_clip_failed(montado):
    """Reinício do container (§3.5): o buffer em RAM se foi. O evento sobe sem
    clipe — perder o alerta porque o vídeo não saiu seria trocar um problema
    pequeno por um grande."""
    dispara(montado, at=10.0)
    resultado = espera_resultado(montado)

    assert resultado.status is ClipStatus.CLIP_FAILED
    assert resultado.path is None
    assert "buffer vazio" in (resultado.error or "")


def test_falha_do_remux_vira_clip_failed_e_o_evento_sobrevive(tmp_path, fmp4_sessao_longa):
    """Corte que falha não pode derrubar a thread nem sumir com o evento."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", OPCOES)
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])
    resultados: list[ClipResult] = []
    chegou = threading.Event()

    recorder = ClipRecorder(
        OPCOES,
        ClipStore(tmp_path / "clipes"),
        remuxer=RemuxerFalso(falha=RemuxError("keyframe ausente")),
        clock=RelogioFalso(10.0),
        on_result=lambda r: (resultados.append(r), chegou.set()),
    )
    recorder.attach("cam1", buffer)
    recorder.start()
    try:
        recorder.trigger("cam1", event_id="evt-1", at=10.0)
        assert chegou.wait(PRAZO_S)
    finally:
        recorder.stop(timeout=PRAZO_S)

    assert resultados[-1].status is ClipStatus.CLIP_FAILED
    assert "keyframe" in (resultados[-1].error or "")
    assert recorder.stats().failed == 1


def test_falha_do_remux_nao_deixa_arquivo_para_tras(tmp_path, fmp4_sessao_longa):
    """Um MP4 parcial no diretório de pendentes subiria como se fosse clipe, e a
    falha só apareceria quando um humano tentasse assistir."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", OPCOES)
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])
    store = ClipStore(tmp_path / "clipes")
    chegou = threading.Event()

    recorder = ClipRecorder(
        OPCOES,
        store,
        remuxer=RemuxerFalso(falha=RemuxError("container inválido")),
        clock=RelogioFalso(10.0),
        on_result=lambda _: chegou.set(),
    )
    recorder.attach("cam1", buffer)
    recorder.start()
    try:
        recorder.trigger("cam1", event_id="evt-1", at=10.0)
        assert chegou.wait(PRAZO_S)
    finally:
        recorder.stop(timeout=PRAZO_S)

    assert list(store.directory.glob("*.mp4")) == []


def test_fila_de_pedidos_cheia_descarta_e_conta(tmp_path, fmp4_sessao_longa):
    """Rajada de gatilhos é o sintoma de câmera mal calibrada (R-1). Ela não pode
    crescer memória nem bloquear o motor de regras — descarta, conta, e o número
    aparece no heartbeat."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", OPCOES)
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])
    opcoes = replace(OPCOES, pending_requests=2)

    # Sem `start()`: ninguém consome, então a fila enche na terceira tentativa.
    recorder = ClipRecorder(opcoes, ClipStore(tmp_path / "clipes"), remuxer=RemuxerFalso())
    recorder.attach("cam1", buffer)

    aceitos = [recorder.trigger("cam1", event_id=f"evt-{n}", at=10.0) for n in range(5)]

    assert aceitos == [True, True, False, False, False]
    stats = recorder.stats()
    assert stats.requested == 5
    assert stats.dropped == 3
    assert stats.pending == 2


def test_camera_desconhecida_e_erro_de_fiacao(tmp_path):
    """Gatilho para câmera sem buffer é bug de montagem do agente, não condição de
    operação: falhar alto na subida é melhor que engolir e nunca gerar clipe."""
    recorder = ClipRecorder(OPCOES, ClipStore(tmp_path / "clipes"), remuxer=RemuxerFalso())

    with pytest.raises(KeyError, match="cam9"):
        recorder.trigger("cam9", event_id="evt-1", at=1.0)


def test_dois_eventos_seguidos_saem_ambos(montado):
    """A thread é uma só e os cortes serializam; nenhum pode se perder no caminho."""
    buffer, init, fragmentos = montado["buffer"], montado["init"], montado["fragmentos"]
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])

    dispara(montado, at=8.0, event_id="evt-1")
    dispara(montado, at=12.0, event_id="evt-2")

    prazo = threading.Event()
    while not prazo.wait(0.02):
        if len(montado["resultados"]) >= 2:
            break
    assert {r.event_id for r in montado["resultados"]} == {"evt-1", "evt-2"}
    assert all(r.status is ClipStatus.OK for r in montado["resultados"])


def test_consumidor_que_levanta_nao_derruba_o_recorder(tmp_path, fmp4_sessao_longa):
    """O `on_result` é código de outro estágio. Uma exceção dele não pode matar a
    thread que corta os clipes de todas as câmeras — é a mesma regra do `_guarded`
    do `ffmpeg/process.py`."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", OPCOES)
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:20])
    vistos: list[str] = []

    def explode(resultado: ClipResult) -> None:
        vistos.append(resultado.event_id)
        raise RuntimeError("o estágio 6 quebrou")

    recorder = ClipRecorder(
        OPCOES,
        ClipStore(tmp_path / "clipes"),
        remuxer=RemuxerFalso(),
        clock=RelogioFalso(10.0),
        on_result=explode,
    )
    recorder.attach("cam1", buffer)
    recorder.start()
    try:
        recorder.trigger("cam1", event_id="evt-1", at=10.0)
        recorder.trigger("cam1", event_id="evt-2", at=12.0)
        prazo = threading.Event()
        while not prazo.wait(0.02):
            if len(vistos) >= 2:
                break
    finally:
        recorder.stop(timeout=PRAZO_S)

    assert vistos == ["evt-1", "evt-2"], "a thread morreu no primeiro consumidor que levantou"


def test_stop_encerra_a_thread(tmp_path):
    recorder = ClipRecorder(OPCOES, ClipStore(tmp_path / "clipes"), remuxer=RemuxerFalso())
    recorder.start()

    recorder.stop(timeout=PRAZO_S)

    assert all(thread.name != "clip-recorder" for thread in threading.enumerate())


def test_start_duplo_e_erro(tmp_path):
    recorder = ClipRecorder(OPCOES, ClipStore(tmp_path / "clipes"), remuxer=RemuxerFalso())
    recorder.start()
    try:
        with pytest.raises(RuntimeError, match="já está rodando"):
            recorder.start()
    finally:
        recorder.stop(timeout=PRAZO_S)
