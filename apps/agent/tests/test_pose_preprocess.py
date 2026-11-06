"""Frame da câmera → recorte do RTMPose → junta de volta.

Nenhum erro deste módulo estoura em produção. Letterbox do YOLOX, canal BGR
ou média 0..1 produzem um tensor válido, o modelo devolve juntas plausíveis, e
elas caem fora do corpo. O sintoma aparece no gesto de ocultação, como pulso
no lugar errado (o mesmo fracasso silencioso de `detect/preprocess.py`).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from lince_agent.config import PixelFormat
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.preprocess import (
    INPUT_ALTURA,
    INPUT_LARGURA,
    RTMPOSE,
    Crop,
    PreprocessError,
    PreprocessSpec,
    crop_bbox,
    frame_to_bgr,
    to_crop,
    undo_crop,
)

LARGURA, ALTURA = 640, 480
# Caixa 100×200 em (100, 50). Com padding 1.25 e razão 192/256 o recorte fica
# com centro (150, 150) e escala (187.5, 250) — números diádicos, para a ida
# e a volta serem iguais de verdade e não "quase".
CAIXA = (100.0, 50.0, 200.0, 250.0)


def imagem(height: int = ALTURA, width: int = LARGURA) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


def frame(pixel_format: PixelFormat = PixelFormat.BGR24) -> Frame:
    return Frame(
        data=bytes(pixel_format.frame_bytes(LARGURA, ALTURA)),
        width=LARGURA,
        height=ALTURA,
        pixel_format=pixel_format,
        sequence=1,
        received_at=0.0,
    )


def normalizado(pixel: float, media: float, desvio: float) -> float:
    """A conta do PoseDataPreprocessor, em float32, que é o dtype do tensor."""
    return float((np.float32(pixel) - np.float32(media)) / np.float32(desvio))


# --- o frame --------------------------------------------------------------------


def test_bgr24_atravessa_sem_conversao():
    assert frame_to_bgr(frame()).shape == (ALTURA, LARGURA, 3)


@pytest.mark.parametrize("pixel_format", [PixelFormat.NV12, PixelFormat.YUV420P])
def test_planar_recusado_apontando_o_conserto(pixel_format: PixelFormat):
    """NV12 é o que o `HwAccel.CUDA_GPU_FILTER` entrega. Recusar sem dizer onde
    arrumar mandaria alguém escrever conversão de 4:2:0 em NumPy, que custa por
    frame o que o ffmpeg faz de graça no filtro."""
    with pytest.raises(PreprocessError, match="format=bgr24"):
        frame_to_bgr(frame(pixel_format))


def test_recorte_aceita_a_vista_somente_leitura_do_pipe():
    """`Frame.as_array` é uma vista sobre `bytes`, e vista sobre `bytes` não
    aceita escrita. Um recorte que preenchesse a moldura no próprio frame
    morreria na primeira câmera."""
    array = frame_to_bgr(frame())
    assert array.flags.writeable is False
    tensor, _ = crop_bbox(array, CAIXA)
    assert tensor.shape == (1, 3, INPUT_ALTURA, INPUT_LARGURA)


# --- a geometria do recorte -----------------------------------------------------


def test_caixa_de_referencia_tem_padding_e_razao_do_rtmpose():
    """Padding 1.25 e razão 192/256, nessa ordem. Sem o padding a pessoa entra
    cortada no queixo; sem a razão o corpo é esticado e o pulso troca de altura
    com o ombro. Os dois erros devolvem juntas plausíveis."""
    _, recorte = crop_bbox(imagem(), CAIXA)
    assert (recorte.center_x, recorte.center_y) == pytest.approx((150.0, 150.0))
    assert (recorte.scale_w, recorte.scale_h) == pytest.approx((187.5, 250.0))
    assert recorte.scale_w / recorte.scale_h == pytest.approx(INPUT_LARGURA / INPUT_ALTURA)
    assert (recorte.input_width, recorte.input_height) == (INPUT_LARGURA, INPUT_ALTURA)
    assert (recorte.source_width, recorte.source_height) == (LARGURA, ALTURA)


@pytest.mark.parametrize(
    "caixa",
    [(10.0, 10.0, 400.0, 80.0), (10.0, 10.0, 40.0, 400.0)],
    ids=["larga", "alta"],
)
def test_razao_do_recorte_nao_depende_da_caixa(caixa: tuple[float, float, float, float]):
    _, recorte = crop_bbox(imagem(), caixa)
    assert recorte.scale_w / recorte.scale_h == pytest.approx(192 / 256)
    assert recorte.scale_w > 0 and recorte.scale_h > 0


def test_tensor_sai_nchw_256_por_192_contiguo_e_float32():
    """O modelo é 256 de altura por 192 de largura. Trocar os lados produz um
    tensor válido — e juntas com x e y invertidos, o pulso na altura errada."""
    tensor, _ = crop_bbox(imagem(), CAIXA)
    assert tensor.shape == (1, 3, 256, 192)
    assert tensor.dtype == np.float32
    assert tensor.flags["C_CONTIGUOUS"]
    assert tensor[0].flags["C_CONTIGUOUS"]


def test_ida_e_volta_devolve_a_junta_original():
    """O teste que guarda o módulo: junta conhecida no frame, levada ao recorte
    e trazida de volta, é a mesma junta. O centro da caixa cai no centro do
    recorte (96, 128); o canto do recorte cai em (56.25, 25), que é
    centro − escala/2."""
    _, recorte = crop_bbox(imagem(), CAIXA)
    original = np.array([[150.0, 150.0], [56.25, 25.0]], dtype=np.float32)

    no_recorte = to_crop(original, recorte)
    assert no_recorte[0] == pytest.approx((96.0, 128.0))
    assert no_recorte[1] == pytest.approx((0.0, 0.0))
    assert undo_crop(no_recorte, recorte) == pytest.approx(original)


def test_a_volta_nao_altera_a_entrada():
    """O mesmo array de picos segue para a confiança. Desfazer o recorte no
    lugar deixaria o resto do estágio olhando coordenada já convertida."""
    recorte = Crop(
        center_x=150.0,
        center_y=150.0,
        scale_w=187.5,
        scale_h=250.0,
        input_width=INPUT_LARGURA,
        input_height=INPUT_ALTURA,
        source_width=LARGURA,
        source_height=ALTURA,
    )
    entrada = np.array([[96.0, 128.0]], dtype=np.float32)
    undo_crop(entrada, recorte)
    assert entrada.tolist() == [[96.0, 128.0]]


def test_pontos_com_forma_errada_sao_recusados():
    recorte = crop_bbox(imagem(), CAIXA)[1]
    with pytest.raises(PreprocessError, match=r"\(N, 2\)"):
        undo_crop(np.zeros((3, 3), dtype=np.float32), recorte)


# --- canal e normalização -------------------------------------------------------


def test_bgr_vira_rgb_e_normaliza_com_a_media_do_rtmpose():
    """Pixel BGR (10, 20, 30) no centro da caixa. O canal 0 do tensor é o R
    (30) menos 123.675, sobre 58.395 — a média do ImageNet em 0..255, ordem
    RGB, da config `rtmpose-s_8xb256-420e_coco-256x192.py`. Canal trocado ou
    média 0..1 passa um tensor válido e desloca a junta."""
    img = imagem()
    img[80, 100] = (10, 20, 30)
    tensor, recorte = crop_bbox(img, (60.0, 0.0, 140.0, 160.0))
    assert (recorte.center_x, recorte.center_y) == pytest.approx((100.0, 80.0))

    u, v = INPUT_LARGURA // 2, INPUT_ALTURA // 2
    assert tensor[0, 0, v, u] == pytest.approx(normalizado(30, 123.675, 58.395))
    assert tensor[0, 1, v, u] == pytest.approx(normalizado(20, 116.28, 57.12))
    assert tensor[0, 2, v, u] == pytest.approx(normalizado(10, 103.53, 57.375))
    assert RTMPOSE.mean_rgb == (123.675, 116.28, 103.53)
    assert RTMPOSE.std_rgb == (58.395, 57.12, 57.375)


def test_fora_do_frame_e_preto_normalizado_nao_cinza_de_yolo():
    """Pessoa na borda: o retângulo do recorte sai do quadro. O treino preenche
    com 0 (`warpAffine`) e só então normaliza. Preto normalizado não é o
    número 0, nem o cinza 114 do letterbox do YOLOX — os dois entregam à rede
    uma borda que ela não viu."""
    img = np.full((ALTURA, LARGURA, 3), 255, dtype=np.uint8)
    tensor, _ = crop_bbox(img, (0.0, 0.0, 40.0, 80.0))

    centro = tensor[0, :, INPUT_ALTURA // 2, INPUT_LARGURA // 2]
    canto = tensor[0, :, 0, 0]
    branco = [
        normalizado(255, media, desvio)
        for media, desvio in zip(RTMPOSE.mean_rgb, RTMPOSE.std_rgb, strict=True)
    ]
    preto = [
        normalizado(0, media, desvio)
        for media, desvio in zip(RTMPOSE.mean_rgb, RTMPOSE.std_rgb, strict=True)
    ]
    assert centro == pytest.approx(branco)
    assert canto == pytest.approx(preto)
    assert canto[0] != pytest.approx(0.0)
    assert canto[0] != pytest.approx(normalizado(114, 123.675, 58.395))


@pytest.mark.parametrize(
    "caixa",
    [
        (0.0, 0.0, 30.0, 80.0),
        (610.0, 400.0, 639.0, 479.0),
        (-20.0, -10.0, 25.0, 70.0),
        (620.0, -5.0, 700.0, 40.0),
    ],
    ids=[
        "canto_superior_esquerdo",
        "canto_inferior_direito",
        "sai_pela_esquerda",
        "sai_pela_direita",
    ],
)
def test_caixa_na_borda_do_frame_ainda_recorte(caixa: tuple[float, float, float, float]):
    """A caixa do detector, depois do `undo_letterbox`, encosta e às vezes
    passa da moldura. Recusar aqui derrubaria a pose de quem está saindo;
    ignorar o transbordo leria pixel de outro lugar da memória."""
    tensor, recorte = crop_bbox(imagem(), caixa)
    assert tensor.shape == (1, 3, INPUT_ALTURA, INPUT_LARGURA)
    assert np.isfinite(tensor).all()
    junta = np.array([[recorte.center_x, recorte.center_y]], dtype=np.float32)
    assert undo_crop(to_crop(junta, recorte), recorte) == pytest.approx(junta)


# --- as recusas -----------------------------------------------------------------


@pytest.mark.parametrize(
    "caixa",
    [
        (10.0, 10.0, 10.0, 40.0),
        (10.0, 40.0, 30.0, 10.0),
        (5.0, 5.0, 4.0, 20.0),
        (10.0, 10.0, 30.0, 10.0),
    ],
    ids=["largura_zero", "invertida", "largura_negativa", "altura_zero"],
)
def test_caixa_degenerada_e_recusada(caixa: tuple[float, float, float, float]):
    """Sem área não há escala. Seguir em frente divide por zero e espalha as
    17 juntas por qualquer lugar do quadro, com confiança de quem acertou."""
    with pytest.raises(PreprocessError, match="degenerada"):
        crop_bbox(imagem(), caixa)


def test_caixa_nao_finita_e_recusada():
    with pytest.raises(PreprocessError, match="não finita"):
        crop_bbox(imagem(), (1.0, 2.0, math.nan, 40.0))


def test_imagem_sem_tres_canais_e_recusada():
    with pytest.raises(PreprocessError, match=r"\(altura, largura, 3\)"):
        crop_bbox(np.zeros((ALTURA, LARGURA), dtype=np.uint8), CAIXA)


def test_imagem_float_e_recusada():
    """A média oficial está em 0..255. Um float 0..1 normalizado com ela vira
    quase preto e a junta sai deslocada, sem erro."""
    with pytest.raises(PreprocessError, match="0..255"):
        crop_bbox(np.zeros((ALTURA, LARGURA, 3), dtype=np.float32), CAIXA)


def test_caixa_com_forma_errada_e_recusada():
    with pytest.raises(PreprocessError, match="x1, y1, x2, y2"):
        crop_bbox(imagem(), (1.0, 2.0, 3.0))


def test_caixa_numa_linha_numpy_e_aceita():
    """O detector guarda a caixa como linha `(1, 4)`. Recusar essa forma
    obrigaria cada chamada a achatar na mão, e alguém ia achatar o lote."""
    tensor, recorte = crop_bbox(imagem(), np.array([CAIXA], dtype=np.float64))
    assert tensor.shape == (1, 3, INPUT_ALTURA, INPUT_LARGURA)
    assert (recorte.center_x, recorte.center_y) == pytest.approx((150.0, 150.0))


def test_sem_pontos_devolve_forma_utilizavel():
    """Quem empilha juntas de várias pessoas não pode estourar no frame em que
    uma caixa foi descartada antes do decode."""
    recorte = crop_bbox(imagem(), CAIXA)[1]
    vazio = undo_crop(np.zeros((0, 2), dtype=np.float32), recorte)
    assert vazio.shape == (0, 2)
    assert vazio.dtype == np.float32


def test_imagem_vazia_e_recusada():
    with pytest.raises(PreprocessError, match="sem pixels"):
        crop_bbox(np.zeros((0, LARGURA, 3), dtype=np.uint8), CAIXA)


def test_padding_nao_positivo_e_recusado():
    """Padding 1.25 é o default do `GetBBoxCenterScale`. Zero devolve a caixa
    crua, que o treino nunca viu, e a junta sai colada na borda do corpo."""
    with pytest.raises(PreprocessError, match="padding"):
        crop_bbox(imagem(), CAIXA, spec=PreprocessSpec(padding=0.0))
