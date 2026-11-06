"""Decode SimCC, sem modelo e sem ONNX Runtime.

O que quebra em silêncio aqui é a junta cair no lugar errado com confiança de
acerto: esquecer o split ratio 2.0, projetar o sentinela -1 do MMPose, ou ler
133 canais de um WholeBody como se fossem os 17 do COCO. Nenhum desses casos
levanta exceção dentro do modelo — o tensor é válido. Por isso o teste constrói
o SimCC à mão, com o pico num bin conhecido.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from lince_agent.config import DetectionOptions
from lince_agent.detect.onnx import model_version as versao_do_detector
from lince_agent.pose.onnx import (
    JUNTAS_COCO17,
    NUM_JUNTAS,
    PROVIDERS,
    SPLIT_RATIO,
    DecodeError,
    ModelError,
    decode_simcc,
    model_version,
    recusa_modelo_fora_do_piloto,
)
from lince_agent.pose.preprocess import INPUT_ALTURA, INPUT_LARGURA, Crop

# O mesmo recorte da caixa de referência de test_pose_preprocess: centro
# (150, 150), escala (187.5, 250). Bin (192, 256) / split 2.0 = (96, 128),
# o centro do recorte, que volta para (150, 150). Bin (0, 0) volta para
# (56.25, 25), o canto centro − escala/2.
RECORTE = Crop(
    center_x=150.0,
    center_y=150.0,
    scale_w=187.5,
    scale_h=250.0,
    input_width=INPUT_LARGURA,
    input_height=INPUT_ALTURA,
    source_width=640,
    source_height=480,
)
BINS_X = int(INPUT_LARGURA * SPLIT_RATIO)
BINS_Y = int(INPUT_ALTURA * SPLIT_RATIO)


def simcc(com_lote: bool = True) -> tuple[np.ndarray, np.ndarray]:
    forma_x = (1, NUM_JUNTAS, BINS_X) if com_lote else (NUM_JUNTAS, BINS_X)
    forma_y = (1, NUM_JUNTAS, BINS_Y) if com_lote else (NUM_JUNTAS, BINS_Y)
    return np.zeros(forma_x, dtype=np.float32), np.zeros(forma_y, dtype=np.float32)


def _escreve(eixo: np.ndarray, junta: int, bin_: int, valor: float) -> None:
    if eixo.ndim == 3:
        eixo[0, junta, bin_] = valor
    else:
        eixo[junta, bin_] = valor


# --- o pico ---------------------------------------------------------------------


@pytest.mark.parametrize("com_lote", [True, False], ids=["lote_1", "sem_lote"])
def test_pico_conhecido_cai_na_junta_do_frame(com_lote: bool):
    """Bin (192, 256) com split ratio 2.0 é o centro do recorte, e o centro do
    recorte desta caixa é o ponto (150, 150) do frame. Esquecer de dividir por
    2.0 — ou de desfazer o recorte — entrega o pulso dezenas de pixels fora,
    com confiança alta."""
    simcc_x, simcc_y = simcc(com_lote)
    _escreve(simcc_x, 9, 192, 0.8)
    _escreve(simcc_y, 9, 256, 0.3)
    copia_x = simcc_x.copy()

    juntas = decode_simcc(simcc_x, simcc_y, RECORTE)

    assert len(juntas) == NUM_JUNTAS
    pulso = juntas[9]
    assert pulso.indice == 9
    assert pulso.nome == "pulso_esquerdo"
    assert (pulso.x, pulso.y) == pytest.approx((150.0, 150.0))
    assert pulso.confianca == pytest.approx(0.3)
    # Junta sem pico nenhum não pode herdar o bin 0, que é um canto plausível.
    assert math.isnan(juntas[0].x) and math.isnan(juntas[0].y)
    assert juntas[0].confianca == 0.0
    assert np.array_equal(simcc_x, copia_x)


def test_dois_picos_nao_trocam_de_junta():
    """O pulso esquerdo é o canal 9 e o direito é o 10, na ordem COCO. Trocar
    os canais — o que um WholeBody faria sem avisar — põe o gesto na outra mão."""
    simcc_x, simcc_y = simcc()
    _escreve(simcc_x, 9, 192, 0.8)
    _escreve(simcc_y, 9, 256, 0.3)
    _escreve(simcc_x, 10, 0, 0.7)
    _escreve(simcc_y, 10, 0, 0.4)

    juntas = decode_simcc(simcc_x, simcc_y, RECORTE)

    assert (juntas[9].x, juntas[9].y, juntas[9].confianca) == pytest.approx((150.0, 150.0, 0.3))
    assert juntas[10].nome == "pulso_direito"
    assert (juntas[10].x, juntas[10].y, juntas[10].confianca) == pytest.approx((56.25, 25.0, 0.4))


def test_confianca_baixa_permanece_no_lugar_certo():
    """A confiança é o menor pico cru, não um softmax. 0.02 tem que continuar
    0.02: softmax de um único bin pequeno vira outra ordem de grandeza, e um
    limiar de gesto passaria a aceitar junta que o modelo mal viu — ou a
    descartar uma que ele viu."""
    simcc_x, simcc_y = simcc()
    _escreve(simcc_x, 9, 192, 0.05)
    _escreve(simcc_y, 9, 256, 0.02)

    pulso = decode_simcc(simcc_x, simcc_y, RECORTE)[9]

    assert pulso.confianca == pytest.approx(0.02)
    assert (pulso.x, pulso.y) == pytest.approx((150.0, 150.0))


def test_pico_nao_positivo_nao_vira_junta_no_canto():
    """O MMPose marca o pico ≤ 0 com o sentinela -1, ainda no espaço do SimCC.
    Projetar esse -1 com a inversa do recorte cria uma junta perto do canto,
    com cara de pulso. Aqui o pico menos ruim está no bin 0, que cairia em
    (56.25, 25) se alguém esquecesse o corte."""
    simcc_x, simcc_y = simcc()
    simcc_x[:] = -1.0
    simcc_y[:] = -1.0
    _escreve(simcc_x, 0, 0, -0.2)
    _escreve(simcc_y, 0, 0, -0.2)

    nariz = decode_simcc(simcc_x, simcc_y, RECORTE)[0]

    assert nariz.confianca == pytest.approx(-0.2)
    assert math.isnan(nariz.x) and math.isnan(nariz.y)


def test_pico_nao_finito_nao_ancora_junta():
    """NaN no vetor não pode virar o bin 0. `argmax` de NaN devolve um índice,
    e projetá-lo colocaria o pulso no canto do recorte com confiança NaN —
    que nenhum `if confianca < limiar` pega, porque NaN comparado é falso."""
    simcc_x, simcc_y = simcc()
    _escreve(simcc_x, 9, 0, math.nan)
    _escreve(simcc_y, 9, 0, math.nan)

    pulso = decode_simcc(simcc_x, simcc_y, RECORTE)[9]

    assert math.isnan(pulso.confianca)
    assert math.isnan(pulso.x) and math.isnan(pulso.y)


def test_eixo_zerado_nao_ancora_a_junta_no_outro():
    """Confiança é o mínimo dos dois eixos. x certo e y em zero é junta
    inexistente — usar só o x colocaria o pulso na altura do bin 0."""
    simcc_x, simcc_y = simcc()
    _escreve(simcc_x, 9, 192, 0.9)

    pulso = decode_simcc(simcc_x, simcc_y, RECORTE)[9]

    assert pulso.confianca == 0.0
    assert math.isnan(pulso.x) and math.isnan(pulso.y)


def test_ordem_coco_tem_dezessete_e_os_pulsos_nos_indices_certos():
    """WholeBody e Halpe reordenam e acrescentam junta. O gesto de ocultação
    lê o pulso; índice errado é a outra mão, ou o quadril, sem erro nenhum."""
    assert len(JUNTAS_COCO17) == NUM_JUNTAS == 17
    assert JUNTAS_COCO17[9] == "pulso_esquerdo"
    assert JUNTAS_COCO17[10] == "pulso_direito"
    assert len(set(JUNTAS_COCO17)) == NUM_JUNTAS


# --- o que não é SimCC deste modelo ---------------------------------------------


def test_wholebody_e_recusado_pela_contagem():
    simcc_x = np.zeros((1, 133, BINS_X), dtype=np.float32)
    simcc_y = np.zeros((1, 133, BINS_Y), dtype=np.float32)
    with pytest.raises(DecodeError, match="COCO-17"):
        decode_simcc(simcc_x, simcc_y, RECORTE)


def test_esquecer_o_split_ratio_e_recusado():
    """Vetor de 192 bins em vez de 384 é o split ratio 2.0 esquecido. Decodificar
    assim mesmo coloca a junta na metade do caminho."""
    simcc_x = np.zeros((1, NUM_JUNTAS, INPUT_LARGURA), dtype=np.float32)
    simcc_y = np.zeros((1, NUM_JUNTAS, INPUT_ALTURA), dtype=np.float32)
    with pytest.raises(DecodeError, match="2.0"):
        decode_simcc(simcc_x, simcc_y, RECORTE)


def test_lote_de_varias_pessoas_e_recusado():
    simcc_x = np.zeros((2, NUM_JUNTAS, BINS_X), dtype=np.float32)
    simcc_y = np.zeros((2, NUM_JUNTAS, BINS_Y), dtype=np.float32)
    with pytest.raises(DecodeError, match="uma pessoa"):
        decode_simcc(simcc_x, simcc_y, RECORTE)


def test_split_ratio_invalido_e_recusado():
    simcc_x, simcc_y = simcc()
    with pytest.raises(DecodeError, match="split"):
        decode_simcc(simcc_x, simcc_y, RECORTE, split_ratio=0.0)


# --- a sessão que ainda não abre ------------------------------------------------


def test_checksum_e_o_mesmo_mecanismo_do_detector(tmp_path: Path):
    """R-7: dois boxes com o mesmo nome de arquivo e pesos diferentes têm que
    divergir no heartbeat. Uma segunda receita de hash para a pose esconderia
    o rollout parcial justo no modelo novo."""
    caminho = tmp_path / "rtmpose-s.onnx"
    caminho.write_bytes(b"ainda nao e um onnx, e o hash nao liga para isso")
    assert model_version(caminho) == versao_do_detector(caminho)
    assert model_version(caminho).startswith("rtmpose-s@")
    assert len(model_version(caminho).split("@", 1)[1]) == 12


def test_providers_sao_os_do_detector():
    """CUDA primeiro, CPU se a placa cair (§10.10). Lista diferente faz a pose
    cair para CPU num box em que o detector usa a GPU, ou o contrário, e o
    heartbeat mente sobre qual dos dois pegou."""
    assert DetectionOptions().providers == PROVIDERS


@pytest.mark.parametrize(
    "nome",
    [
        "yolov8n-pose.onnx",
        "yolo11s-pose.onnx",
        "ultralytics-pose.onnx",
        "rtmpose-m_coco-wholebody.onnx",
        "rtmpose-s_halpe26.onnx",
        "whole-body.onnx",
    ],
)
def test_modelo_fora_do_piloto_e_recusado_pelo_nome(tmp_path: Path, nome: str):
    """Ultralytics é AGPL-3.0 (ADR-009). WholeBody e Halpe têm outra contagem
    de juntas. O arquivo não precisa existir: a recusa é anterior a abrir
    sessão, que nesta fase nem abre."""
    with pytest.raises(ModelError):
        recusa_modelo_fora_do_piloto(tmp_path / nome)


def test_rtmpose_s_coco_passa_na_trava(tmp_path: Path):
    recusa_modelo_fora_do_piloto(tmp_path / "rtmpose-s_8xb256-420e_coco-256x192.onnx")
