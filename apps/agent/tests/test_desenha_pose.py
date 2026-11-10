"""O vídeo com o esqueleto desenhado — o jeito de conferir a pose a olho.

`qualifica_camera.py` diz se o modelo estava *confiante* sobre o punho; só a
imagem diz se ele *acertou*. Por isso o desenho tem que mostrar exatamente o
que a régua usou, e nada a mais: uma junta abaixo do limiar desenhada como se
fosse boa faria o fundador confiar num pulso que a régua descartou.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from lince_agent.detect.state import Detection
from lince_agent.pose.onnx import Junta, PosePessoa
from lince_agent.pose.worker import CaixaPose
from lince_agent.track.state import Track, TrackingResult, TrackStatus

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
# Mesmo pulo dos outros testes de tools/: a imagem do agente não copia tools/.
if not (_TOOLS / "desenha_pose.py").is_file():
    pytest.skip(
        f"{_TOOLS} não está nesta árvore. A imagem do agente não copia tools/.",
        allow_module_level=True,
    )


def _carrega(nome: str):
    if nome in sys.modules:
        return sys.modules[nome]
    spec = importlib.util.spec_from_file_location(nome, _TOOLS / f"{nome}.py")
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


_carrega("qualifica_camera")
desenha = _carrega("desenha_pose")

PULSO_ESQ, PULSO_DIR = 9, 10
OMBRO_ESQ, COTOVELO_ESQ = 5, 7


def pose_com(**juntas: tuple[float, float, float]) -> PosePessoa:
    """Juntas por índice (`j9=(x, y, conf)`); as outras ficam fora do quadro, sem confiança."""
    valores = {int(nome[1:]): v for nome, v in juntas.items()}
    return PosePessoa(
        juntas=tuple(
            Junta(
                indice=i,
                x=valores.get(i, (-50.0, -50.0, 0.0))[0],
                y=valores.get(i, (-50.0, -50.0, 0.0))[1],
                confianca=valores.get(i, (-50.0, -50.0, 0.0))[2],
            )
            for i in range(17)
        )
    )


def track(track_id: int, *, status=TrackStatus.CONFIRMADO, x1=10.0, y1=10.0, x2=190.0, y2=190.0):
    return Track(
        track_id=track_id,
        camera_id="v",
        status=status,
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        score=0.9,
        hits=3,
        age_s=1.0,
        time_since_update_s=0.0,
        first_seen_at=0.0,
        last_seen_at=1.0,
    )


def amostra(pessoas, *tracks, instante=0.0, tamanho=(200, 200)):
    imagem = np.frombuffer(bytes(tamanho[0] * tamanho[1] * 3), dtype=np.uint8).reshape(
        tamanho[1], tamanho[0], 3
    )
    rastreio = TrackingResult(
        camera_id="v", sequence=0, received_at=instante, tracks=tuple(tracks), tracking_ms=0.0
    )
    return sys.modules["qualifica_camera"].Amostra(
        instante=instante, imagem=imagem, rastreio=rastreio, pessoas=tuple(pessoas)
    )


def caixa(track_id: int = 1) -> CaixaPose:
    return CaixaPose(track_id=track_id, x1=10.0, y1=10.0, x2=190.0, y2=190.0)


def _canal_dominante(imagem: np.ndarray, x: int, y: int) -> str:
    b, g, r = (int(v) for v in imagem[y, x])
    return {0: "azul", 1: "verde", 2: "vermelho"}[int(np.argmax([b, g, r]))]


# --- o que vai para a imagem ---------------------------------------------------


def test_pulso_visto_sai_verde_e_o_descartado_vermelho():
    """A cor é a decisão da régua: verde conta como punho, vermelho não conta.
    Se o vermelho sumisse, ninguém veria que a régua descartou aquele pulso."""
    pose = pose_com(j9=(50.0, 50.0, 0.9), j10=(150.0, 150.0, 0.1))
    saida = desenha.desenha(amostra([(caixa(), pose)], track(1)), limiar=0.3)
    assert _canal_dominante(saida, 50, 50 - desenha.RAIO_PULSO) == "verde"
    assert _canal_dominante(saida, 150, 150 - desenha.RAIO_PULSO) == "vermelho"


def test_osso_com_uma_ponta_fraca_nao_e_desenhado():
    """Um braço desenhado até um cotovelo de confiança 0,1 parece um braço
    medido. É chute do modelo, e a régua o ignora."""
    fraco = pose_com(
        **{f"j{OMBRO_ESQ}": (20.0, 100.0, 0.9), f"j{COTOVELO_ESQ}": (180.0, 100.0, 0.1)}
    )
    forte = pose_com(
        **{f"j{OMBRO_ESQ}": (20.0, 100.0, 0.9), f"j{COTOVELO_ESQ}": (180.0, 100.0, 0.9)}
    )
    sem_osso = desenha.desenha(amostra([(caixa(), fraco)], track(1)), limiar=0.3)
    com_osso = desenha.desenha(amostra([(caixa(), forte)], track(1)), limiar=0.3)
    assert sem_osso[100, 100].sum() == 0
    assert com_osso[100, 100].sum() > 0


def test_junta_sem_numero_nao_vira_ponto_nem_estoura():
    nan = float("nan")
    pose = pose_com(j9=(nan, nan, nan), j10=(100.0, 100.0, nan))
    saida = desenha.desenha(amostra([(caixa(), pose)], track(1)), limiar=0.3)
    assert saida[100, 100 - desenha.RAIO_PULSO].sum() == 0


def test_frame_original_fica_intacto():
    """O frame vem do pipe do ffmpeg, somente leitura. Desenhar por cima dele
    estouraria; desenhar numa vista compartilhada sujaria a medição."""
    base = amostra([(caixa(), pose_com(j9=(50.0, 50.0, 0.9)))], track(1))
    saida = desenha.desenha(base, limiar=0.3)
    assert base.imagem.sum() == 0
    assert saida is not base.imagem


def test_track_sem_pose_aparece_em_cinza():
    """Provisório e perdido não vão para a pose. Eles aparecem, em cinza, para
    explicar por que aquela pessoa está sem esqueleto — e não somem do vídeo."""
    provisorio = track(2, status=TrackStatus.PROVISORIO, x1=20.0, y1=20.0, x2=120.0, y2=120.0)
    saida = desenha.desenha(amostra([], provisorio), limiar=0.3)
    b, g, r = (int(v) for v in saida[20, 70])
    assert b == g == r > 0


def test_cor_de_cada_track_e_estavel_e_distinta():
    """A verificação do estágio 3 já é visual: cor trocando no meio do percurso
    é troca de ID (R-2). A cor tem que ser função do ID, e só dele."""
    assert desenha.cor_do_track(7) == desenha.cor_do_track(7)
    cores = {desenha.cor_do_track(i) for i in range(1, 9)}
    assert len(cores) == 8
    assert all(cor != desenha.CINZA for cor in cores)


# --- o vídeo, de verdade ----------------------------------------------------------


def _video(caminho: Path, largura: int, altura: int, segundos: int = 2) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg não está no PATH")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={largura}x{altura}:rate=30",
            "-t",
            str(segundos),
            "-pix_fmt",
            "yuv420p",
            str(caminho),
        ],
        check=True,
    )
    return caminho


def _sonda(video: Path) -> dict:
    saida = subprocess.run(  # noqa: S603
        [
            shutil.which("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-count_frames",
            "-show_entries",
            "stream=width,height,nb_read_frames,codec_name",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(saida.stdout)["streams"][0]


class DetectorFalso:
    def detect(self, frame):
        return (
            Detection(
                class_id=0,
                score=0.9,
                x1=frame.width * 0.4,
                y1=frame.height * 0.1,
                x2=frame.width * 0.6,
                y2=frame.height * 0.9,
            ),
        )


class PoseFalsa:
    def estimar(self, imagem, caixas):
        return tuple(pose_com(j9=((c[0] + c[2]) / 2, (c[1] + c[3]) / 2, 0.8)) for c in caixas)


def test_video_desenhado_tem_os_frames_e_a_resolucao_do_que_foi_medido(tmp_path: Path):
    """Um frame por amostra de `percorre`, no tamanho do frame original: o vídeo
    mostra o que a pose recortou, não a cópia reduzida que o detector viu."""
    entrada = _video(tmp_path / "v.mp4", 1280, 720, segundos=2)
    saida = tmp_path / "saida" / "v.mp4"

    frames = desenha.desenha_video(
        entrada, saida, DetectorFalso(), PoseFalsa(), fps=8.0, lado_detector=640, limiar=0.3
    )

    assert frames == 16
    sonda = _sonda(saida)
    assert (sonda["width"], sonda["height"]) == (1280, 720)
    assert int(sonda["nb_read_frames"]) == 16
    assert sonda["codec_name"] == "h264"


def test_pasta_espelha_a_estrutura_e_respeita_o_maximo(tmp_path: Path, monkeypatch):
    """Com 182 vídeos no dataset, desenhar todos é horas de disco à toa. O
    máximo é por pasta, para a amostra ter das duas classes."""

    class OnnxDetectorFalso(DetectorFalso):
        def __init__(self, caminho, opcoes) -> None:
            pass

        def info(self):
            from lince_agent.detect.state import DetectorInfo

            return DetectorInfo("dublê@0", 640, "CPUExecutionProvider", (0,))

        def close(self) -> None:
            pass

    class OnnxPoseFalsa(PoseFalsa):
        def __init__(self, caminho, providers) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(desenha, "OnnxDetector", OnnxDetectorFalso)
    monkeypatch.setattr(desenha, "OnnxPose", OnnxPoseFalsa)
    fonte = tmp_path / "dataset"
    for nome in ("Normal/a.mp4", "Normal/b.mp4", "Normal/c.mp4", "Shoplifting/x.mp4"):
        _video(fonte / nome, 320, 240, segundos=1)
    destino = tmp_path / "desenhados"

    codigo = desenha.main(
        [
            str(fonte),
            "--saida",
            str(destino),
            "--detector",
            "d.onnx",
            "--pose",
            "p.onnx",
            "--provider",
            "cpu",
            "--max-por-pasta",
            "2",
        ]
    )

    assert codigo == 0
    gerados = sorted(p.relative_to(destino).as_posix() for p in destino.rglob("*.mp4"))
    assert gerados == ["Normal/a.mp4", "Normal/b.mp4", "Shoplifting/x.mp4"]


def test_saida_dentro_da_fonte_e_recusada(tmp_path: Path, capsys):
    """Gravar os vídeos desenhados dentro da pasta do dataset faria a próxima
    rodada desenhar por cima dos desenhos."""
    fonte = tmp_path / "dataset"
    _video(fonte / "a.mp4", 64, 48, segundos=1)
    codigo = desenha.main(
        [
            str(fonte),
            "--saida",
            str(fonte / "desenhados"),
            "--detector",
            "d.onnx",
            "--pose",
            "p.onnx",
            "--provider",
            "cpu",
        ]
    )
    assert codigo == 1
    assert "dentro da fonte" in capsys.readouterr().err
