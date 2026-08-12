"""CLI de desenvolvimento do estágio 1.

Não é o ponto de entrada de produção — o agente de verdade vai puxar a lista de
câmeras da nuvem (§5.2). Isto existe para apontar o pipeline para uma câmera e ver
o que sai, que é como as pendências do §10 vão ser medidas.

    uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from lince_agent.config import CameraConfig, DecodeOptions, HwAccel, PixelFormat
from lince_agent.ffmpeg.capabilities import select_hwaccel
from lince_agent.ffmpeg.probe import ProbeError, probe_stream
from lince_agent.ffmpeg.process import IngestCallbacks
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.ingest.state import CameraHealth
from lince_agent.ingest.supervisor import CameraSupervisor

log = logging.getLogger("lince_agent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lince-agent",
        description="Ingestão RTSP do agente da borda (estágio 1 da arquitetura)",
    )
    parser.add_argument("--camera", required=True, help="URL RTSP da câmera")
    parser.add_argument("--camera-id", default="cam", help="identificador nos logs")
    parser.add_argument("--fps", type=float, default=3.0, help="taxa entregue à detecção")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument(
        "--pix-fmt",
        default=PixelFormat.BGR24.value,
        choices=[fmt.value for fmt in PixelFormat],
    )
    parser.add_argument(
        "--hwaccel",
        default=HwAccel.NONE.value,
        choices=[accel.value for accel in HwAccel],
        help="verificado em tempo de execução; cai para CPU se não funcionar",
    )
    parser.add_argument("--duration", type=float, help="segundos até parar sozinho")
    parser.add_argument("--stats", action="store_true", help="imprime saúde a cada segundo")
    parser.add_argument(
        "--dump-fragments",
        type=Path,
        metavar="DIR",
        help="grava init.mp4 e frag_NNNNN.mp4; concatenados formam um MP4 reproduzível",
    )
    parser.add_argument("--probe-only", action="store_true", help="só roda o ffprobe e sai")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _format_health(health: CameraHealth, frame_bytes: int) -> str:
    last = (
        "nunca"
        if health.last_frame_at is None
        else f"{time.monotonic() - health.last_frame_at:.1f}s atrás"
    )
    return (
        f"[{health.camera_id}] {health.status:<12} "
        f"frames={health.frames:<6} {health.sampled_fps:5.2f} fps "
        f"({frame_bytes} B/frame, {health.frames_dropped} descartados)  "
        f"fragmentos={health.fragments:<4} {health.fragment_bytes / 1024:8.1f} KiB "
        f"({health.fragments_dropped} descartados)  último frame {last}"
    )


class _FragmentDumper:
    """Grava init segment e fragmentos para a verificação de ponta a ponta.

    `cat init.mp4 frag_*.mp4 > clipe.mp4` tem que produzir um MP4 reproduzível. É a
    demonstração de que a unidade do buffer circular está correta e de que o corte
    `-c copy` do §3.5 vai ter do que se alimentar.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._count = 0

    def on_init_segment(self, item) -> None:  # noqa: ANN001 - InitSegment
        (self._directory / "init.mp4").write_bytes(item.data)
        log.info("init segment gravado: %d bytes, timescale %d", len(item.data), item.timescale)

    def on_fragment(self, fragment) -> None:  # noqa: ANN001 - Fragment
        self._count += 1
        name = self._directory / f"frag_{self._count:05d}.mp4"
        name.write_bytes(fragment.data)
        log.debug(
            "fragmento %d: %d bytes, início em %.3fs",
            self._count,
            len(fragment),
            fragment.start_seconds,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    decode = DecodeOptions(
        sample_fps=args.fps,
        width=args.width,
        height=args.height,
        pixel_format=PixelFormat(args.pix_fmt),
        hwaccel=select_hwaccel(HwAccel(args.hwaccel)),
    )

    try:
        info = probe_stream(args.camera, decode)
    except ProbeError as error:
        log.error("%s", error)
        return 1
    log.info(
        "câmera entrega %s %dx%d, %.2f fps (nominal %.2f)",
        info.codec,
        info.width,
        info.height,
        info.avg_frame_rate,
        info.r_frame_rate,
    )
    if args.probe_only:
        return 0

    frames_seen = 0

    def on_frame(frame: Frame) -> None:
        nonlocal frames_seen
        frames_seen += 1
        if args.verbose and frames_seen == 1:
            array = frame.as_array()
            log.debug("primeiro frame: shape=%s dtype=%s", array.shape, array.dtype)

    callbacks = IngestCallbacks(on_frame=on_frame)
    if args.dump_fragments is not None:
        dumper = _FragmentDumper(args.dump_fragments)
        callbacks = IngestCallbacks(
            on_frame=on_frame,
            on_init_segment=dumper.on_init_segment,
            on_fragment=dumper.on_fragment,
        )

    supervisor = CameraSupervisor(
        CameraConfig(camera_id=args.camera_id, url=args.camera, decode=decode), callbacks
    )

    finished = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: finished.set())
    signal.signal(signal.SIGTERM, lambda *_: finished.set())

    supervisor.start()
    deadline = time.monotonic() + args.duration if args.duration else None
    try:
        while not finished.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            if args.stats:
                print(_format_health(supervisor.health(), decode.frame_bytes), flush=True)  # noqa: T201
            finished.wait(1.0)
    finally:
        supervisor.stop()

    health = supervisor.health()
    print(_format_health(health, decode.frame_bytes), flush=True)  # noqa: T201
    return 0 if health.frames > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
