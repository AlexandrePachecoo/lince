"""O benchmark do detector, sem modelo e com um vídeo sintético.

O que este arquivo segura são os dois números que decidem o box sem placa (fase 1): o
recall relativo e as câmeras sustentadas. Um recall que aprova modelo cego, ou uma conta
de câmeras otimista, mandariam para a loja um mini PC que não dá conta — e isso não dá
erro, dá loja que perde gente.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lince_agent.detect.state import Detection, DetectorInfo

_CAMINHO = Path(__file__).resolve().parents[1] / "tools" / "bench_deteccao.py"
# O estágio `teste` da imagem copia tests/ e não tools/ — ver test_bench_pose.py.
if not _CAMINHO.is_file():
    pytest.skip(
        f"{_CAMINHO} não está nesta árvore. A imagem do agente não copia tools/.",
        allow_module_level=True,
    )
_SPEC = importlib.util.spec_from_file_location("bench_deteccao", _CAMINHO)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
sys.modules["bench_deteccao"] = bench
_SPEC.loader.exec_module(bench)

PESSOA = (100.0, 100.0, 160.0, 260.0)


# --- recall relativo ---------------------------------------------------------------


def test_recall_conta_as_pessoas_da_referencia_que_o_medido_tambem_acha():
    referencia = [[PESSOA], [PESSOA, (300.0, 100.0, 360.0, 260.0)]]
    medido = [[PESSOA], [(102.0, 101.0, 161.0, 262.0)]]  # a segunda pessoa sumiu
    assert bench.recall_relativo(referencia, medido) == (2, 3)


def test_caixa_extra_do_medido_nao_aumenta_o_recall():
    """Recall é sobre quem a referência viu. Um modelo que inventa caixas não pode
    compensar com elas as pessoas que deixou de ver."""
    referencia = [[PESSOA]]
    medido = [[(400.0, 0.0, 460.0, 160.0), (500.0, 0.0, 560.0, 160.0)]]
    assert bench.recall_relativo(referencia, medido) == (0, 1)


def test_video_sem_ninguem_nao_aprova_modelo_nenhum():
    """Sem pessoas na referência, não há recall a medir. Devolver 100% ali aprovaria
    qualquer modelo — inclusive um cego — num vídeo de loja vazia."""
    assert bench.recall_relativo([[], []], [[], [PESSOA]]) is None


def test_recall_de_videos_diferentes_e_recusado():
    with pytest.raises(bench.BenchError):
        bench.recall_relativo([[PESSOA]], [[PESSOA], [PESSOA]])


def test_iou_de_caixa_degenerada_e_zero_e_nao_nan():
    """`nan >= limiar` é falso em silêncio: a caixa degenerada sumiria do recall sem
    sinal nenhum (armadilha já paga no NMS)."""
    ponto = (10.0, 10.0, 10.0, 10.0)
    assert bench.iou(ponto, ponto) == 0.0
    assert bench.iou(PESSOA, PESSOA) == 1.0


# --- câmeras sustentadas -------------------------------------------------------------


def test_cameras_sustentadas_usa_o_p95_e_arredonda_para_baixo():
    """33 ms a 3 fps são 100 ms por segundo por câmera: cabem 10, não 10,1. Arredondar
    para cima é prometer uma câmera que a fila do §3.2 vai descartar."""
    assert bench.cameras_sustentadas(33.0, 3.0) == 10
    assert bench.cameras_sustentadas(34.0, 3.0) == 9
    assert bench.cameras_sustentadas(400.0, 3.0) == 0


def test_cameras_sustentadas_recusa_tempo_nao_positivo():
    with pytest.raises(bench.BenchError):
        bench.cameras_sustentadas(0.0, 3.0)


def test_relatorio_mede_cada_modelo_contra_o_primeiro():
    modelos = [bench.Modelo(Path("yolox_s.onnx"), 640), bench.Modelo(Path("yolox_tiny.onnx"), 416)]
    medidas = [
        bench.Medida("CPUExecutionProvider", [70.0] * 10, [[PESSOA], [PESSOA]]),
        bench.Medida("OpenVINOExecutionProvider", [20.0] * 10, [[PESSOA], []]),
    ]
    relatorio = bench.monta_relatorio(modelos, medidas, fps=3.0, frames=2)

    s, tiny = relatorio["modelos"]
    assert relatorio["referencia"] == "yolox_s"
    assert (s["recall"], s["cameras"]) == (1.0, 4)
    assert (tiny["recall"], tiny["cameras"], tiny["provider"]) == (
        0.5,
        16,
        "OpenVINOExecutionProvider",
    )
    tabela = bench.formata_tabela(relatorio)
    assert "yolox_tiny" in tabela and "50%" in tabela
    json.dumps(relatorio)  # o --saida grava isto


# --- medição e entrada ---------------------------------------------------------------


class _Detector:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    def detect(self, frame):
        score = self._scores[frame.sequence]
        return (Detection(0, score, *PESSOA),)

    def info(self):
        return DetectorInfo("dublê@0", 416, "OpenVINOExecutionProvider", (0,))


def test_mede_tira_o_aquecimento_do_tempo_mas_nao_das_caixas(tmp_path: Path):
    """A primeira inferência aloca arenas e, no OpenVINO, compila o grafo. Ela não pode
    entrar no tempo — mas tem que entrar nas caixas, senão o recall compara o frame 3 de
    um modelo com o frame 0 do outro."""
    quadros = bench.decodifica(_video(tmp_path, 6), largura=64, altura=48, fps=3.0, max_frames=6)
    medida = bench.mede(_Detector([0.9, 0.9, 0.9, 0.9, 0.3, 0.9]), quadros)

    assert len(medida.ms) == len(quadros) - bench.AQUECIMENTO
    assert [len(caixas) for caixas in medida.caixas] == [1, 1, 1, 1, 0, 1]
    assert medida.provider == "OpenVINOExecutionProvider"


def test_mede_recusa_video_que_so_tem_aquecimento(tmp_path: Path):
    quadros = bench.decodifica(_video(tmp_path, 2), largura=64, altura=48, fps=3.0, max_frames=2)
    with pytest.raises(bench.BenchError, match="frames"):
        bench.mede(_Detector([0.9, 0.9]), quadros)


def test_decodifica_entrega_o_frame_que_o_estagio_2_receberia(tmp_path: Path):
    quadros = bench.decodifica(_video(tmp_path, 9), largura=64, altura=48, fps=3.0, max_frames=4)
    assert len(quadros) == 4
    assert (quadros[0].width, quadros[0].height) == (64, 48)
    assert len(quadros[0].data) == 64 * 48 * 3
    assert [q.sequence for q in quadros] == [0, 1, 2, 3]


def test_decodifica_recusa_video_que_nao_existe(tmp_path: Path):
    with pytest.raises(bench.BenchError, match="não encontrado"):
        bench.decodifica(tmp_path / "nada.mp4", largura=64, altura=48, fps=3.0, max_frames=1)


@pytest.mark.parametrize("texto", ["models/yolox_tiny.onnx", ":416", "m.onnx:grande"])
def test_modelo_sem_entrada_valida_e_recusado(texto: str):
    """A entrada é obrigatória: tiny e nano são de 416, o s é de 640."""
    with pytest.raises(bench.BenchError):
        bench.interpreta_modelo(texto)


def test_modelo_com_entrada():
    modelo = bench.interpreta_modelo("models/yolox_tiny.onnx:416")
    assert (modelo.nome, modelo.entrada) == ("yolox_tiny", 416)


def test_providers_poem_a_cpu_depois_do_pedido():
    assert bench.providers_de("openvino") == ("OpenVINOExecutionProvider", "CPUExecutionProvider")
    assert bench.providers_de("cpu") == ("CPUExecutionProvider",)
    with pytest.raises(bench.BenchError):
        bench.providers_de("tensorrt")


def test_main_devolve_erro_legivel_sem_o_video(tmp_path: Path, capsys):
    codigo = bench.main([str(tmp_path / "nada.mp4"), "--modelo", "m.onnx:416"])
    assert codigo == 2
    assert "não encontrado" in capsys.readouterr().err


def _video(pasta: Path, frames_a_3fps: int) -> Path:
    """Um vídeo curto de verdade, pelo ffmpeg do ambiente (regra 3: nada de mock onde
    cabe o real)."""
    destino = pasta / "sintetico.mp4"
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg is not None
    subprocess.run(
        [
            ffmpeg,
            *("-v", "error", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=3"),
            *("-frames:v", str(frames_a_3fps), "-pix_fmt", "yuv420p", str(destino)),
        ],
        check=True,
    )
    return destino
