"""CLI de desenvolvimento: argumentos e formatação da saída."""

from __future__ import annotations

import os
import signal
import threading
import time

import pytest

from lince_agent import __main__ as cli
from lince_agent.config import HwAccel, OutboxOptions, PixelFormat
from lince_agent.ffmpeg.fmp4 import Fragment, InitSegment
from lince_agent.ffmpeg.probe import ProbeError, StreamInfo
from lince_agent.ingest.state import CameraHealth, CameraStatus
from lince_agent.outbox.state import OutboxStats, SenderStats
from lince_agent.outbox.store import MemoryOutbox
from lince_agent.runtime import AgentHealth


def test_padroes_batem_com_a_arquitetura():
    """3 fps e 640x480 são o que o §3.1 e o §3.2 especificam; mudar aqui em
    silêncio mudaria o dimensionamento inteiro."""
    args = cli.build_parser().parse_args(["--camera", "rtsp://x/y"])
    assert args.fps == 3.0
    assert (args.width, args.height) == (640, 480)
    assert args.pix_fmt == PixelFormat.BGR24.value
    assert args.hwaccel == HwAccel.NONE.value


def test_camera_e_obrigatoria():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_pix_fmt_invalido_e_recusado():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--camera", "rtsp://x", "--pix-fmt", "rgb999"])


def saude(**kwargs) -> CameraHealth:
    base = {
        "camera_id": "cam1",
        "status": CameraStatus.OK,
        "consecutive_failures": 0,
        "restarts": 0,
        "sampled_fps": 3.0,
        "frames": 42,
        "frames_dropped": 0,
        "fragments": 7,
        "fragments_dropped": 0,
        "fragment_bytes": 2048,
        "last_frame_at": None,
    }
    return CameraHealth(**(base | kwargs))


def test_linha_de_status_traz_o_essencial():
    linha = cli._format_health(saude(), 921_600)
    for pedaço in ("cam1", "ok", "42", "3.00 fps", "921600"):
        assert pedaço in linha


def test_camera_sem_frame_nao_mente_sobre_o_ultimo():
    assert "nunca" in cli._format_health(saude(frames=0, last_frame_at=None), 921_600)


def test_probe_only_nao_sobe_o_pipeline(monkeypatch):
    subiu = False

    class SupervisorFalso:
        def __init__(self, *a, **k) -> None:
            nonlocal subiu
            subiu = True

    monkeypatch.setattr(cli, "CameraSupervisor", SupervisorFalso)
    monkeypatch.setattr(
        cli,
        "probe_stream",
        lambda *a, **k: StreamInfo("h264", 640, 480, 15.0, 15.0),
    )
    assert cli.main(["--camera", "rtsp://x/y", "--probe-only"]) == 0
    assert not subiu


def test_camera_inalcancavel_sai_com_erro(monkeypatch):
    def falha(*a, **k):
        raise ProbeError("Connection refused")

    monkeypatch.setattr(cli, "probe_stream", falha)
    assert cli.main(["--camera", "rtsp://x/y"]) == 1


def test_fila_em_ram_avisa_que_nao_e_duravel(caplog):
    """`--outbox memory` é andaime. Sem o aviso alto, é fácil rodar meia hora achando
    que a fila é durável e descobrir no primeiro restart que não era (ADR-004)."""
    args = cli.build_parser().parse_args(["--camera", "rtsp://x", "--outbox", "memory"])

    with caplog.at_level("WARNING"):
        fila = cli._monta_fila(args, OutboxOptions())

    assert isinstance(fila, MemoryOutbox)
    assert "restart" in caplog.text


def test_primeiro_gatilho_respeita_after_e_every():
    def quando(argv: list[str]) -> float | None:
        return cli._primeiro_gatilho(cli.build_parser().parse_args(["--camera", "rtsp://x", *argv]))

    assert quando([]) is None
    assert quando(["--trigger-after", "5"]) is not None
    assert quando(["--trigger-every", "20"]) is not None


class RuntimeFalso:
    def __init__(self) -> None:
        self.gatilhos: list[str] = []
        self.parado = False

    camera_ids = ("cam1", "cam2")

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 30.0) -> None:
        self.parado = True

    def trigger(self, camera_id: str) -> str:
        self.gatilhos.append(camera_id)
        return f"evento-{len(self.gatilhos)}"

    def health(self) -> AgentHealth:
        return AgentHealth(cameras=(saude(),))


def test_gatilho_de_andaime_atinge_todas_as_cameras():
    runtime = RuntimeFalso()

    cli._dispara(runtime, "teste")

    assert runtime.gatilhos == ["cam1", "cam2"]


def test_trigger_after_dispara_uma_vez_e_para(monkeypatch, tmp_path):
    """O andaime tem que produzir evento sem o estágio 4 existir — é assim que o
    caminho gatilho → clipe → fila → nuvem é exercitado hoje."""
    runtime = RuntimeFalso()
    monkeypatch.setattr(cli, "AgentRuntime", lambda *a, **k: runtime)
    monkeypatch.setattr(cli, "probe_stream", lambda *a, **k: StreamInfo("h264", 640, 480, 15, 15))

    cli.main(
        [
            *("--camera", "rtsp://x/y", "--clips-dir", str(tmp_path)),
            *("--trigger-after", "0", "--duration", "0.1", "--dry-run"),
        ]
    )

    assert runtime.gatilhos == ["cam1", "cam2"], "um gatilho por câmera, uma vez só"
    assert runtime.parado, "o agente tem que ser desmontado mesmo saindo por --duration"


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="SIGUSR1 não existe nesta plataforma")
def test_sigusr1_dispara_um_evento(monkeypatch, tmp_path):
    """`kill -USR1` é como se testa uma câmera recém-instalada sem esperar alguém
    passar na frente dela.

    O handler só marca um `Event`; quem chama `trigger()` é o laço principal. Handler
    de sinal interrompe qualquer thread em qualquer ponto, inclusive segurando o lock
    que o próprio `trigger` iria querer.
    """
    runtime = RuntimeFalso()
    monkeypatch.setattr(cli, "AgentRuntime", lambda *a, **k: runtime)
    monkeypatch.setattr(cli, "probe_stream", lambda *a, **k: StreamInfo("h264", 640, 480, 15, 15))
    anterior = signal.getsignal(signal.SIGUSR1)

    def manda_sinal() -> None:
        # Espera o handler existir em vez de dormir um tempo fixo: o sinal enviado
        # antes da instalação mataria o processo de teste (SIGUSR1 default é terminar).
        limite = time.monotonic() + 5.0
        while time.monotonic() < limite:
            if signal.getsignal(signal.SIGUSR1) is not anterior:
                os.kill(os.getpid(), signal.SIGUSR1)
                return
            time.sleep(0.01)

    threading.Thread(target=manda_sinal, daemon=True).start()
    try:
        cli.main(
            [
                *("--camera", "rtsp://x/y", "--clips-dir", str(tmp_path)),
                *("--duration", "2", "--dry-run"),
            ]
        )
    finally:
        signal.signal(signal.SIGUSR1, anterior)

    assert runtime.gatilhos == ["cam1", "cam2"]


def test_dry_run_nao_fala_com_a_nuvem_e_libera_o_upload(tmp_path):
    """O `--dry-run` precisa devolver `clip_upload_url`, senão o caminho do clipe nunca
    é exercitado — que é justamente o que se quer ver rodando sem uma API do lado."""
    clipe = tmp_path / "e1.mp4"
    clipe.write_bytes(b"mp4")
    cliente = cli.ClienteSeco()

    aceite = cliente.post_event({"camera_id": "cam1", "clip": {"status": "ok"}}, event_id="e1")

    assert aceite.status == 202
    assert aceite.body["clip_upload_url"]
    assert cliente.put_clip(aceite.body["clip_upload_url"], clipe).status == 200
    assert cliente.patch_event("e1", {"clip": {"status": "ok"}}).status == 200


def test_linha_da_fila_mostra_o_que_o_heartbeat_vai_mostrar():
    """São os campos de fila do §5.3. Ver `mais antigo` crescendo na tela é como se
    percebe um link caído antes de o TTL começar a descartar."""
    saude_agente = AgentHealth(
        outbox=OutboxStats(depth=3, events=2, clips=1, ready=1, oldest_age_s=42.0),
        sender=SenderStats(sent=7, failures=2),
        clip_disk_bytes=2048,
    )

    linha = cli._format_outbox(saude_agente.outbox, saude_agente)

    for pedaço in ("3 pendentes", "2 ev + 1 clipes", "42.0", "enviados=7", "falhas=2"):
        assert pedaço in linha


def test_dumper_grava_init_e_fragmentos(tmp_path):
    """`cat init.mp4 frag_*.mp4` precisa dar um MP4 válido; a ordem dos nomes é
    parte disso, e por isso o contador é zero-padded."""
    dumper = cli._FragmentDumper(tmp_path)
    dumper.on_init_segment(InitSegment(data=b"init", timescale=90_000))
    for i in range(11):
        dumper.on_fragment(
            Fragment(
                data=f"frag{i}".encode(),
                base_media_decode_time=i * 90_000,
                timescale=90_000,
                received_at=0.0,
            )
        )

    assert (tmp_path / "init.mp4").read_bytes() == b"init"
    nomes = sorted(p.name for p in tmp_path.glob("frag_*.mp4"))
    assert nomes[0] == "frag_00001.mp4"
    assert nomes[-1] == "frag_00011.mp4", "ordenação alfabética tem que bater com a temporal"
