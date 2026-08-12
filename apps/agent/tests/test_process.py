"""Ciclo de vida do processo e das threads, com ffmpeg de verdade e sem rede.

A entrada é um arquivo em vez de RTSP — `build_ingest_command` já condiciona as
opções do demuxer RTSP ao esquema da URL, então o resto do caminho é idêntico:
mesmos dois pipes, mesmo parser, mesmas threads. O que se perde é a camada de rede;
o que se ganha é que estes testes rodam por padrão, sem `pnpm rtsp:up`.
"""

from __future__ import annotations

import subprocess
import threading
import time

import pytest

from lince_agent.config import DecodeOptions
from lince_agent.ffmpeg.fmp4 import Fragment, InitSegment
from lince_agent.ffmpeg.process import FfmpegIngest, IngestCallbacks
from lince_agent.ffmpeg.rawframe import Frame

DURACAO_S = 4
FPS_FONTE = 15
GOP = 15


@pytest.fixture(scope="session")
def video_file(tmp_path_factory) -> str:
    """Um arquivo 640x480 com keyframe a cada segundo, como uma câmera entregaria."""
    caminho = tmp_path_factory.mktemp("video") / "fonte.mp4"
    subprocess.run(  # noqa: S603
        [
            *("ffmpeg", "-hide_banner", "-loglevel", "error", "-y"),
            *("-f", "lavfi", "-i", f"testsrc2=size=640x480:rate={FPS_FONTE}"),
            *("-t", str(DURACAO_S), "-c:v", "libx264", "-preset", "ultrafast"),
            *("-tune", "zerolatency", "-g", str(GOP), "-pix_fmt", "yuv420p", "-an"),
            str(caminho),
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    return str(caminho)


class Coletor:
    def __init__(self, atraso_por_frame: float = 0.0) -> None:
        self.frames: list[Frame] = []
        self.fragments: list[Fragment] = []
        self.init: InitSegment | None = None
        self._atraso = atraso_por_frame
        self._lock = threading.Lock()

    def callbacks(self) -> IngestCallbacks:
        return IngestCallbacks(
            on_frame=self._on_frame,
            on_init_segment=self._on_init,
            on_fragment=self._on_fragment,
        )

    def _on_frame(self, frame: Frame) -> None:
        if self._atraso:
            time.sleep(self._atraso)
        with self._lock:
            self.frames.append(frame)

    def _on_init(self, item: InitSegment) -> None:
        with self._lock:
            self.init = item

    def _on_fragment(self, fragment: Fragment) -> None:
        with self._lock:
            self.fragments.append(fragment)


def roda_ate_o_fim(ingest: FfmpegIngest, prazo: float = 60.0) -> None:
    limite = time.monotonic() + prazo
    while ingest.is_running() and time.monotonic() < limite:
        time.sleep(0.05)
    assert not ingest.is_running(), "o ffmpeg não terminou de ler o arquivo"
    # O processo sair não significa que as threads já drenaram o que ficou nos
    # pipes; `stop` fecha tudo e espera por elas.
    ingest.stop()


def test_entrega_frames_e_fragmentos(video_file: str):
    coletor = Coletor()
    ingest = FfmpegIngest(video_file, DecodeOptions(sample_fps=3.0), coletor.callbacks())
    ingest.start()
    roda_ate_o_fim(ingest)

    assert coletor.frames
    assert coletor.fragments
    assert coletor.init is not None
    assert all(len(frame.data) == 640 * 480 * 3 for frame in coletor.frames)


def test_amostragem_reduz_a_contagem_de_frames(video_file: str):
    """3 fps sobre uma fonte de 15: um em cada cinco chega à detecção."""
    coletor = Coletor()
    ingest = FfmpegIngest(video_file, DecodeOptions(sample_fps=3.0), coletor.callbacks())
    ingest.start()
    roda_ate_o_fim(ingest)

    assert len(coletor.frames) == pytest.approx(3 * DURACAO_S, abs=2)


def test_consumidor_lento_perde_frames_mas_nao_trava_o_ffmpeg(video_file: str):
    """A propriedade central do estágio 1, medida em vez de assumida.

    O consumidor demora 100 ms por frame e a fila cabe 2. Se o descarte não
    existisse, a thread despachante seguraria a leitora, o pipe encheria e o ffmpeg
    bloquearia — inclusive a saída de fragmentos e a leitura da entrada. O que tem
    que acontecer é o oposto: frames são descartados e o processo termina inteiro.
    """
    coletor = Coletor(atraso_por_frame=0.1)
    ingest = FfmpegIngest(
        video_file,
        DecodeOptions(sample_fps=float(FPS_FONTE)),
        coletor.callbacks(),
        frame_queue_size=2,
    )
    ingest.start()
    roda_ate_o_fim(ingest)

    stats = ingest.stats()
    assert stats.frames == pytest.approx(FPS_FONTE * DURACAO_S, abs=3)
    assert stats.frames_dropped > 0, "sem descarte, este teste não provaria nada"
    assert len(coletor.frames) < stats.frames
    # E, apesar do consumidor lento, a outra saída não foi afetada:
    assert coletor.fragments
    assert ingest.returncode == 0


def test_fragmentos_reconstroem_video_reproduzivel(video_file: str, tmp_path):
    coletor = Coletor()
    ingest = FfmpegIngest(video_file, DecodeOptions(), coletor.callbacks())
    ingest.start()
    roda_ate_o_fim(ingest)

    assert coletor.init is not None
    clipe = tmp_path / "clipe.mp4"
    clipe.write_bytes(coletor.init.data + b"".join(f.data for f in coletor.fragments))

    result = subprocess.run(  # noqa: S603
        [
            *("ffprobe", "-hide_banner", "-loglevel", "error"),
            *("-select_streams", "v:0", "-count_frames"),
            *("-show_entries", "stream=codec_name,width,height,nb_read_frames"),
            *("-of", "csv=p=0"),
            str(clipe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    codec, largura, altura, frames = result.stdout.strip().split(",")
    assert (codec, largura, altura) == ("h264", "640", "480")
    assert int(frames) == pytest.approx(FPS_FONTE * DURACAO_S, abs=2)


def test_stats_contam_o_que_passou(video_file: str):
    ingest = FfmpegIngest(video_file, DecodeOptions(), IngestCallbacks())
    ingest.start()
    roda_ate_o_fim(ingest)

    stats = ingest.stats()
    assert stats.frames > 0
    assert stats.fragments > 0
    assert stats.fragment_bytes > 0
    assert stats.last_frame_at is not None


def test_comando_registra_o_fd_do_pipe_de_fragmentos(video_file: str):
    """O fd vem do `os.pipe()`, então o número varia; o que não pode variar é ele
    aparecer no argv e ser o mesmo do `pass_fds`."""
    ingest = FfmpegIngest(video_file, DecodeOptions())
    ingest.start()
    try:
        pipes = [arg for arg in ingest.command if arg.startswith("pipe:")]
        assert pipes[0] == "pipe:1"
        assert int(pipes[1].removeprefix("pipe:")) > 2
    finally:
        ingest.stop()


def test_start_duas_vezes_e_erro(video_file: str):
    ingest = FfmpegIngest(video_file, DecodeOptions())
    ingest.start()
    try:
        with pytest.raises(RuntimeError, match="já está rodando"):
            ingest.start()
    finally:
        ingest.stop()


def test_stop_no_meio_encerra_as_threads(video_file: str):
    ingest = FfmpegIngest(video_file, DecodeOptions())
    ingest.start()
    time.sleep(0.5)
    ingest.stop()

    assert not ingest.is_running()
    vivas = [t for t in threading.enumerate() if t.name.endswith(("-read", "-dispatch"))]
    assert not vivas, f"threads sobreviveram ao stop: {vivas}"


def test_stop_sem_start_nao_explode():
    assert FfmpegIngest("/inexistente.mp4", DecodeOptions()).stop() is None


def test_entrada_inexistente_sai_com_erro():
    """O processo morre; quem reage é o supervisor, não esta camada."""
    ingest = FfmpegIngest("/nao/existe/camera.mp4", DecodeOptions(), IngestCallbacks())
    ingest.start()
    roda_ate_o_fim(ingest, prazo=30.0)

    assert ingest.returncode != 0
    assert ingest.stats().frames == 0


def test_excecao_no_callback_nao_derruba_a_ingestao(video_file: str):
    """Um consumidor que explode não pode parar a thread que drena o pipe — seria
    exatamente o deadlock que o desenho todo existe para evitar."""
    explosoes = 0

    def on_frame(_: Frame) -> None:
        nonlocal explosoes
        explosoes += 1
        raise RuntimeError("consumidor com defeito")

    ingest = FfmpegIngest(video_file, DecodeOptions(), IngestCallbacks(on_frame=on_frame))
    ingest.start()
    roda_ate_o_fim(ingest)

    assert explosoes > 1, "a thread parou na primeira exceção"
    assert ingest.returncode == 0
