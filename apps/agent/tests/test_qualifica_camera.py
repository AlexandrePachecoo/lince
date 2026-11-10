"""A régua que decide se uma câmera entra no piloto do gesto (Fase 0).

O erro caro aqui não estoura: é aprovar uma câmera que não vê a mão. O piloto
treinaria em ruído, a sombra dispararia à toa, e a conclusão errada seria
"o classificador é ruim" — quando o problema era o ângulo. Por isso cada
critério do plano tem um teste que o fixa, e o modelo fica de fora: detector e
pose são dublês, que é a exceção para mock que o CLAUDE.md reserva. O vídeo e o
ffmpeg são de verdade.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lince_agent.detect.state import Detection
from lince_agent.pose.onnx import Junta, PosePessoa

_CAMINHO = Path(__file__).resolve().parents[1] / "tools" / "qualifica_camera.py"
# Mesmo pulo dos outros testes de tools/: a imagem do agente não copia tools/.
if not _CAMINHO.is_file():
    pytest.skip(
        f"{_CAMINHO} não está nesta árvore. A imagem do agente não copia tools/.",
        allow_module_level=True,
    )
_SPEC = importlib.util.spec_from_file_location("qualifica_camera", _CAMINHO)
assert _SPEC is not None and _SPEC.loader is not None
qualifica = importlib.util.module_from_spec(_SPEC)
sys.modules["qualifica_camera"] = qualifica
_SPEC.loader.exec_module(qualifica)

CRITERIOS = qualifica.Criterios()


def pose(pulso_esquerdo: float, pulso_direito: float) -> PosePessoa:
    confiancas = [0.9] * 17
    confiancas[9] = pulso_esquerdo
    confiancas[10] = pulso_direito
    return PosePessoa(
        juntas=tuple(Junta(indice=i, x=0.0, y=0.0, confianca=c) for i, c in enumerate(confiancas))
    )


def medidor_com(track_id: int, amostras: list[tuple[float, float, float]]) -> object:
    """`amostras` = (instante_s, altura_px, confiança do melhor pulso)."""
    medidor = qualifica.Medidor()
    for instante, altura, conf in amostras:
        medidor.observa(track_id, instante, altura, pose(conf, 0.0))
    return medidor


# --- o que conta como track ------------------------------------------------------


def test_track_curto_nao_entra_na_conta():
    """O plano mede "tracks com mais de 1 s". Um track de meio segundo é passagem
    pela borda ou detecção espúria; contá-lo puxaria a régua para qualquer lado."""
    medidor = medidor_com(1, [(0.0, 200.0, 0.9), (0.5, 200.0, 0.9)])
    assert medidor.tracks_longos(CRITERIOS) == []


def test_track_de_um_segundo_ou_mais_entra():
    medidor = medidor_com(1, [(0.0, 200.0, 0.9), (1.0, 200.0, 0.9)])
    (track,) = medidor.tracks_longos(CRITERIOS)
    assert track.track_id == 1
    assert track.duracao_s == pytest.approx(1.0)


def test_altura_do_track_e_a_mediana_nao_a_maxima():
    """A pessoa cresce ao se aproximar da câmera. O máximo aprovaria um corredor
    em que ela só fica grande no último passo, colada na lente."""
    medidor = medidor_com(
        1, [(0.0, 100.0, 0.9), (0.5, 120.0, 0.9), (1.0, 140.0, 0.9), (1.5, 400.0, 0.9)]
    )
    (track,) = medidor.tracks_longos(CRITERIOS)
    assert track.altura_mediana_px == pytest.approx(130.0)


def test_punho_utilizavel_exige_metade_das_amostras_com_confianca():
    medidor = medidor_com(
        1, [(0.0, 200.0, 0.9), (0.5, 200.0, 0.9), (1.0, 200.0, 0.1), (1.5, 200.0, 0.1)]
    )
    (track,) = medidor.tracks_longos(CRITERIOS)
    assert track.fracao_punho == pytest.approx(0.5)
    assert track.punho_utilizavel(CRITERIOS)


def test_um_pulso_visivel_basta():
    """A mão que esconde é uma. Exigir os dois pulsos recusaria toda câmera
    oblíqua, em que um braço fica atrás do tronco."""
    medidor = qualifica.Medidor()
    for instante in (0.0, 0.5, 1.0):
        medidor.observa(1, instante, 200.0, pose(0.0, 0.8))
    (track,) = medidor.tracks_longos(CRITERIOS)
    assert track.fracao_punho == pytest.approx(1.0)


def test_confianca_nao_finita_nao_conta_como_punho():
    """`nan >= limiar` é falso, mas `max(nan, x)` depende da ordem. Junta sem
    número não pode virar punho visível por acidente de comparação."""
    medidor = qualifica.Medidor()
    for instante in (0.0, 0.5, 1.0):
        medidor.observa(1, instante, 200.0, pose(float("nan"), 0.0))
    (track,) = medidor.tracks_longos(CRITERIOS)
    assert track.fracao_punho == 0.0


# --- o veredito ------------------------------------------------------------------


def _tracks(*especificacoes: tuple[float, float]) -> list:
    """Cada especificação é (altura_px, confiança do pulso), 3 amostras em 1 s."""
    medidor = qualifica.Medidor()
    for track_id, (altura, conf) in enumerate(especificacoes, start=1):
        for instante in (0.0, 0.5, 1.0):
            medidor.observa(track_id, instante, altura, pose(conf, 0.0))
    return medidor.tracks_longos(CRITERIOS)


def test_camera_boa_passa():
    resumo = qualifica.avalia(_tracks((220, 0.8), (240, 0.7), (200, 0.2)), (1280, 720), CRITERIOS)
    assert resumo.passa
    assert resumo.fracao_punho_utilizavel == pytest.approx(2 / 3)
    assert resumo.motivos == ()


def test_menos_da_metade_com_punho_reprova():
    """O critério de recusa do plano, palavra por palavra: menos da metade dos
    tracks longos com punho decente e a câmera não entra. Mexer no limiar do
    classificador não corrige ângulo."""
    resumo = qualifica.avalia(_tracks((220, 0.8), (240, 0.1), (200, 0.1)), (1280, 720), CRITERIOS)
    assert not resumo.passa
    assert any("punho" in motivo for motivo in resumo.motivos)


def test_pessoa_pequena_reprova_mesmo_com_punho():
    """Caixa de 120 px dá um punho de poucos pixels no recorte 256×192. A
    confiança pode até sair alta: é o modelo chutando em cima de borrão."""
    resumo = qualifica.avalia(_tracks((120, 0.9), (130, 0.9), (110, 0.9)), (1280, 720), CRITERIOS)
    assert not resumo.passa
    assert any("180" in motivo for motivo in resumo.motivos)


def test_resolucao_abaixo_de_720p_reprova_e_diz_por_que():
    """O plano exige 1280×720 no pipe de detecção. 640×480 é a câmera de hoje, e
    o piloto do gesto a recusa — mas o resto da medição continua útil, então o
    motivo sai junto com os números, não no lugar deles."""
    resumo = qualifica.avalia(_tracks((220, 0.8), (240, 0.7)), (640, 480), CRITERIOS)
    assert not resumo.passa
    assert any("720" in motivo for motivo in resumo.motivos)
    assert resumo.fracao_punho_utilizavel == pytest.approx(1.0)


def test_sem_track_longo_nao_e_aprovacao():
    """Vídeo sem ninguém não prova que a câmera vê a mão. Zero de zero não pode
    virar "100% dos tracks"."""
    resumo = qualifica.avalia([], (1280, 720), CRITERIOS)
    assert not resumo.passa
    assert resumo.tracks_longos == 0
    assert any("nenhum track" in motivo for motivo in resumo.motivos)


# --- o vídeo, de verdade --------------------------------------------------------


def _video(caminho: Path, largura: int, altura: int, segundos: int = 2) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg não está no PATH")
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


def test_frames_saem_na_taxa_pedida_e_na_resolucao_nativa(tmp_path: Path):
    """O plano mede a 8 fps e "no frame que o agente realmente vê". Sem
    `--largura`, esse frame é o do arquivo: escalar por conta própria mediria
    outra pessoa, maior ou menor do que a câmera entrega."""
    video = _video(tmp_path / "v.mp4", 768, 432, segundos=2)
    frames = list(qualifica.frames_de(video, fps=8.0))
    assert len(frames) == 16
    assert {imagem.shape for _, imagem in frames} == {(432, 768, 3)}
    assert [round(t, 3) for t, _ in frames[:3]] == [0.0, 0.125, 0.25]


def test_largura_emula_o_scale_do_agente(tmp_path: Path):
    video = _video(tmp_path / "v.mp4", 1920, 1080, segundos=1)
    frames = list(qualifica.frames_de(video, fps=4.0, largura=1280))
    assert frames[0][1].shape == (720, 1280, 3)


def test_videos_de_uma_pasta_sao_achados_em_subpastas(tmp_path: Path):
    """Datasets vêm separados por classe (`normal/`, `shoplifting/`). A ferramenta
    tem que achar todos e lembrar de onde cada um veio."""
    (tmp_path / "normal").mkdir()
    (tmp_path / "furto").mkdir()
    _video(tmp_path / "normal" / "a.mp4", 64, 48, segundos=1)
    _video(tmp_path / "furto" / "b.avi", 64, 48, segundos=1)
    (tmp_path / "leiame.txt").write_text("não é vídeo")
    achados = qualifica.videos_em(tmp_path)
    assert [p.relative_to(tmp_path).as_posix() for p in achados] == ["furto/b.avi", "normal/a.mp4"]


class DetectorFalso:
    """Uma pessoa parada no meio do quadro, no espaço do frame que recebeu."""

    def __init__(self) -> None:
        self.tamanhos: list[tuple[int, int]] = []

    def detect(self, frame):
        self.tamanhos.append((frame.width, frame.height))
        x = frame.width / 2
        return (
            Detection(
                class_id=0,
                score=0.9,
                x1=x - frame.width * 0.05,
                y1=frame.height * 0.1,
                x2=x + frame.width * 0.05,
                y2=frame.height * 0.6,
            ),
        )


class PoseFalsa:
    def __init__(self) -> None:
        self.caixas: list[tuple] = []
        self.imagens: list[tuple[int, ...]] = []

    def estimar(self, imagem, caixas):
        self.imagens.append(imagem.shape)
        self.caixas.extend(caixas)
        return tuple(pose(0.8, 0.0) for _ in caixas)


def test_detector_e_pose_recebem_o_frame_cheio(tmp_path: Path):
    """Num vídeo 1280×720 o detector recebe o frame cheio e reduz por dentro, no
    letterbox, como no agente: uma redução só, para a ferramenta e para a loja.
    A pose recorta do mesmo frame, onde o punho tem o dobro de pixels, e a altura
    medida é a do frame original."""
    video = _video(tmp_path / "v.mp4", 1280, 720, segundos=2)
    detector = DetectorFalso()
    pose_falsa = PoseFalsa()

    medido = qualifica.processa_video(video, detector, pose_falsa, fps=8.0)

    assert set(detector.tamanhos) == {(1280, 720)}
    assert set(pose_falsa.imagens) == {(720, 1280, 3)}
    (track,) = medido.tracks
    assert track.altura_mediana_px == pytest.approx(720 * 0.5, abs=2.0)
    assert medido.resolucao == (1280, 720)


def test_relatorio_reune_por_pasta_e_no_total(tmp_path: Path):
    """O dataset de furto encenado tem `normal/` e `shoplifting/`. O total é o
    que decide; a separação mostra se a câmera vê a mão justamente no gesto."""
    resumos = {
        Path("normal/a.mp4"): qualifica.avalia(_tracks((220, 0.8)), (1280, 720), CRITERIOS),
        Path("furto/b.mp4"): qualifica.avalia(_tracks((220, 0.1)), (1280, 720), CRITERIOS),
    }
    tracks = {Path("normal/a.mp4"): _tracks((220, 0.8)), Path("furto/b.mp4"): _tracks((220, 0.1))}
    relatorio = qualifica.monta_relatorio(
        resumos, tracks, {p: (1280, 720) for p in resumos}, CRITERIOS
    )

    assert set(relatorio["por_pasta"]) == {"normal", "furto"}
    assert relatorio["por_pasta"]["normal"]["passa"] is True
    assert relatorio["por_pasta"]["furto"]["passa"] is False
    assert relatorio["total"]["tracks_longos"] == 2
    assert relatorio["total"]["fracao_punho_utilizavel"] == pytest.approx(0.5)
    texto = qualifica.formata(relatorio)
    assert "normal" in texto and "furto" in texto and "TOTAL" in texto


def test_resolucoes_diferentes_no_total_reprovam_pela_pior():
    """Somar um vídeo 720p com um 480p e declarar "720p" aprovaria a câmera
    errada. O total carrega a menor resolução vista."""
    tracks = {Path("a.mp4"): _tracks((220, 0.8)), Path("b.mp4"): _tracks((220, 0.8))}
    resolucoes = {Path("a.mp4"): (1280, 720), Path("b.mp4"): (640, 480)}
    resumos = {p: qualifica.avalia(t, resolucoes[p], CRITERIOS) for p, t in tracks.items()}
    relatorio = qualifica.monta_relatorio(resumos, tracks, resolucoes, CRITERIOS)
    assert relatorio["total"]["resolucao"] == [640, 480]
    assert relatorio["total"]["passa"] is False


def test_numeros_da_regua_sao_os_do_plano():
    """Os números vêm da Fase 0 do plano, não de gosto. Mudá-los é mudar o
    portão — tem que ser decisão escrita, não um default que escorregou."""
    assert CRITERIOS.altura_min_px == 180
    assert CRITERIOS.fracao_tracks_min == 0.5
    assert CRITERIOS.duracao_min_s == 1.0
    assert (CRITERIOS.largura_min, CRITERIOS.altura_min) == (1280, 720)
    assert qualifica.FPS_PADRAO == 8.0


def test_linha_de_comando_ponta_a_ponta(tmp_path: Path, monkeypatch, capsys):
    """A montagem inteira — achar os vídeos, abrir os modelos com o lado do
    detector, medir, imprimir e gravar o JSON —, com os modelos trocados por
    dublês. O código de saída diz o veredito: 0 passa, 2 não passa, 1 erro."""
    from lince_agent.detect.state import DetectorInfo

    class OnnxDetectorFalso(DetectorFalso):
        fechado = False

        def __init__(self, caminho, opcoes) -> None:
            super().__init__()
            assert opcoes.providers == ("CPUExecutionProvider",)

        def info(self):
            return DetectorInfo("dublê@0", 640, "CPUExecutionProvider", (0,))

        def close(self) -> None:
            OnnxDetectorFalso.fechado = True

    class OnnxPoseFalsa(PoseFalsa):
        def __init__(self, caminho, providers) -> None:
            super().__init__()

        def info(self):
            class Info:
                provider = "CPUExecutionProvider"

            return Info()

        def close(self) -> None:
            pass

    monkeypatch.setattr(qualifica, "OnnxDetector", OnnxDetectorFalso)
    monkeypatch.setattr(qualifica, "OnnxPose", OnnxPoseFalsa)
    pasta = tmp_path / "dataset"
    (pasta / "normal").mkdir(parents=True)
    _video(pasta / "normal" / "a.mp4", 1280, 720, segundos=2)
    saida = tmp_path / "relatorio.json"

    codigo = qualifica.main(
        [
            str(pasta),
            "--detector",
            "d.onnx",
            "--pose",
            "p.onnx",
            "--provider",
            "cpu",
            "--saida",
            str(saida),
        ]
    )

    assert codigo == 0
    assert OnnxDetectorFalso.fechado
    assert "TOTAL" in capsys.readouterr().out
    import json

    relatorio = json.loads(saida.read_text())
    assert relatorio["total"]["passa"] is True
    assert list(relatorio["videos"]) == ["normal/a.mp4"]


def test_pasta_sem_video_e_erro_e_nao_aprovacao(tmp_path: Path, capsys):
    (tmp_path / "vazia").mkdir()
    codigo = qualifica.main(
        [str(tmp_path / "vazia"), "--detector", "d.onnx", "--pose", "p.onnx", "--provider", "cpu"]
    )
    assert codigo == 1
    assert "nenhum vídeo" in capsys.readouterr().err
