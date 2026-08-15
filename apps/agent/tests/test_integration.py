"""Ponta a ponta contra RTSP de verdade. Exige `pnpm rtsp:up`.

    uv run pytest -m rtsp

Estes testes são a única forma de verificar as partes que só existem em execução:
que os dois pipes fluem ao mesmo tempo sem travar um ao outro, que a amostragem
entrega a taxa pedida, e que os fragmentos formam vídeo reproduzível.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

import pytest

from lince_agent.config import CameraConfig, DecodeOptions, SupervisionOptions
from lince_agent.ffmpeg.fmp4 import Fragment, InitSegment
from lince_agent.ffmpeg.probe import probe_stream
from lince_agent.ffmpeg.process import FfmpegIngest, IngestCallbacks
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.ingest.state import CameraStatus
from lince_agent.ingest.supervisor import CameraSupervisor

pytestmark = pytest.mark.rtsp

JANELA_S = 10.0
"""Precisa cobrir vários GOPs: com -g 30 a 15 fps, um keyframe a cada 2 s."""


class Coletor:
    def __init__(self) -> None:
        self.frames: list[Frame] = []
        self.fragments: list[Fragment] = []
        self.init: InitSegment | None = None
        self._lock = threading.Lock()

    def callbacks(self) -> IngestCallbacks:
        return IngestCallbacks(
            on_frame=self._on_frame,
            on_init_segment=self._on_init,
            on_fragment=self._on_fragment,
        )

    def _on_frame(self, frame: Frame) -> None:
        with self._lock:
            self.frames.append(frame)

    def _on_init(self, item: InitSegment) -> None:
        with self._lock:
            self.init = item

    def _on_fragment(self, fragment: Fragment) -> None:
        with self._lock:
            self.fragments.append(fragment)


@pytest.fixture
def coletado(rtsp_url: str) -> Coletor:
    coletor = Coletor()
    decode = DecodeOptions(sample_fps=3.0, width=640, height=480)
    with FfmpegIngest(rtsp_url, decode, coletor.callbacks()) as ingest:
        time.sleep(JANELA_S)
        assert ingest.is_running(), "o ffmpeg morreu durante a coleta"
    return coletor


def test_probe_ve_o_substream(rtsp_url: str):
    """A taxa conferida é a **medida** (`avg_frame_rate`), não a nominal.

    `r_frame_rate` não é a taxa do stream: é o menor framerate com que o ffprobe
    consegue representar todos os timestamps que viu. Basta um segundo leitor no mesmo
    caminho do MediaMTX para o jitter de RTP fazê-lo inferir 30 numa câmera de 15 —
    medido, com `avg_frame_rate` continuando correto em 15. Este teste já afirmava o
    contrário e passava por sorte, enquanto só uma câmera sintética estava no ar.

    Nada em produção depende disso: `StreamInfo.matches` só olha resolução, e a
    divergência entre as duas taxas é informação de log (§3.1), não decisão.
    """
    info = probe_stream(rtsp_url, DecodeOptions())
    assert info.codec == "h264"
    assert (info.width, info.height) == (640, 480)
    assert info.avg_frame_rate == pytest.approx(15.0, abs=1.0)
    assert info.r_frame_rate >= info.avg_frame_rate - 1.0


def test_frames_chegam_na_taxa_amostrada(coletado: Coletor):
    """A câmera entrega 15 fps; o filtro `fps=3` entrega 3. O decode continua em
    taxa cheia — o que a amostragem economiza é inferência e banda de pipe.

    A taxa é medida **entre os frames coletados**, e não dividindo a contagem pela
    janela de parede. Os dois primeiros segundos de uma ingestão não são regime
    permanente: o ffmpeg passa o `-analyzeduration` analisando e depois solta uma
    rajada, e enquanto a thread despachante não assume, a `DropOldestQueue` descarta o
    mais antigo — por desenho, porque contrapressão ali travaria o ffmpeg inteiro
    (§3.1). Contar frames numa janela fixa transformava essa perda de subida, mais o
    arredondamento das bordas, num teste que passava conforme a carga da máquina.
    """
    frames = coletado.frames
    assert len(frames) >= 10, "quase nada chegou; o problema não é cadência"

    intervalo = frames[-1].received_at - frames[0].received_at
    taxa = (len(frames) - 1) / intervalo
    assert 2.0 <= taxa <= 4.5, f"amostragem entregou {taxa:.2f} fps, esperava ~3"


def test_todo_frame_tem_o_tamanho_do_contrato(coletado: Coletor):
    """`rawvideo` não tem framing: um único byte a mais ou a menos embaralharia
    todos os frames seguintes, em silêncio."""
    assert coletado.frames
    for frame in coletado.frames:
        assert len(frame.data) == 640 * 480 * 3


def test_frame_vira_array_no_formato_do_detector(coletado: Coletor):
    array = coletado.frames[0].as_array()
    assert array.shape == (480, 640, 3)
    assert array.dtype.name == "uint8"


def test_sequencia_de_frames_nao_tem_buraco(coletado: Coletor):
    """Descarte na fila apareceria como salto na sequência. Com um consumidor que
    só guarda numa lista, não pode haver nenhum."""
    sequencias = [frame.sequence for frame in coletado.frames]
    assert sequencias == list(range(1, len(sequencias) + 1))


def test_os_dois_pipes_fluem_ao_mesmo_tempo(coletado: Coletor):
    """A verificação central do desenho: um processo, duas saídas, nenhuma das
    duas travando a outra. Se o pipe de fragmentos não fosse drenado, o de frames
    pararia junto — e vice-versa."""
    assert coletado.frames
    assert coletado.fragments
    assert coletado.init is not None


def test_fragmentos_chegam_a_cada_gop(coletado: Coletor):
    """cam1 usa -g 30 a 15 fps: um keyframe, e portanto um fragmento, a cada 2 s."""
    assert len(coletado.fragments) >= JANELA_S / 2 - 2


def test_buffer_e_ordens_de_grandeza_menor_que_os_frames(coletado: Coletor):
    """A conta que motivou o desenho, medida em vez de estimada."""
    bytes_fragmentos = sum(len(fragment) for fragment in coletado.fragments)
    bytes_frames_equivalentes = 640 * 480 * 3 * 15 * JANELA_S
    assert bytes_frames_equivalentes / bytes_fragmentos > 50


def test_init_mais_fragmentos_formam_video_reproduzivel(coletado: Coletor, tmp_path):
    """A prova que fecha o estágio 1. Se isto passa, o corte `-c copy` do estágio 5
    tem do que se alimentar."""
    assert coletado.init is not None
    clipe = tmp_path / "clipe.mp4"
    clipe.write_bytes(coletado.init.data + b"".join(f.data for f in coletado.fragments))

    result = subprocess.run(  # noqa: S603
        [
            *("ffprobe", "-hide_banner", "-loglevel", "error"),
            *("-select_streams", "v:0"),
            *("-show_entries", "format=duration:stream=codec_name,width,height"),
            *("-of", "csv=p=0"),
            str(clipe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    saida = result.stdout.strip()
    assert "h264" in saida
    assert "640,480" in saida


def test_tempos_dos_fragmentos_sao_continuos(coletado: Coletor):
    """Sem buraco no timeline: é o que o estágio 5 vai assumir para selecionar a
    janela de pré-roll e pós-roll a partir do t0 do gatilho."""
    inicios = [fragment.start_seconds for fragment in coletado.fragments]
    intervalos = [b - a for a, b in zip(inicios, inicios[1:], strict=False)]
    assert intervalos
    for intervalo in intervalos:
        assert 0 < intervalo < 5.0, f"buraco de {intervalo:.1f}s entre fragmentos"


def test_supervisor_chega_em_ok_e_encerra_limpo(rtsp_url: str):
    supervisor = CameraSupervisor(
        CameraConfig(
            camera_id="cam-teste",
            url=rtsp_url,
            decode=DecodeOptions(),
            supervision=SupervisionOptions(stall_timeout_s=15.0),
        )
    )
    supervisor.start()
    try:
        prazo = time.monotonic() + 20.0
        while time.monotonic() < prazo:
            if supervisor.health().status is CameraStatus.OK:
                break
            time.sleep(0.25)
        saude = supervisor.health()
        assert saude.status is CameraStatus.OK
        assert saude.consecutive_failures == 0
        assert saude.frames > 0
        # Câmera que subiu de primeira não pode reportar restart: é a métrica de
        # crash loop do §5.3, e ruído fixo nela a torna inútil.
        assert saude.restarts == 0
    finally:
        supervisor.stop()
    assert supervisor.health().status is CameraStatus.STOPPED


def test_camera_inexistente_vira_reconnecting_e_depois_offline():
    """Câmera offline mata o processo ffmpeg; o `-reconnect` do ffmpeg não existe
    para RTSP, então quem tem que reagir é o supervisor."""
    supervisor = CameraSupervisor(
        CameraConfig(
            camera_id="cam-fantasma",
            url="rtsp://127.0.0.1:1/inexistente",
            decode=DecodeOptions(socket_timeout_s=1.0, analyze_duration_s=0.5),
            supervision=SupervisionOptions(
                backoff_base_s=0.1,
                backoff_cap_s=0.3,
                backoff_jitter_s=0.0,
                offline_after_failures=3,
                stall_timeout_s=3.0,
            ),
        )
    )
    supervisor.start()
    try:
        prazo = time.monotonic() + 30.0
        while time.monotonic() < prazo:
            if supervisor.health().status is CameraStatus.OFFLINE:
                break
            time.sleep(0.25)
        saude = supervisor.health()
        assert saude.status is CameraStatus.OFFLINE
        assert saude.consecutive_failures >= 3
        assert saude.restarts >= 2, "o supervisor precisa continuar tentando"
    finally:
        supervisor.stop()


# --- estágio 2: detecção contra vídeo com pessoas ---------------------------


@pytest.fixture
def camera_com_pessoas() -> str:
    """A cam3 do compose: um vídeo com gente andando, em loop.

    As cam1 e cam2 são `testsrc2` — padrão de barras. Um teste de detecção contra
    barras de cor concordaria com qualquer coisa, inclusive com um detector quebrado.
    """
    url = os.environ.get("RTSP_PESSOAS_URL", "rtsp://localhost:8554/cam3")
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
        pytest.skip(f"sem cam3 em {url} — rode `bash scripts/modelo.sh && pnpm rtsp:up`")
    return url


@pytest.mark.modelo
def test_pessoas_de_verdade_viram_caixas_dentro_do_frame(camera_com_pessoas: str, model_path):
    """O caminho inteiro do §3.2 com peças reais: RTSP → ffmpeg → letterbox → ONNX →
    NMS → caixas no espaço do frame da câmera.

    Duas asserções, e a segunda é a que importa. Que apareçam pessoas prova que o
    modelo roda. Que as caixas caiam **dentro** de 640x480 prova que o letterbox foi
    desfeito: uma caixa em y=520 estaria na faixa cinza de preenchimento, e o §3.4
    testaria um ponto que não existe no frame contra um polígono desenhado sobre ele.
    """
    from lince_agent.config import DetectionOptions
    from lince_agent.detect.onnx import OnnxDetector
    from lince_agent.detect.worker import DetectorWorker

    decode = DecodeOptions(sample_fps=3.0, width=640, height=480)
    opcoes = DetectionOptions(
        enabled=True, model_path=model_path, providers=("CPUExecutionProvider",), queue_size=4
    )
    detector = OnnxDetector(model_path, opcoes)
    resultados = []
    achou = threading.Event()

    def guarda(resultado) -> None:
        resultados.append(resultado)
        if resultado.detections:
            achou.set()

    worker = DetectorWorker(detector, options=opcoes, on_result=guarda)
    ingest = FfmpegIngest(
        camera_com_pessoas,
        decode,
        IngestCallbacks(on_frame=lambda frame: worker.submit("cam3", frame)),
    )

    worker.start()
    ingest.start()
    try:
        assert achou.wait(JANELA_S * 3), (
            "nenhuma pessoa detectada em 30 s de vídeo com pessoas — "
            f"{len(resultados)} frames inferidos"
        )
    finally:
        ingest.stop()
        worker.stop(timeout=JANELA_S)

    caixas = [caixa for resultado in resultados for caixa in resultado.detections]
    assert all(caixa.class_id == 0 for caixa in caixas), "só pessoas passam pelo filtro de classe"
    assert all(0 <= caixa.x1 < caixa.x2 <= decode.width for caixa in caixas)
    assert all(0 <= caixa.y1 < caixa.y2 <= decode.height for caixa in caixas), (
        "caixa fora da moldura: o letterbox não foi desfeito"
    )

    stats = worker.stats()
    assert stats.errors == 0
    assert stats.info is not None and stats.info.model_version.startswith("yolox")


@pytest.mark.modelo
def test_a_mesma_pessoa_mantem_o_id_em_video_real(camera_com_pessoas: str, model_path):
    """O estágio 3 com peças reais: RTSP → ffmpeg → ONNX → ByteTrack.

    Os testes de `test_track_bytetrack.py` encenam cenários com detecções fabricadas, o
    que os torna determinísticos e é o certo para verificar a lógica. O que eles não
    conseguem provar é que o tracker sobrevive à **detecção real**, que é intermitente:
    o modelo perde a pessoa por um frame, devolve a caixa alguns pixels deslocada, e às
    vezes entrega duas caixas para a mesma pessoa.

    O critério é deliberadamente modesto — um track que aparece em três frames — por dois
    motivos. Sem *ground truth* não dá para afirmar mais, que é exatamente o que a
    pendência §10.7 espera de vídeo de loja. E numa máquina sem GPU a inferência entrega
    pouco mais de 1 fps (§10.10), então três frames já são quase três segundos de vídeo:
    exigir mais mediria a velocidade da CPU, não a qualidade do tracking.

    A verificação de verdade deste estágio é visual, com `--dump-tracks`: cada pessoa
    mantém uma cor enquanto atravessa o quadro, e cor trocando no meio é o R-2 na tela.
    """
    from lince_agent.config import DetectionOptions, TrackingOptions
    from lince_agent.detect.onnx import OnnxDetector
    from lince_agent.detect.worker import DetectorWorker
    from lince_agent.track.pool import TrackerPool

    decode = DecodeOptions(sample_fps=3.0, width=640, height=480)
    opcoes = DetectionOptions(
        enabled=True, model_path=model_path, providers=("CPUExecutionProvider",), queue_size=4
    )
    pool = TrackerPool(TrackingOptions())
    resultados = []
    suficiente = threading.Event()

    def rastreia(deteccao) -> None:
        resultado = pool.update(deteccao)
        resultados.append(resultado)
        contagem: dict[int, int] = {}
        for anterior in resultados:
            for track in anterior.tracks:
                contagem[track.track_id] = contagem.get(track.track_id, 0) + 1
        if any(vezes >= 3 for vezes in contagem.values()):
            suficiente.set()

    worker = DetectorWorker(OnnxDetector(model_path, opcoes), options=opcoes, on_result=rastreia)
    ingest = FfmpegIngest(
        camera_com_pessoas,
        decode,
        IngestCallbacks(on_frame=lambda frame: worker.submit("cam3", frame)),
    )

    worker.start()
    ingest.start()
    try:
        assert suficiente.wait(JANELA_S * 4), (
            "nenhum track apareceu em 3 frames ao longo de 40 s de vídeo com pessoas — "
            f"{len(resultados)} frames rastreados"
        )
    finally:
        ingest.stop()
        worker.stop(timeout=JANELA_S)

    (camera,) = pool.stats().cameras
    assert camera.criados > 0
    assert camera.resets == 0, "não houve reconexão; um reset aqui é bug de sequence"

    tracks = [track for resultado in resultados for track in resultado.tracks]
    assert all(t.x1 < t.x2 and t.y1 < t.y2 for t in tracks), "caixa degenerada"

    # A caixa de um track **não** é recortada na moldura, ao contrário da caixa de uma
    # detecção: ela é estimativa do Kalman, e quem está saindo pela porta tem mesmo os
    # pés estimados um pouco além da borda. Recortar moveria a `base_central`, que é
    # justamente o ponto que o §3.4 vai testar contra a linha de saída — o recorte
    # empurraria toda travessia para a beirada do quadro.
    #
    # O que se garante é que a extrapolação não delira: um track perdido é previsto por
    # no máximo `max_perdido_s`, o que a 165 px/s dá algumas centenas de pixels.
    folga = decode.width
    assert all(-folga <= t.x1 and t.x2 <= decode.width + folga for t in tracks)
    assert all(-folga <= t.y1 and t.y2 <= decode.height + folga for t in tracks)
