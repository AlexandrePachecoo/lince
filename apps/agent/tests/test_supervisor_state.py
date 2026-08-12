"""Máquina de estados do supervisor, com dublê no lugar do ffmpeg.

Estes testes existem porque as transições que mais importam são as mais difíceis de
provocar com hardware: câmera que conecta e nunca entrega frame, stream que congela
no meio, oito falhas seguidas. Com um dublê e um relógio falso, cada uma vira um
teste determinístico de milissegundos.
"""

from __future__ import annotations

import threading
import time

import pytest

from lince_agent.config import CameraConfig, DecodeOptions, SupervisionOptions
from lince_agent.ffmpeg.process import IngestStats
from lince_agent.ingest.state import CameraStatus
from lince_agent.ingest.supervisor import CameraSupervisor

RAPIDO = SupervisionOptions(
    backoff_base_s=0.01,
    backoff_cap_s=0.02,
    backoff_jitter_s=0.0,
    offline_after_failures=3,
    stall_timeout_s=5.0,
    stop_grace_s=0.05,
)


class IngestFalso:
    """Dublê de `IngestProcess`. O teste dirige frames e morte à mão."""

    def __init__(self, *, morre_apos: float | None = None) -> None:
        self.frames = 0
        self.started = False
        self.stopped = False
        self._vivo = True
        self._morre_apos = morre_apos
        self._inicio = 0.0
        self.started_at = 0.0
        self.last_frame_at: float | None = None

    # --- interface consumida pelo supervisor --------------------------------

    @property
    def command(self) -> list[str]:
        return ["ffmpeg", "-dublê"]

    @property
    def returncode(self) -> int | None:
        return None if self._vivo else 1

    def start(self) -> None:
        self.started = True
        self._inicio = time.monotonic()

    def stop(self, grace_s: float = 3.0) -> int | None:
        self.stopped = True
        self._vivo = False
        return 0

    def is_running(self) -> bool:
        if self._morre_apos is not None and time.monotonic() - self._inicio > self._morre_apos:
            self._vivo = False
        return self._vivo

    def stats(self) -> IngestStats:
        return IngestStats(
            frames=self.frames,
            last_frame_at=self.last_frame_at,
            started_at=self.started_at,
        )

    # --- controle do teste ---------------------------------------------------

    def entrega_frame(self, quando: float) -> None:
        self.frames += 1
        self.last_frame_at = quando

    def mata(self) -> None:
        self._vivo = False


class RelogioFalso:
    def __init__(self, inicio: float = 1000.0) -> None:
        self.agora = inicio

    def __call__(self) -> float:
        return self.agora

    def avanca(self, segundos: float) -> None:
        self.agora += segundos


def supervisor(dublês, *, opcoes=RAPIDO, clock=time.monotonic) -> CameraSupervisor:
    fila = list(dublês)
    return CameraSupervisor(
        CameraConfig(camera_id="cam", url="rtsp://x/y", decode=DecodeOptions(), supervision=opcoes),
        clock=clock,
        jitter=lambda: 0.0,
        ingest_factory=lambda: fila.pop(0) if fila else IngestFalso(),
    )


def espera(sup: CameraSupervisor, status: CameraStatus, prazo: float = 5.0) -> bool:
    limite = time.monotonic() + prazo
    while time.monotonic() < limite:
        if sup.health().status is status:
            return True
        time.sleep(0.005)
    return False


def test_primeiro_frame_e_o_que_declara_a_camera_saudavel():
    """Um ffmpeg que sobe, conecta e nunca entrega nada não é câmera saudável — daí
    a transição para `ok` não acontecer no `start`."""
    dublê = IngestFalso()
    relogio = RelogioFalso()
    dublê.started_at = relogio.agora
    sup = supervisor([dublê], clock=relogio)
    sup.start()
    try:
        assert espera(sup, CameraStatus.STARTING, prazo=2.0)
        time.sleep(0.3)
        assert sup.health().status is CameraStatus.STARTING, "virou ok sem nenhum frame"

        dublê.entrega_frame(relogio.agora)
        assert espera(sup, CameraStatus.OK)
    finally:
        sup.stop()


def test_reconnecting_antes_de_offline():
    """O §5.3 exige a distinção: reconectando se resolve sozinho, offline vale uma
    visita. Com `offline_after_failures=3`, as duas primeiras falhas são ruído."""
    sup = supervisor([IngestFalso(morre_apos=0.0) for _ in range(6)])
    sup.start()
    try:
        assert espera(sup, CameraStatus.RECONNECTING)
        assert espera(sup, CameraStatus.OFFLINE)
        assert sup.health().consecutive_failures >= 3
    finally:
        sup.stop()


def test_recuperacao_zera_o_contador_de_falhas():
    morre = IngestFalso(morre_apos=0.0)
    saudavel = IngestFalso()
    relogio = RelogioFalso()
    saudavel.started_at = relogio.agora
    sup = supervisor([morre, saudavel], clock=relogio)
    sup.start()
    try:
        assert espera(sup, CameraStatus.RECONNECTING)
        saudavel.entrega_frame(relogio.agora)
        assert espera(sup, CameraStatus.OK)
        assert sup.health().consecutive_failures == 0
    finally:
        sup.stop()


def test_watchdog_dispara_com_o_processo_vivo():
    """A falha que engana: o socket segue aberto, o processo segue rodando, o
    `-timeout` do demuxer não dispara e nenhum frame sai. Só a ausência de frame
    denuncia — e é por isso que o relógio de referência é `last_frame_at`."""
    dublê = IngestFalso()
    relogio = RelogioFalso()
    dublê.started_at = relogio.agora
    sup = supervisor([dublê, IngestFalso()], clock=relogio)
    sup.start()
    try:
        dublê.entrega_frame(relogio.agora)
        assert espera(sup, CameraStatus.OK)
        assert dublê.is_running(), "o dublê tem que continuar vivo, é esse o ponto"

        relogio.avanca(RAPIDO.stall_timeout_s + 1)
        assert espera(sup, CameraStatus.RECONNECTING)
        assert "travado" in (sup.health().last_error or "")
        assert dublê.stopped, "o supervisor precisa matar o processo congelado"
    finally:
        sup.stop()


def test_watchdog_tambem_limita_o_tempo_de_conexao():
    """Antes do primeiro frame o relógio de referência é a subida do processo, o
    que dá teto a um ffmpeg que fica pendurado tentando conectar."""
    dublê = IngestFalso()
    relogio = RelogioFalso()
    dublê.started_at = relogio.agora
    sup = supervisor([dublê, IngestFalso()], clock=relogio)
    sup.start()
    try:
        assert espera(sup, CameraStatus.STARTING, prazo=2.0)
        relogio.avanca(RAPIDO.stall_timeout_s + 1)
        assert espera(sup, CameraStatus.RECONNECTING)
    finally:
        sup.stop()


def test_camera_saudavel_nao_reporta_restart():
    """`restarts` é a métrica de crash loop do §5.3; ruído fixo nela a torna
    inútil."""
    dublê = IngestFalso()
    relogio = RelogioFalso()
    dublê.started_at = relogio.agora
    sup = supervisor([dublê], clock=relogio)
    sup.start()
    try:
        dublê.entrega_frame(relogio.agora)
        assert espera(sup, CameraStatus.OK)
        assert sup.health().restarts == 0
    finally:
        sup.stop()


def test_restarts_contam_as_retomadas():
    sup = supervisor([IngestFalso(morre_apos=0.0) for _ in range(6)])
    sup.start()
    try:
        assert espera(sup, CameraStatus.OFFLINE)
        assert sup.health().restarts >= 2
    finally:
        sup.stop()


def test_stop_durante_o_backoff_nao_espera_o_backoff():
    """Um `stop()` no meio de um backoff de 60 s não pode levar 60 s: é a diferença
    entre `Event.wait` e `sleep`, e o que separa um encerramento de container de um
    SIGKILL do Docker."""
    lento = SupervisionOptions(
        backoff_base_s=60.0, backoff_cap_s=60.0, backoff_jitter_s=0.0, stop_grace_s=0.05
    )
    sup = supervisor([IngestFalso(morre_apos=0.0) for _ in range(3)], opcoes=lento)
    sup.start()
    assert espera(sup, CameraStatus.RECONNECTING)

    inicio = time.monotonic()
    sup.stop(timeout=5.0)
    assert time.monotonic() - inicio < 2.0
    assert sup.health().status is CameraStatus.STOPPED


def test_processo_que_nao_executa_vira_falha_e_nao_excecao():
    """ffmpeg ausente do PATH derruba esta câmera, não o agente inteiro (§5.4)."""

    class NaoExecuta(IngestFalso):
        def start(self) -> None:
            raise OSError("ffmpeg não encontrado")

    sup = supervisor([NaoExecuta() for _ in range(6)])
    sup.start()
    try:
        assert espera(sup, CameraStatus.OFFLINE)
        assert "ffmpeg" in (sup.health().last_error or "")
    finally:
        sup.stop()


def test_start_duplicado_e_erro():
    sup = supervisor([IngestFalso()])
    sup.start()
    try:
        with pytest.raises(RuntimeError, match="já está rodando"):
            sup.start()
    finally:
        sup.stop()


def test_stop_encerra_a_thread_do_supervisor():
    sup = supervisor([IngestFalso()])
    sup.start()
    sup.stop()
    vivas = [t for t in threading.enumerate() if t.name.startswith("supervisor-")]
    assert not vivas, f"threads sobreviveram ao stop: {vivas}"


def test_health_antes_do_start():
    sup = supervisor([IngestFalso()])
    saude = sup.health()
    assert saude.status is CameraStatus.STOPPED
    assert saude.frames == 0
    assert saude.last_frame_at is None
