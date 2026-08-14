"""Frame da câmera → tensor do modelo → caixas de volta.

Nenhum erro deste módulo estoura em produção. Preenchimento no canto trocado, canal
invertido ou escala esquecida produzem tensor válido, o modelo devolve caixas
plausíveis, e elas caem fora do lugar. O sintoma aparece no §3.4, como zona errada, e
zona errada é falso positivo em massa (R-1). Daí o teste de ida e volta.
"""

from __future__ import annotations

import numpy as np
import pytest

from lince_agent.config import PixelFormat
from lince_agent.detect.preprocess import (
    PAD_VALUE,
    YOLOV8,
    YOLOX,
    Letterbox,
    PreprocessError,
    frame_to_bgr,
    letterbox,
    undo_letterbox,
)
from lince_agent.ffmpeg.rawframe import Frame

LARGURA, ALTURA, LADO = 640, 480, 640


def imagem(width: int = LARGURA, height: int = ALTURA) -> np.ndarray:
    """Gradiente, não cor chapada: um deslocamento de preenchimento some numa imagem
    uniforme e aparece numa que muda a cada pixel."""
    base = np.arange(height * width * 3, dtype=np.uint8).reshape(height, width, 3)
    return base


def frame(pixel_format: PixelFormat = PixelFormat.BGR24) -> Frame:
    return Frame(
        data=bytes(pixel_format.frame_bytes(LARGURA, ALTURA)),
        width=LARGURA,
        height=ALTURA,
        pixel_format=pixel_format,
        sequence=1,
        received_at=0.0,
    )


# --- a conversão do frame -------------------------------------------------------


def test_bgr24_atravessa_sem_conversao():
    assert frame_to_bgr(frame()).shape == (ALTURA, LARGURA, 3)


@pytest.mark.parametrize("pixel_format", [PixelFormat.NV12, PixelFormat.YUV420P])
def test_planar_recusado_apontando_o_conserto(pixel_format: PixelFormat):
    """NV12 é o que o `HwAccel.CUDA_GPU_FILTER` entrega, que é a configuração ótima do
    box. Recusar sem dizer onde arrumar mandaria alguém escrever conversão de 4:2:0 em
    NumPy, que custa por frame o que o ffmpeg faz de graça no filtro."""
    with pytest.raises(PreprocessError, match="format=bgr24"):
        frame_to_bgr(frame(pixel_format))


def test_letterbox_aceita_a_vista_somente_leitura_do_pipe():
    """`Frame.as_array` é uma vista sobre `bytes`, e vista sobre `bytes` não aceita
    escrita. Um letterbox que tentasse preencher a moldura no próprio frame morreria
    com `ValueError: assignment destination is read-only` na primeira câmera."""
    array = frame_to_bgr(frame())
    assert array.flags.writeable is False
    tensor, _ = letterbox(array, size=LADO)
    assert tensor.shape == (1, 3, LADO, LADO)


# --- a geometria ----------------------------------------------------------------


def test_caso_de_referencia_nao_redimensiona():
    """640x480 num modelo de 640: a escala dá exatamente 1.0 e o letterbox vira só
    160 linhas de cinza. É o que dispensa OpenCV do agente inteiro."""
    _, caixa = letterbox(imagem(), size=LADO)
    assert caixa.scale == 1.0
    assert caixa.source_width == LARGURA
    assert caixa.source_height == ALTURA


def test_yolox_encosta_no_canto_superior_esquerdo():
    """YOLOX preenche à direita e embaixo. Centralizar aqui desloca toda caixa em 80
    pixels no eixo Y — e o modelo não reclama."""
    tensor, caixa = letterbox(imagem(), size=LADO, spec=YOLOX)
    assert (caixa.pad_x, caixa.pad_y) == (0, 0)
    assert np.array_equal(tensor[0, :, :ALTURA, :LARGURA], imagem().transpose(2, 0, 1))
    assert np.all(tensor[0, :, ALTURA:, :] == PAD_VALUE)


def test_yolov8_centraliza():
    _, caixa = letterbox(imagem(), size=LADO, spec=YOLOV8)
    assert (caixa.pad_x, caixa.pad_y) == (0, (LADO - ALTURA) // 2)


def test_as_duas_convencoes_produzem_tensores_diferentes():
    """A prova de que "letterbox é letterbox" é falso. Trocar o modelo alvo (ADR-006)
    sem trocar a `PreprocessSpec` junto é perda de recall sem sintoma."""
    yolox, _ = letterbox(imagem(), size=LADO, spec=YOLOX)
    yolov8, _ = letterbox(imagem(), size=LADO, spec=YOLOV8)
    assert not np.allclose(yolox, yolov8 * 255.0)


def test_moldura_recebe_cinza_medio():
    """Preto daria uma borda de alto contraste que o modelo nunca viu no treino, e
    borda assim produz detecção fantasma na moldura."""
    tensor, _ = letterbox(imagem(), size=LADO)
    assert np.all(tensor[0, :, ALTURA:, :] == float(PAD_VALUE))


# --- canais e escala ------------------------------------------------------------


def test_yolox_mantem_bgr_e_a_faixa_0_255():
    """YOLOX foi treinado em BGR e embute a normalização no grafo. Trocar os canais
    "para ficar certo" é o erro de quem veio da linhagem Ultralytics."""
    img = np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)
    img[0, 0] = (10, 20, 30)  # B, G, R
    tensor, _ = letterbox(img, size=LADO, spec=YOLOX)
    assert tensor[0, 0, 0, 0] == 10.0
    assert tensor[0, 2, 0, 0] == 30.0


def test_yolov8_troca_para_rgb_e_normaliza():
    img = np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)
    img[0, 0] = (10, 20, 30)
    tensor, caixa = letterbox(img, size=LADO, spec=YOLOV8)
    assert tensor[0, 0, caixa.pad_y, caixa.pad_x] == pytest.approx(30 / 255)
    assert tensor[0, 2, caixa.pad_y, caixa.pad_x] == pytest.approx(10 / 255)


def test_tensor_sai_nchw_contiguo_e_float32():
    """Sessão ONNX alimentada com array não contíguo copia por baixo dos panos a cada
    inferência — 3 fps × 8 câmeras de cópia que ninguém pediu."""
    tensor, _ = letterbox(imagem(), size=LADO)
    assert tensor.shape == (1, 3, LADO, LADO)
    assert tensor.dtype == np.float32
    assert tensor[0].flags["C_CONTIGUOUS"]


# --- as recusas -----------------------------------------------------------------


def test_frame_maior_que_o_modelo_e_recusado_com_o_conserto():
    """1280x720 precisaria de redimensionamento. O conserto é no filtro do ffmpeg,
    que já tem um `scale` na cadeia e faz isso de graça (§3.1)."""
    with pytest.raises(PreprocessError, match="DecodeOptions"):
        letterbox(imagem(1280, 720), size=LADO)


def test_frame_menor_que_o_modelo_tambem_e_recusado():
    """Ampliar em silêncio entregaria ao modelo uma imagem borrada e um recall pior
    sem nenhum sinal de que foi isso."""
    with pytest.raises(PreprocessError, match="não cabe"):
        letterbox(imagem(320, 240), size=LADO)


def test_imagem_sem_tres_canais_e_recusada():
    with pytest.raises(PreprocessError, match=r"\(altura, largura, 3\)"):
        letterbox(np.zeros((ALTURA, LARGURA), dtype=np.uint8), size=LADO)


def test_size_nao_positivo_e_recusado():
    with pytest.raises(PreprocessError, match="size"):
        letterbox(imagem(), size=0)


# --- a volta --------------------------------------------------------------------


@pytest.mark.parametrize("spec", [YOLOX, YOLOV8], ids=["yolox", "yolov8"])
def test_ida_e_volta_devolve_a_caixa_original(spec):
    """O teste que guarda o módulo inteiro: uma caixa conhecida no frame da câmera,
    levada para o espaço do modelo e trazida de volta, tem que ser a mesma caixa."""
    _, caixa = letterbox(imagem(), size=LADO, spec=spec)
    original = np.array([[100.0, 50.0, 300.0, 400.0]], dtype=np.float32)

    no_modelo = original * caixa.scale
    no_modelo[:, 0::2] += caixa.pad_x
    no_modelo[:, 1::2] += caixa.pad_y

    assert undo_letterbox(no_modelo, caixa) == pytest.approx(original)


def test_caixa_que_transborda_e_recortada_na_moldura():
    """Pessoa encostada na borda sai com a caixa transbordando. Coordenada negativa
    atravessaria até o §3.4 testar se um ponto fora do frame está dentro de um
    polígono."""
    caixa = Letterbox(
        size=LADO, scale=1.0, pad_x=0, pad_y=0, source_width=LARGURA, source_height=ALTURA
    )
    recortada = undo_letterbox(np.array([[-30.0, -10.0, 900.0, 700.0]]), caixa)
    assert recortada.tolist() == [[0.0, 0.0, float(LARGURA), float(ALTURA)]]


def test_sem_caixas_devolve_forma_utilizavel():
    """Frame sem ninguém é o caso comum, não a exceção: a saída vazia precisa ter
    forma (0, 4) para quem for empilhar não estourar."""
    caixa = Letterbox(
        size=LADO, scale=1.0, pad_x=0, pad_y=0, source_width=LARGURA, source_height=ALTURA
    )
    vazio = undo_letterbox(np.zeros((0, 4), dtype=np.float32), caixa)
    assert vazio.shape == (0, 4)


def test_caixas_com_forma_errada_sao_recusadas():
    caixa = Letterbox(
        size=LADO, scale=1.0, pad_x=0, pad_y=0, source_width=LARGURA, source_height=ALTURA
    )
    with pytest.raises(PreprocessError, match=r"\(N, 4\)"):
        undo_letterbox(np.zeros((3, 5), dtype=np.float32), caixa)


def test_a_volta_nao_altera_a_entrada():
    """O mesmo array de caixas é usado depois para pontuar e filtrar. Desfazer o
    letterbox no lugar deixaria o resto do estágio olhando coordenadas já convertidas."""
    caixa = Letterbox(
        size=LADO, scale=1.0, pad_x=0, pad_y=80, source_width=LARGURA, source_height=ALTURA
    )
    entrada = np.array([[10.0, 90.0, 20.0, 100.0]], dtype=np.float32)
    undo_letterbox(entrada, caixa)
    assert entrada.tolist() == [[10.0, 90.0, 20.0, 100.0]]
