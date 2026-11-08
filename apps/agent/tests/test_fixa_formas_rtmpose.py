"""As formas declaradas do ONNX que o mmdeploy grava.

O export real do RTMPose-s (`pose-detection_simcc_onnxruntime_dynamic.py`)
saiu com o eixo das juntas simbólico — `MatMulsimcc_x_dim_1` em vez de 17.
`OnnxPose` recusa isso de propósito: sem o 17 declarado, um WholeBody de 133
canais renomeado passaria na subida e o pulso sairia no índice errado. O
conserto não é afrouxar a trava; é rodar o grafo, ver que ele devolve
(lote, 17, bins) de fato e só então gravar esses números no arquivo.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper, numpy_helper

from lince_agent.detect.onnx import abre_sessao
from lince_agent.pose.onnx import BINS_X, BINS_Y, NUM_JUNTAS, OnnxPose
from lince_agent.pose.preprocess import INPUT_ALTURA, INPUT_LARGURA

_CAMINHO = Path(__file__).resolve().parents[1] / "tools" / "fixa_formas_rtmpose.py"
# Mesmo pulo de `test_exporta_rtmpose.py`: o estágio `teste` da imagem não copia tools/.
if not _CAMINHO.is_file():
    pytest.skip(
        f"{_CAMINHO} não está nesta árvore. A imagem do agente não copia tools/.",
        allow_module_level=True,
    )
_SPEC = importlib.util.spec_from_file_location("fixa_formas_rtmpose", _CAMINHO)
assert _SPEC is not None and _SPEC.loader is not None
fixa = importlib.util.module_from_spec(_SPEC)
sys.modules["fixa_formas_rtmpose"] = fixa
_SPEC.loader.exec_module(fixa)


def grava(
    caminho: Path,
    *,
    juntas_reais: int = NUM_JUNTAS,
    bins: dict[str, int] | None = None,
    declarado: dict[str, list] | None = None,
) -> Path:
    """Um grafo que devolve (N, juntas_reais, bins) e declara o que se pedir.

    O padrão imita o mmdeploy: lote `batch`, juntas com nome de eixo do
    MatMul, bins fixos.
    """
    bins = {"simcc_x": BINS_X, "simcc_y": BINS_Y} if bins is None else bins
    if declarado is None:
        declarado = {
            nome: ["batch", f"MatMul{nome}_dim_1", tamanho] for nome, tamanho in bins.items()
        }
    entrada = helper.make_tensor_value_info(
        "input", TensorProto.FLOAT, ["batch", 3, INPUT_ALTURA, INPUT_LARGURA]
    )
    inits = [
        numpy_helper.from_array(np.array(0, dtype=np.int64), "idx0"),
        numpy_helper.from_array(np.array([1, 1], dtype=np.int64), "uns"),
        numpy_helper.from_array(np.array([0], dtype=np.int64), "eixo0"),
    ]
    nos = [
        helper.make_node("Shape", ["input"], ["shape_in"]),
        helper.make_node("Gather", ["shape_in", "idx0"], ["n_escalar"], axis=0),
        helper.make_node("Unsqueeze", ["n_escalar", "eixo0"], ["n_vec"]),
        helper.make_node("Concat", ["n_vec", "uns"], ["repeats"], axis=0),
    ]
    saidas = []
    for nome, tamanho in bins.items():
        base = np.zeros((1, juntas_reais, tamanho), dtype=np.float32)
        inits.append(numpy_helper.from_array(base, f"base_{nome}"))
        nos.append(helper.make_node("Tile", [f"base_{nome}", "repeats"], [nome]))
        saidas.append(helper.make_tensor_value_info(nome, TensorProto.FLOAT, declarado[nome]))
    grafo = helper.make_graph(nos, "mmdeploy_sintetico", [entrada], saidas, inits)
    modelo = helper.make_model(grafo, opset_imports=[helper.make_opsetid("", 13)])
    modelo.ir_version = 8
    onnx.checker.check_model(modelo)
    onnx.save(modelo, caminho)
    return caminho


def _declarado(caminho: Path) -> dict[str, list]:
    modelo = onnx.load(caminho)
    formas = {}
    for saida in modelo.graph.output:
        dims = saida.type.tensor_type.shape.dim
        formas[saida.name] = [d.dim_param if d.HasField("dim_param") else d.dim_value for d in dims]
    return formas


def test_eixo_de_juntas_simbolico_vira_17_e_passa_na_trava(tmp_path: Path):
    """O arquivo do export real, antes e depois. Depois, `OnnxPose` abre."""
    original = grava(tmp_path / "end2end.onnx")
    with pytest.raises(Exception, match="juntas da saída"):
        OnnxPose(
            original.rename(tmp_path / "rtmpose-s-cru.onnx"),
            providers=("CPUExecutionProvider",),
            session_factory=abre_sessao,
        )

    destino = tmp_path / "rtmpose-s.onnx"
    assert fixa.main([str(tmp_path / "rtmpose-s-cru.onnx"), str(destino)]) == 0

    assert _declarado(destino) == {
        "simcc_x": ["batch", NUM_JUNTAS, BINS_X],
        "simcc_y": ["batch", NUM_JUNTAS, BINS_Y],
    }
    pose = OnnxPose(destino, providers=("CPUExecutionProvider",), session_factory=abre_sessao)
    assert pose.estimar(np.zeros((480, 640, 3), dtype=np.uint8), [(10, 10, 110, 310)])


def test_grafo_que_devolve_outra_contagem_de_juntas_nao_e_carimbado(tmp_path: Path):
    """O carimbo só vale porque é conferido. Um grafo de 133 canais com o eixo
    simbólico não pode sair do conserto declarando 17."""
    cru = grava(tmp_path / "cru.onnx", juntas_reais=133)
    destino = tmp_path / "rtmpose-s.onnx"
    assert fixa.main([str(cru), str(destino)]) == 1
    assert not destino.exists()


def test_bins_sem_o_split_ratio_sao_recusados(tmp_path: Path):
    cru = grava(tmp_path / "cru.onnx", bins={"simcc_x": 192, "simcc_y": 256})
    destino = tmp_path / "rtmpose-s.onnx"
    assert fixa.main([str(cru), str(destino)]) == 1
    assert not destino.exists()


def test_eixo_de_lote_continua_dinamico(tmp_path: Path):
    """Fixar o lote em 2, o tamanho que o conserto usa para observar, mandaria
    todo frame de uma pessoa estourar na sessão."""
    destino = tmp_path / "rtmpose-s.onnx"
    assert fixa.main([str(grava(tmp_path / "cru.onnx")), str(destino)]) == 0
    modelo = onnx.load(destino)
    entrada = modelo.graph.input[0].type.tensor_type.shape.dim
    assert entrada[0].dim_param == "batch"
    for saida in modelo.graph.output:
        assert saida.type.tensor_type.shape.dim[0].dim_param == "batch"


def test_arquivo_ja_correto_sai_igual_no_que_importa(tmp_path: Path):
    """Um export que um dia vier certo não pode ser estragado pelo conserto."""
    certo = grava(
        tmp_path / "cru.onnx",
        declarado={
            "simcc_x": ["batch", NUM_JUNTAS, BINS_X],
            "simcc_y": ["batch", NUM_JUNTAS, BINS_Y],
        },
    )
    destino = tmp_path / "rtmpose-s.onnx"
    assert fixa.main([str(certo), str(destino)]) == 0
    assert _declarado(destino) == _declarado(certo)
