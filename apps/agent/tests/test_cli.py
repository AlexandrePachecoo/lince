"""CLI de desenvolvimento: argumentos e formatação da saída."""

from __future__ import annotations

import pytest

from lince_agent import __main__ as cli
from lince_agent.config import HwAccel, PixelFormat
from lince_agent.ffmpeg.fmp4 import Fragment, InitSegment
from lince_agent.ffmpeg.probe import ProbeError, StreamInfo
from lince_agent.ingest.state import CameraHealth, CameraStatus


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
