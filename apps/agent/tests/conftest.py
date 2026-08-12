from __future__ import annotations

import os
import shutil
import subprocess

import pytest


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("ffmpeg não encontrado no PATH")
    return binary


@pytest.fixture(scope="session")
def fmp4_stream() -> bytes:
    """Bytes fMP4 reais, produzidos com as mesmas flags da ingestão.

    Testar o parser contra bytes escritos à mão testaria a nossa leitura da
    especificação; testar contra a saída do ffmpeg testa o que vai chegar no pipe.
    `-g 10` em 50 frames dá 5 fragmentos.
    """
    result = subprocess.run(  # noqa: S603
        [
            *(_ffmpeg(), "-hide_banner", "-loglevel", "error"),
            *("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10"),
            *("-frames:v", "50", "-c:v", "libx264", "-preset", "ultrafast"),
            *("-g", "10", "-pix_fmt", "yuv420p", "-an"),
            *("-movflags", "+frag_keyframe+empty_moov+default_base_moof"),
            *("-f", "mp4", "pipe:1"),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert result.stdout, "ffmpeg não produziu bytes"
    return result.stdout


@pytest.fixture(scope="session")
def rtsp_url() -> str:
    url = os.environ.get("RTSP_TEST_URL", "rtsp://localhost:8554/cam1")
    probe = shutil.which("ffprobe")
    if probe is None:
        pytest.skip("ffprobe não encontrado no PATH")
    check = subprocess.run(  # noqa: S603
        [probe, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if check.returncode != 0:
        pytest.skip(f"sem servidor RTSP em {url} — rode `pnpm rtsp:up`")
    return url
