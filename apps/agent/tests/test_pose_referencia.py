"""O preprocess e o decode contra uma referência que não é o nosso código.

`to_crop` seguido de `undo_crop` passa mesmo quando as duas funções compartilham
o mesmo erro de convenção. A fixture foi gerada por
`tools/gera_referencia_pose.py` com o MMPose 1.3.2: `GetBBoxCenterScale`,
`TopdownAffine`, `PoseDataPreprocessor` e `SimCCLabel.decode`, mais a projeção
do `TopdownPoseEstimator`. O teste não importa mmpose.

Tolerância, de propósito apertada:

- o checksum é o sha256 dos bytes do tensor float32 inteiro. Ou bate, ou o
  recorte não é o do warpAffine;
- a amostra de pixels usa atol 0, para o erro dizer qual pixel saiu;
- junta no frame usa atol 1e-3. Meio pixel de convenção, nesta escala de
  caixa, são cerca de 0,5 px — folga de um milésimo não esconde isso;
- confiança usa atol 1e-5, acima do ruído de float32 de um pico como 0.3 e
  abaixo de trocar o mínimo dos eixos pela média.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from lince_agent.pose.onnx import NUM_JUNTAS, decode_simcc
from lince_agent.pose.preprocess import INPUT_ALTURA, INPUT_LARGURA, Crop, crop_bbox

_FIXTURE = Path(__file__).parent / "fixtures" / "pose_rtmpose_s.npz"
_ATOL_JUNTA = 1e-3
_ATOL_CONFIANCA = 1e-5


def _frame(seed: int, altura: int, largura: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 256, size=(altura, largura, 3), dtype=np.uint8)


def _checksum(tensor: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(tensor, dtype=np.float32).tobytes()).hexdigest()


@pytest.fixture(scope="module")
def referencia():
    if not _FIXTURE.is_file():
        pytest.fail(f"fixture ausente: {_FIXTURE}. Rode tools/gera_referencia_pose.py")
    with np.load(_FIXTURE) as dados:
        return {chave: dados[chave] for chave in dados.files}


def test_fixture_tem_caixa_dentro_e_caixa_fora_do_quadro(referencia):
    """Sem uma caixa que transborda, a borda 0 do warpAffine nunca é conferida
    contra o MMPose — e é ali que o preenchimento errado (cinza 114, ou 0
    depois da normalização) mora."""
    altura = int(referencia["altura"])
    largura = int(referencia["largura"])
    caixas = referencia["caixas"]
    assert len(caixas) >= 2
    dentro = fora = False
    for x1, y1, x2, y2 in caixas:
        if x1 >= 0 and y1 >= 0 and x2 <= largura and y2 <= altura:
            dentro = True
        if x1 < 0 or y1 < 0 or x2 > largura or y2 > altura:
            fora = True
    assert dentro and fora


def test_tensor_bate_com_o_topdown_affine(referencia):
    imagem = _frame(int(referencia["seed"]), int(referencia["altura"]), int(referencia["largura"]))
    linhas = referencia["amostra_y"]
    colunas = referencia["amostra_x"]

    for indice, caixa in enumerate(referencia["caixas"]):
        tensor, _ = crop_bbox(imagem, caixa)
        assert tensor.shape == (1, 3, INPUT_ALTURA, INPUT_LARGURA)
        amostra = np.stack(
            [tensor[0, :, int(y), int(x)] for y, x in zip(linhas, colunas, strict=True)]
        )
        if not np.array_equal(amostra, referencia["amostras"][indice]):
            diferenca = np.abs(amostra - referencia["amostras"][indice])
            ponto, canal = np.unravel_index(int(diferenca.argmax()), diferenca.shape)
            pytest.fail(
                f"caixa {indice} diverge no pixel "
                f"(y={int(linhas[ponto])}, x={int(colunas[ponto])}), canal {int(canal)}"
            )
        assert _checksum(tensor) == referencia["checksums"][indice]


def test_juntas_batem_com_o_simcc_label(referencia):
    imagem = _frame(int(referencia["seed"]), int(referencia["altura"]), int(referencia["largura"]))

    for indice, caixa in enumerate(referencia["caixas"]):
        _, recorte = crop_bbox(imagem, caixa)
        juntas = decode_simcc(referencia["simcc_x"][indice], referencia["simcc_y"][indice], recorte)
        assert len(juntas) == NUM_JUNTAS
        obtidas = np.array([(junta.x, junta.y) for junta in juntas], dtype=np.float32)
        confiancas = np.array([junta.confianca for junta in juntas], dtype=np.float32)
        assert obtidas == pytest.approx(referencia["juntas"][indice], abs=_ATOL_JUNTA)
        assert confiancas == pytest.approx(referencia["confiancas"][indice], abs=_ATOL_CONFIANCA)
        # O pulso esquerdo (canal 9) não pode cair no mesmo ponto nas três
        # caixas: a inversa que ignora a caixa devolve o recorte cru.
        if indice:
            assert obtidas[9].tolist() != pytest.approx(referencia["juntas"][0, 9], abs=1.0)


def test_recorte_da_fixture_nao_e_o_quadrado_do_detector(referencia):
    """O letterbox 640 do YOLOX, aplicado aqui, produziria outro checksum.
    Este teste só documenta o tamanho que a referência exige."""
    imagem = _frame(int(referencia["seed"]), int(referencia["altura"]), int(referencia["largura"]))
    _, recorte = crop_bbox(imagem, referencia["caixas"][0])
    assert isinstance(recorte, Crop)
    assert (recorte.input_width, recorte.input_height) == (INPUT_LARGURA, INPUT_ALTURA)
    assert referencia["centros"].shape[0] == len(referencia["caixas"])
