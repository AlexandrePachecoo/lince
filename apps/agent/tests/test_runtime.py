"""A montagem do agente: o ponto em que o estágio 5 deixa de ser uma ilha.

O que esta suíte guarda é a fiação, e fiação errada falha de um jeito específico e
caro: o clipe de uma câmera vai para o buffer de outra, o gatilho de uma rajada
descarta o alerta junto com o vídeo, ou o evento sai sem regra porque o rascunho se
perdeu na espera do pós-roll. Nada disso levanta erro — tudo isso produz um alerta
errado na mão do gerente, que é o pior desfecho possível num sistema que produz
suspeita (R-10).

O ffmpeg não entra: a ingestão já é exercitada com processo real em `test_process.py`
e ponta a ponta em `test_integration.py`. Aqui o supervisor é injetado, e é ele que
entrega fragmentos de verdade — produzidos pelo ffmpeg na fixture — nos callbacks.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from deteccoes import DetectorFalso, DetectorQueFalha, pessoa
from deteccoes import frame as quadro

from lince_agent.clip.state import ClipResult, ClipStatus
from lince_agent.clip.store import ClipStore
from lince_agent.config import (
    AgentConfig,
    CameraConfig,
    ClipOptions,
    CloudOptions,
    DetectionOptions,
    OutboxOptions,
    RetryOptions,
)
from lince_agent.ffmpeg.fmp4 import Fmp4Parser, Fragment, InitSegment
from lince_agent.ffmpeg.process import IngestCallbacks
from lince_agent.ingest.state import CameraHealth, CameraStatus
from lince_agent.outbox.event import EventSource
from lince_agent.outbox.http import CloudResponse, NetworkError
from lince_agent.outbox.state import ClipState
from lince_agent.outbox.store import MemoryOutbox
from lince_agent.runtime import AgentRuntime

PRAZO_S = 5.0
GOP_S = 1.0

CORTE = ClipOptions(window_s=30.0, pre_roll_s=3.0, post_roll_s=4.0, post_roll_grace_s=2.0)
"""Rolls curtos pelo mesmo motivo de `test_clip_recorder.py`: a fixture tem fragmentos
de 1 s, e a aritmética é a mesma dos 5 s + 10 s do §3.5."""

OPCOES_FILA = OutboxOptions(
    key_prefix="teste", retry=RetryOptions(base_s=1.0, cap_s=10.0, jitter_s=0.0)
)


class SupervisorFalso:
    """Dublê no lugar do `CameraSupervisor`, guardando os callbacks que recebeu.

    É por eles que o teste entrega fragmentos — os mesmos objetos que o `FfmpegIngest`
    entregaria, produzidos por um ffmpeg de verdade na fixture. O que fica de fora é o
    processo, que já tem suíte própria.
    """

    def __init__(self, camera: CameraConfig, callbacks: IngestCallbacks) -> None:
        self.camera = camera
        self.callbacks = callbacks
        self.rodando = False
        self.parado = False

    def start(self) -> None:
        self.rodando = True

    def stop(self, timeout: float = 15.0) -> None:
        self.rodando = False
        self.parado = True

    def health(self) -> CameraHealth:
        return CameraHealth(
            camera_id=self.camera.camera_id,
            status=CameraStatus.OK if self.rodando else CameraStatus.STOPPED,
            consecutive_failures=0,
            restarts=0,
            sampled_fps=3.0,
            frames=10,
            frames_dropped=0,
            fragments=5,
            fragments_dropped=0,
            fragment_bytes=1000,
            last_frame_at=None,
        )


class ClienteMudo:
    """Nuvem que aceita tudo. Os desfechos de rede são assunto de `test_outbox_sender`."""

    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.uploads: list[str] = []

    def post_event(self, payload, *, event_id):
        self.posts.append(payload)
        return CloudResponse(
            status=202,
            body={
                "schema_version": 1,
                "clip_upload_url": "https://r2.exemplo/loja-01/clipe.mp4",
                "clip_object_key": f"rede-abc/{event_id}.mp4",
            },
        )

    def put_clip(self, url, path, *, content_type="video/mp4"):
        self.uploads.append(path.name)
        return CloudResponse(status=200)

    def patch_event(self, event_id, payload):
        return CloudResponse(status=200)


class ClienteCaido:
    """O link da loja fora do ar: nada sai da fila, e é isso que a torna observável."""

    def post_event(self, payload, *, event_id):
        raise NetworkError("link da loja fora do ar")

    def put_clip(self, url, path, *, content_type="video/mp4"):
        raise NetworkError("link da loja fora do ar")

    def patch_event(self, event_id, payload):
        raise NetworkError("link da loja fora do ar")


class RelogioFalso:
    def __init__(self, agora: float = 0.0) -> None:
        self.agora = agora

    def __call__(self) -> float:
        return self.agora


class RemuxerFalso:
    """O remux de verdade tem suíte própria com ffmpeg (`test_clip_remux.py`)."""

    def __call__(self, data: bytes, destination: Path, *, timeout_s: float) -> None:
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


class Agente:
    """Runtime montado com dublês nas bordas e componentes reais no meio."""

    def __init__(
        self,
        tmp_path,
        *,
        cameras: tuple[str, ...] = ("cam1",),
        cliente=None,
        detector=None,
        inelegiveis: tuple[str, ...] = (),
    ):
        self.relogio = RelogioFalso()
        self.parede = RelogioFalso(1_700_000_000.0)
        self.store = MemoryOutbox(OPCOES_FILA)
        # Link caído por padrão, e isso não é pessimismo: com a nuvem aceitando, o
        # sender esvazia a fila entre o gatilho e a asserção, e o teste passa a
        # depender de quem correr mais rápido. Quem quer o caminho completo pede
        # `ClienteMudo` explicitamente.
        self.cliente = cliente or ClienteCaido()
        self.supervisores: dict[str, SupervisorFalso] = {}
        self.resultados: list[ClipResult] = []
        self.chegou = threading.Event()

        self.config = AgentConfig(
            tenant_id="rede-abc",
            store_id="loja-01",
            cameras=tuple(
                CameraConfig(
                    camera_id=nome,
                    url=f"rtsp://camera/{nome}",
                    clip=CORTE,
                    detect=nome not in inelegiveis,
                )
                for nome in cameras
            ),
            clips_dir=tmp_path / "clipes",
            outbox=OPCOES_FILA,
            cloud=CloudOptions(api_url="http://nuvem.invalida"),
        )
        self.clipes = ClipStore(self.config.clips_dir, max_bytes=CORTE.max_disk_bytes)

        from lince_agent.clip.recorder import ClipRecorder

        self.recorder = ClipRecorder(
            CORTE,
            self.clipes,
            remuxer=RemuxerFalso(),
            clock=self.relogio,
            on_result=self._observa,
        )
        self.runtime = AgentRuntime(
            self.config,
            store=self.store,
            client=self.cliente,
            clip_store=self.clipes,
            clock=self.relogio,
            wall_clock=self.parede,
            supervisor_factory=self._fabrica,
            recorder=self.recorder,
            detector=detector,
        )
        # O recorder foi construído com o `on_result` do teste, então encadeamos: o
        # runtime precisa continuar recebendo o resultado, senão nada é enfileirado.
        self._encadeia()

    def _encadeia(self) -> None:
        self._do_runtime = self.runtime._ao_terminar_clipe

    def _observa(self, resultado: ClipResult) -> None:
        self._do_runtime(resultado)
        self.resultados.append(resultado)
        self.chegou.set()

    def _fabrica(self, camera: CameraConfig, callbacks: IngestCallbacks) -> SupervisorFalso:
        supervisor = SupervisorFalso(camera, callbacks)
        self.supervisores[camera.camera_id] = supervisor
        return supervisor

    def alimenta(self, camera_id: str, init: InitSegment, fragmentos) -> None:
        callbacks = self.supervisores[camera_id].callbacks
        callbacks.on_init_segment(init)
        for fragmento in fragmentos:
            callbacks.on_fragment(fragmento)

    def alimenta_frame(self, camera_id: str, quadro) -> None:
        """Entrega um frame pelo mesmo callback que a thread despachante do ffmpeg
        usaria. É por aqui que o estágio 2 recebe o que recebe em produção."""
        self.supervisores[camera_id].callbacks.on_frame(quadro)

    def espera_clipe(self) -> ClipResult:
        assert self.chegou.wait(PRAZO_S), "o clipe não ficou pronto no prazo"
        self.chegou.clear()
        return self.resultados[-1]


@pytest.fixture
def agente(tmp_path):
    montado = Agente(tmp_path)
    yield montado
    montado.runtime.stop(timeout=PRAZO_S)


@pytest.fixture
def sessao_longa(fmp4_sessao_longa: bytes):
    return sessao(fmp4_sessao_longa)


# --- fiação -----------------------------------------------------------------


def test_fragmento_vai_para_o_buffer_da_propria_camera(tmp_path, sessao_longa):
    """Duas câmeras, dois buffers. Cruzar os callbacks entregaria ao triador o vídeo
    de outro corredor — e ninguém perceberia, porque o clipe seria válido."""
    montado = Agente(tmp_path, cameras=("cam1", "cam2"))
    montado.runtime.start()
    try:
        init, fragmentos = sessao_longa
        montado.alimenta("cam1", init, fragmentos[:10])

        saude = {b.camera_id: b for b in montado.runtime.health().buffers}
        assert saude["cam1"].fragments == 10
        assert saude["cam2"].fragments == 0
        assert saude["cam2"].has_init is False
    finally:
        montado.runtime.stop(timeout=PRAZO_S)


def test_cada_camera_usa_o_proprio_clip_options(tmp_path):
    """`CameraConfig.clip` era campo morto: existia, era validado e ninguém lia. Uma
    câmera de bitrate alto precisa do próprio teto de RAM, senão ela espreme o buffer
    das outras (§3.5, R-4)."""
    apertada = replace(CORTE, max_bytes=64 * 1024)
    config = AgentConfig(
        tenant_id="t",
        store_id="l",
        cameras=(
            CameraConfig(camera_id="cam1", url="rtsp://a", clip=CORTE),
            CameraConfig(camera_id="cam2", url="rtsp://b", clip=apertada),
        ),
        clips_dir=tmp_path / "clipes",
    )
    supervisores = {}

    def fabrica(camera, callbacks):
        supervisores[camera.camera_id] = SupervisorFalso(camera, callbacks)
        return supervisores[camera.camera_id]

    runtime = AgentRuntime(
        config,
        store=MemoryOutbox(OPCOES_FILA),
        client=ClienteMudo(),
        supervisor_factory=fabrica,
    )
    runtime.start()
    try:
        assert runtime.camera_ids == ("cam1", "cam2")
        # O teto por câmera é observável pelo que o buffer aceita reter.
        assert config.cameras[1].clip.max_bytes == 64 * 1024
    finally:
        runtime.stop(timeout=PRAZO_S)


def test_rolls_divergentes_entre_cameras_falham_na_subida(tmp_path):
    """O corte é um só para o agente. Aceitar a divergência cortaria com o pós-roll da
    câmera errada — clipe curto, sem o momento do evento, e nada no log dizendo isso."""
    with pytest.raises(ValueError, match="divergem"):
        AgentConfig(
            tenant_id="t",
            store_id="l",
            cameras=(
                CameraConfig(camera_id="cam1", url="rtsp://a", clip=CORTE),
                CameraConfig(camera_id="cam2", url="rtsp://b", clip=replace(CORTE, post_roll_s=8)),
            ),
            clips_dir=tmp_path / "clipes",
        )


def test_camera_repetida_falha_na_subida(tmp_path):
    with pytest.raises(ValueError, match="repetido"):
        AgentConfig(
            tenant_id="t",
            store_id="l",
            cameras=(
                CameraConfig(camera_id="cam1", url="rtsp://a"),
                CameraConfig(camera_id="cam1", url="rtsp://b"),
            ),
            clips_dir=tmp_path / "clipes",
        )


# --- gatilho até a fila -----------------------------------------------------


def test_gatilho_produz_evento_na_fila_com_o_clipe_medido(agente, sessao_longa):
    """O caminho inteiro do estágio 6: gatilho, corte, payload, fila. O que precisa
    chegar à nuvem é o que a borda **mediu**, não o que ela pediu (§3.5, §6)."""
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])

    agente.relogio.agora = 10.0
    event_id = agente.runtime.trigger("cam1", at=8.0)
    agente.espera_clipe()

    item = agente.store.get(event_id)
    assert item is not None
    assert item.payload["camera_id"] == "cam1"
    assert item.payload["clip"]["status"] == "ok"
    assert item.payload["clip"]["pre_roll_requested_s"] == CORTE.pre_roll_s
    assert item.clip_state is ClipState.PENDING
    assert Path(item.clip_path).exists()


def test_evento_sabe_quando_aconteceu_e_quando_foi_relatado(agente, sessao_longa):
    """`occurred_at` vem do gatilho, `reported_at` do enfileiramento. Com o link caído
    os dois se afastam, e é dessa distância que a nuvem tira a saúde do relógio da
    loja (§5.3)."""
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])

    agente.relogio.agora = 10.0
    event_id = agente.runtime.trigger("cam1", at=8.0)
    agente.parede.agora += 45.0
    agente.espera_clipe()

    payload = agente.store.get(event_id).payload
    assert payload["occurred_at"] < payload["reported_at"]


def test_gatilho_de_regra_carrega_a_regra(agente, sessao_longa):
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])

    agente.relogio.agora = 10.0
    event_id = agente.runtime.trigger(
        "cam1", at=8.0, source=EventSource.RULE, rule_id="saida-sem-caixa", rule_version=3
    )
    agente.espera_clipe()

    assert agente.store.get(event_id).payload["rule"] == {"id": "saida-sem-caixa", "version": 3}


def test_gatilho_manual_nao_finge_ser_regra(agente, sessao_longa):
    """O andaime da CLI e o teste de câmera na instalação não podem contaminar a
    métrica de falso positivo por câmera, que é o número número 1 do R-1."""
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])

    agente.relogio.agora = 10.0
    event_id = agente.runtime.trigger("cam1", at=8.0)
    agente.espera_clipe()

    payload = agente.store.get(event_id).payload
    assert payload["source"] == "manual"
    assert payload["rule"] is None


def test_camera_desconhecida_e_recusada_na_hora(agente):
    agente.runtime.start()

    with pytest.raises(KeyError, match="cam-fantasma"):
        agente.runtime.trigger("cam-fantasma")


def test_clipe_descartado_por_fila_cheia_ainda_enfileira_o_evento(tmp_path, sessao_longa):
    """Rajada numa câmera mal calibrada (R-1): o recorder recusa o gatilho. Perder o
    alerta junto com o vídeo trocaria um problema pequeno por um grande — e é
    justamente na câmera que dispara demais que o operador precisa ver o que houve."""

    class RecorderLotado:
        def attach(self, camera_id, buffer):
            pass

        def trigger(self, camera_id, *, event_id, at):
            return False

        def start(self):
            pass

        def stop(self, timeout=30.0):
            pass

        def stats(self):
            from lince_agent.clip.state import RecorderStats

            return RecorderStats(dropped=1)

    montado = Agente(tmp_path)
    runtime = AgentRuntime(
        montado.config,
        store=montado.store,
        client=montado.cliente,
        clip_store=montado.clipes,
        clock=montado.relogio,
        wall_clock=montado.parede,
        supervisor_factory=montado._fabrica,
        recorder=RecorderLotado(),
    )
    runtime.start()
    try:
        event_id = runtime.trigger("cam1", at=1.0)

        item = montado.store.get(event_id)
        assert item is not None, "o alerta tem que subir mesmo sem vídeo"
        assert item.payload["clip"]["status"] == "clip_failed"
        assert item.payload["clip"]["error"] == "fila de cortes cheia"
        assert item.clip_state is ClipState.NONE
    finally:
        runtime.stop(timeout=PRAZO_S)


def test_clipe_orfao_nao_derruba_o_agente(agente):
    """`ClipResult` sem rascunho não deveria acontecer; se acontecer, o vídeo é
    descartado com ERROR em vez de virar evento sem regra e sem instante."""
    agente.runtime.start()
    caminho = agente.clipes.path_for("00000000-0000-4000-8000-000000000000")
    caminho.write_bytes(b"mp4")

    agente.runtime._ao_terminar_clipe(
        ClipResult(
            event_id="00000000-0000-4000-8000-000000000000",
            camera_id="cam1",
            status=ClipStatus.OK,
            triggered_at=1.0,
            path=caminho,
        )
    )

    assert not caminho.exists()
    assert agente.store.stats(now_ms=0).depth == 0


def test_teto_de_disco_marca_o_clipe_como_perdido_na_hora(agente, sessao_longa):
    """Sem o aviso do `ClipStore`, o sender só descobriria o despejo na hora do upload,
    depois de gastar tentativas com um arquivo que não existe (§3.6)."""
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])
    agente.relogio.agora = 10.0
    event_id = agente.runtime.trigger("cam1", at=8.0)
    agente.espera_clipe()

    agente.runtime._ao_despejar_clipe(event_id)

    assert agente.store.get(event_id).clip_state is ClipState.GIVEN_UP


# --- ciclo de vida e saúde --------------------------------------------------


def test_stop_para_supervisores_recorder_e_sender(agente):
    agente.runtime.start()
    vivas_antes = {t.name for t in threading.enumerate()}
    assert any(nome.startswith("outbox-sender") for nome in vivas_antes)

    agente.runtime.stop(timeout=PRAZO_S)

    assert all(sup.parado for sup in agente.supervisores.values())
    vivas_depois = {t.name for t in threading.enumerate()}
    assert not any(nome.startswith("outbox-sender") for nome in vivas_depois)
    assert not any(nome.startswith("clip-recorder") for nome in vivas_depois)


def test_health_continua_respondendo_depois_do_stop(agente, sessao_longa):
    """O resumo de encerramento e a depuração de um agente que acabou de cair leem
    daqui. Esquecer os supervisores no `stop()` faria toda parada limpa parecer uma
    execução que nunca ingeriu nada — e a CLI sairia com código de erro."""
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])

    agente.runtime.stop(timeout=PRAZO_S)
    saude = agente.runtime.health()

    assert [c.camera_id for c in saude.cameras] == ["cam1"]
    assert saude.cameras[0].frames > 0
    assert saude.buffers[0].fragments == 20


def test_subir_duas_vezes_e_recusado(agente):
    agente.runtime.start()

    with pytest.raises(RuntimeError, match="já está rodando"):
        agente.runtime.start()


def test_health_reune_cameras_buffers_recorder_e_fila(agente, sessao_longa):
    """São os campos do heartbeat do §5.3, coletados antes de existir heartbeat: nada
    aqui espera o `POST` para começar a ser medido.

    Com o link caído — que é justamente quando alguém vai querer olhar a saúde — a
    fila fica parada e observável, em vez de correr com o sender.
    """
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])
    agente.relogio.agora = 10.0
    agente.runtime.trigger("cam1", at=8.0)
    agente.espera_clipe()

    saude = agente.runtime.health()

    assert [c.camera_id for c in saude.cameras] == ["cam1"]
    assert saude.buffers[0].fragments == 20
    assert saude.recorder.requested == 1
    assert saude.outbox.depth == 1
    assert saude.clip_disk_bytes > 0
    assert saude.enqueue_failures == 0


def test_fila_local_indisponivel_conta_e_registra(agente, sessao_longa, caplog):
    """Redis local fora do ar é a fronteira de durabilidade do ADR-004: o evento se
    perde. O que não pode acontecer é ele se perder em silêncio — sem contador no
    heartbeat, uma loja que parou de alertar parece uma loja sem ocorrências."""

    class FilaQuebrada(MemoryOutbox):
        def enqueue(self, item):
            raise ConnectionError("Redis local fora do ar")

    agente.store = FilaQuebrada(OPCOES_FILA)
    agente.runtime._store = agente.store
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])
    agente.relogio.agora = 10.0

    with caplog.at_level("ERROR"):
        event_id = agente.runtime.trigger("cam1", at=8.0)
        agente.espera_clipe()

    assert agente.runtime.health().enqueue_failures == 1
    assert event_id in caplog.text
    assert not agente.clipes.path_for(event_id).exists(), "clipe sem evento não fica no disco"


def test_o_agente_drena_a_fila_sozinho(tmp_path, sessao_longa):
    """A prova de que a fiação está completa: sem ninguém chamar `tick()`, o evento sai
    da fila, o clipe sobe e o disco fica vazio (NFR-3)."""
    agente = Agente(tmp_path, cliente=ClienteMudo())
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])
    agente.relogio.agora = 10.0
    event_id = agente.runtime.trigger("cam1", at=8.0)
    agente.espera_clipe()

    try:
        assert _ate(lambda: agente.store.get(event_id) is None), "o evento não drenou sozinho"
        assert not agente.clipes.path_for(event_id).exists()
        assert agente.cliente.posts[0]["event_id"] == event_id
        assert agente.cliente.uploads == [f"{event_id}.mp4"]
    finally:
        agente.runtime.stop(timeout=PRAZO_S)


def _ate(condicao, prazo_s: float = PRAZO_S) -> bool:
    """Polling com prazo: a thread do sender é real aqui, e `sleep` fixo seria
    instável em CI (regra 6 do CLAUDE.md)."""
    import time

    limite = time.monotonic() + prazo_s
    while time.monotonic() < limite:
        if condicao():
            return True
        time.sleep(0.01)
    return condicao()


def test_gatilho_nao_espera_o_clipe(agente, sessao_longa):
    """`trigger()` é chamado pela thread despachante do ffmpeg. Segurá-la pelos 10 s do
    pós-roll encheria a fila de fragmentos e travaria a leitura do RTSP de todas as
    câmeras daquele processo — a armadilha que o §3.5 já pagou."""
    init, fragmentos = sessao_longa
    agente.runtime.start()
    agente.alimenta("cam1", init, fragmentos[:20])
    agente.relogio.agora = 1.0

    voltou = threading.Event()

    def dispara() -> None:
        agente.runtime.trigger("cam1", at=1.0)
        voltou.set()

    threading.Thread(target=dispara, daemon=True).start()

    assert voltou.wait(1.0), "o gatilho bloqueou esperando o pós-roll"


# --- estágio 2: detecção ----------------------------------------------------


def test_frame_chega_ao_detector_carimbado_com_a_camera_certa(tmp_path):
    """O `on_frame` da ingestão entrega só o `Frame`, que não sabe de onde veio. Se o
    `camera_id` não for amarrado na montagem do supervisor, o §5.3 perde a separação
    por câmera — e é ela que revela **qual** câmera está saturando o box (R-4)."""
    detector = DetectorFalso((pessoa(),))
    montado = Agente(tmp_path, cameras=("cam1", "cam2"), detector=detector)
    montado.runtime.start()
    try:
        montado.alimenta_frame("cam2", quadro(7))
        assert detector.chamou.wait(PRAZO_S)
        assert _ate(lambda: montado.runtime.health().detector.frames_inferred == 1)

        por_camera = {
            camera.camera_id: camera.frames_inferred
            for camera in montado.runtime.health().detector.cameras
        }
        assert por_camera == {"cam1": 0, "cam2": 1}
    finally:
        montado.runtime.stop(timeout=PRAZO_S)


def test_camera_inelegivel_ingere_mas_nao_gasta_inferencia(tmp_path):
    """O R-3 prevê câmeras que a loja já tem e que não servem para IA — ângulo, altura,
    contraluz na porta. Elas continuam alimentando o buffer circular e respondendo a
    gatilho manual: o que some é o custo de GPU, não a câmera."""
    detector = DetectorFalso()
    montado = Agente(tmp_path, cameras=("cam1", "cam2"), detector=detector, inelegiveis=("cam2",))
    montado.runtime.start()
    try:
        montado.alimenta_frame("cam2", quadro(1))
        montado.alimenta_frame("cam1", quadro(1))
        assert detector.chamou.wait(PRAZO_S)
        assert _ate(lambda: montado.runtime.health().detector.frames_inferred == 1)

        saude = montado.runtime.health()
        assert [camera.camera_id for camera in saude.detector.cameras] == ["cam1"]
        assert saude.frames_ingested == 2
    finally:
        montado.runtime.stop(timeout=PRAZO_S)


def test_health_expoe_o_modelo_e_a_fila_do_estagio_2(tmp_path):
    """Os campos que o §5.3 pede já reunidos no mesmo lugar que o resto — é o que vai
    virar corpo de heartbeat quando o transporte existir."""
    montado = Agente(tmp_path, detector=DetectorFalso())
    montado.runtime.start()
    try:
        saude = montado.runtime.health().detector
        assert saude.enabled is True
        assert saude.info is not None and saude.info.provider == "CPUExecutionProvider"
        assert saude.queue_size == DetectionOptions().queue_size
    finally:
        montado.runtime.stop(timeout=PRAZO_S)


def test_sem_modelo_o_agente_sobe_e_continua_gravando_clipe(tmp_path, sessao_longa):
    """O §3.9 exige o mesmo artefato numa máquina sem GPU e no box da loja. Sem modelo
    o estágio 2 fica desligado, e o caminho gatilho → clipe → fila continua inteiro."""
    init, fragmentos = sessao_longa
    montado = Agente(tmp_path)  # sem detector: cai no NullDetector
    montado.runtime.start()
    try:
        montado.alimenta("cam1", init, fragmentos[:20])
        montado.alimenta_frame("cam1", quadro(1))
        montado.relogio.agora = 1.0
        montado.runtime.trigger("cam1", at=1.0)

        resultado = montado.espera_clipe()
        assert resultado.status is ClipStatus.OK
        assert montado.runtime.health().detector.enabled is False
    finally:
        montado.runtime.stop(timeout=PRAZO_S)


def test_detector_falho_nao_derruba_o_caminho_do_clipe(tmp_path, sessao_longa):
    """Regra geral do §5.4 aplicada ao estágio 2: nada no caminho da detecção pode
    parar o que já funciona. Uma placa que sumiu não pode levar junto o alerta."""
    init, fragmentos = sessao_longa
    montado = Agente(tmp_path, detector=DetectorQueFalha(falhas=99))
    montado.runtime.start()
    try:
        montado.alimenta("cam1", init, fragmentos[:20])
        montado.alimenta_frame("cam1", quadro(1))
        assert _ate(lambda: montado.runtime.health().detector.errors == 1)

        montado.relogio.agora = 1.0
        montado.runtime.trigger("cam1", at=1.0)
        assert montado.espera_clipe().status is ClipStatus.OK
    finally:
        montado.runtime.stop(timeout=PRAZO_S)
