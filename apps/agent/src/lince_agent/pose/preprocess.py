"""Do frame da câmera ao tensor do RTMPose, e das juntas de volta.

**Isto não é o letterbox do YOLOX.** A pose é top-down: entra o recorte da caixa
de uma pessoa, não o frame inteiro. Reaproveitar `detect.preprocess.letterbox`
aqui — canto superior esquerdo, BGR, 0..255, quadrado 640 — produz um tensor
válido, o modelo devolve 17 juntas plausíveis, e elas caem fora do corpo. O
sintoma aparece no gesto de ocultação, como pulso no lugar errado, sem uma
exceção em lugar nenhum. É o mesmo fracasso silencioso de `detect/preprocess.py`.

A convenção é a da config oficial do RTMPose-s COCO-256×192 (MMPose, Apache-2.0),
arquivo `configs/body_2d_keypoint/rtmpose/coco/rtmpose-s_8xb256-420e_coco-256x192.py`:

- `GetBBoxCenterScale` sem argumento na `val_pipeline`. O default do MMPose
  (`mmpose/datasets/transforms/common_transforms.py`) é padding **1.25**.
- `TopdownAffine` com `input_size=(192, 256)` — largura × altura — e `use_udp`
  no default, que é **False**. UDP deslocaria cada junta meio pixel e o config
  não liga isso.
- A razão do recorte é fixada em 192/256 antes do warp
  (`TopdownAffine._fix_aspect_ratio`). Sem isso o corpo é esticado e o SimCC
  lê o pulso na altura do ombro.
- `PoseDataPreprocessor` com `bgr_to_rgb=True` e a média/desvio do ImageNet **já
  na escala 0..255** — o preprocessor não divide o pixel por 255:

      mean = [123.675, 116.28, 103.53]   # 0.485, 0.456, 0.406 × 255, ordem RGB
      std  = [58.395, 57.12, 57.375]      # 0.229, 0.224, 0.225 × 255

  Errar o canal ou aplicar a média 0..1 em pixel 0..255 não estoura: sai junta
  deslocada.

O warp em si, com rotação zero, é o `get_warp_matrix` do MMPose
(`mmpose/structures/bbox/transforms.py`) depois da razão já corrigida: o pixel
`(u, v)` do recorte 192×256 cai em

    x = centro_x + (u - 192/2) * (escala_w / 192)
    y = centro_y + (v - 256/2) * (escala_h / 256)

que é a mesma conta do pós-processamento dos exemplos ONNX do RTMPose
(`ponto / input_size * escala + centro - escala/2`). A borda fora do frame é
0, o `borderValue` do `warpAffine` — não o cinza 114 do YOLOX, que este modelo
não viu no treino. O 0 entra **antes** da normalização: preto normalizado não
é o número zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from lince_agent.config import PixelFormat
from lince_agent.ffmpeg.rawframe import Frame

# PoseDataPreprocessor da config citada no docstring do módulo. Ordem RGB,
# escala 0..255. Ficam no spec, não enterrados na conta, pelo mesmo motivo
# que `PreprocessSpec` existe no detector: errar isto não dá erro.
MEAN_RGB = (123.675, 116.28, 103.53)
STD_RGB = (58.395, 57.12, 57.375)

INPUT_LARGURA = 192
"""Largura do recorte. O tensor sai 1×3×256×192: no NCHW a altura vem antes."""

INPUT_ALTURA = 256

PADDING = 1.25
"""`GetBBoxCenterScale` default. A `val_pipeline` do RTMPose-s não sobrescreve."""


@dataclass(frozen=True, slots=True)
class PreprocessSpec:
    """A convenção de entrada do RTMPose-s COCO-17.

    Existe para a troca de modelo não herdar o letterbox do YOLOX em silêncio.
    Os defaults são os da config oficial citada no docstring do módulo; não são
    "valores razoáveis de ImageNet" escolhidos aqui.
    """

    input_width: int = INPUT_LARGURA
    input_height: int = INPUT_ALTURA
    padding: float = PADDING
    mean_rgb: tuple[float, float, float] = MEAN_RGB
    std_rgb: tuple[float, float, float] = STD_RGB


RTMPOSE = PreprocessSpec()
"""O único spec do piloto: RTMPose-s COCO-17, Apache-2.0, exportado do MMPose.

WholeBody (133 juntas) e Halpe (136) não cabem aqui — outra contagem, outro
índice de pulso. Pesos da Ultralytics continuam AGPL-3.0, a mesma trava do
detector (ADR-009)."""


@dataclass(frozen=True, slots=True)
class Crop:
    """A transformação aplicada, guardada para poder ser desfeita.

    Viaja junto do tensor. Remontar centro e escala depois é como o
    deslocamento entra: a junta cai fora da pessoa e o gesto lê o pulso no
    lugar errado, sem erro nenhum. `scale_w`/`scale_h` já incluem o padding
    1.25 e a razão 192/256.
    """

    center_x: float
    center_y: float
    scale_w: float
    scale_h: float
    input_width: int
    input_height: int
    source_width: int
    source_height: int


class PreprocessError(ValueError):
    """Frame ou caixa que não pode virar entrada da pose. Sempre com o que fazer."""


def frame_to_bgr(frame: Frame) -> np.ndarray:
    """Extrai a matriz BGR de um `Frame`, recusando o que não sabe converter.

    A mesma recusa do detector: NV12 e YUV420P chegam quando o box roda
    `HwAccel.CUDA_GPU_FILTER`. Converter planar 4:2:0 em NumPy custaria por
    frame o que o `format` do ffmpeg faz de graça — a recusa aponta para lá.
    """
    if frame.pixel_format is not PixelFormat.BGR24:
        raise PreprocessError(
            f"a pose consome {PixelFormat.BGR24}, e este frame é {frame.pixel_format}. "
            "Acrescente `format=bgr24` ao fim da cadeia de filtros do ffmpeg "
            "(ffmpeg/command.py) ou configure DecodeOptions.pixel_format"
        )
    return frame.as_array()


def plano_crop(
    image: np.ndarray,
    bbox: np.ndarray | Sequence[float],
    *,
    spec: PreprocessSpec = RTMPOSE,
) -> Crop:
    """Centro e escala do recorte, sem amostrar pixel.

    `crop_bbox` amostra em cima deste plano, e `undo_crop` desfaz o mesmo
    plano. Recalcular o centro em outro lugar é como o deslocamento entra: a
    junta volta para um retângulo que não foi o recortado.
    """
    _valida_spec(spec)
    _valida_imagem(image)
    x1, y1, x2, y2 = _caixa_xyxy(bbox)

    center_x = (x1 + x2) * 0.5
    center_y = (y1 + y2) * 0.5
    # bbox_xyxy2cs: escala é o tamanho da caixa vezes o padding, não o tamanho
    # já com a razão do modelo. A razão entra no passo seguinte.
    scale_w, scale_h = _fix_aspect_ratio(
        (x2 - x1) * spec.padding,
        (y2 - y1) * spec.padding,
        spec.input_width / spec.input_height,
    )
    return Crop(
        center_x=center_x,
        center_y=center_y,
        scale_w=scale_w,
        scale_h=scale_h,
        input_width=spec.input_width,
        input_height=spec.input_height,
        source_width=int(image.shape[1]),
        source_height=int(image.shape[0]),
    )


def crop_bbox(
    image: np.ndarray,
    bbox: np.ndarray | Sequence[float],
    *,
    spec: PreprocessSpec = RTMPOSE,
) -> tuple[np.ndarray, Crop]:
    """Recorta a caixa da pessoa e devolve o tensor NCHW float32 e o `Crop`.

    `bbox` é `(x1, y1, x2, y2)` no frame BGR do ffmpeg, o mesmo espaço das
    caixas do detector depois do `undo_letterbox`. A caixa pode transbordar o
    frame — pessoa encostada na borda — e o que falta é preenchido com 0 antes
    da normalização, como o `warpAffine` do treino.
    """
    crop = plano_crop(image, bbox, spec=spec)

    # `image` pode ser a vista somente leitura do pipe (`Frame.as_array`).
    # O warp aloca o destino; escrever no frame estouraria na primeira câmera.
    recortado = _warp_affine(image, crop)
    rgb = recortado[:, :, ::-1]
    media = np.asarray(spec.mean_rgb, dtype=np.float32).reshape(1, 1, 3)
    desvio = np.asarray(spec.std_rgb, dtype=np.float32).reshape(1, 1, 3)
    normalizado = (rgb.astype(np.float32) - media) / desvio

    # NCHW contíguo: `transpose` só troca strides, e uma sessão ONNX alimentada
    # com array não contíguo copia por baixo dos panos a cada inferência.
    tensor = np.ascontiguousarray(normalizado.transpose(2, 0, 1)[np.newaxis, ...])
    return tensor, crop


def to_crop(points: np.ndarray, crop: Crop) -> np.ndarray:
    """Leva pontos `(N, 2)` do frame da câmera para o espaço do recorte.

    O par de `undo_crop`: junta conhecida → recorte → de volta tem que ser a
    mesma junta. Sem os dois lados, um deslocamento de padding passa no teste
    que só olha a ida.
    """
    _valida_crop(crop)
    xs, ys = _pontos(points)
    largura = crop.input_width
    altura = crop.input_height
    saida_u = (xs - crop.center_x) * (largura / crop.scale_w) + largura / 2.0
    saida_v = (ys - crop.center_y) * (altura / crop.scale_h) + altura / 2.0
    return np.stack((saida_u, saida_v), axis=1).astype(np.float32)


def undo_crop(points: np.ndarray, crop: Crop) -> np.ndarray:
    """Leva pontos `(N, 2)` do recorte 192×256 de volta ao frame da câmera.

    Não recorta na moldura, ao contrário de `undo_letterbox`. A caixa da
    detecção é recortada porque coordenada negativa atravessa até o polígono
    do §3.4; a junta não. Pulso estimado um pixel além da borda ainda é aquele
    pulso — trazê-lo para a moldura muda o gesto.
    """
    _valida_crop(crop)
    u, v = _pontos(points)
    largura = crop.input_width
    altura = crop.input_height
    x = crop.center_x + (u - largura / 2.0) * (crop.scale_w / largura)
    y = crop.center_y + (v - altura / 2.0) * (crop.scale_h / altura)
    return np.stack((x, y), axis=1).astype(np.float32)


def _valida_spec(spec: PreprocessSpec) -> None:
    if spec.input_width <= 0 or spec.input_height <= 0:
        raise PreprocessError(
            f"o recorte do RTMPose é {INPUT_LARGURA}×{INPUT_ALTURA} (largura×altura); "
            f"recebi {spec.input_width}×{spec.input_height}"
        )
    if spec.padding <= 0:
        raise PreprocessError(
            f"padding da caixa deve ser positivo (o default do MMPose é {PADDING}), "
            f"recebi {spec.padding}"
        )
    if len(spec.mean_rgb) != 3 or len(spec.std_rgb) != 3:
        raise PreprocessError(
            "mean/std do RTMPose têm 3 canais, na ordem RGB, como o "
            "PoseDataPreprocessor da config oficial"
        )
    if any(valor <= 0 for valor in spec.std_rgb):
        raise PreprocessError(f"std do RTMPose tem que ser positivo, recebi {spec.std_rgb}")


def _valida_imagem(image: np.ndarray) -> None:
    if image.ndim != 3 or image.shape[2] != 3:
        raise PreprocessError(f"esperava uma imagem (altura, largura, 3), recebi {image.shape}")
    if image.shape[0] == 0 or image.shape[1] == 0:
        raise PreprocessError(f"imagem sem pixels, recebi {image.shape}")
    if image.dtype != np.uint8:
        raise PreprocessError(
            f"esperava BGR uint8 do ffmpeg (0..255), recebi {image.dtype}. "
            "A média do RTMPose está na escala 0..255; normalizar um float 0..1 "
            "com ela produz juntas plausíveis e deslocadas"
        )


def _caixa_xyxy(bbox: np.ndarray | Sequence[float]) -> tuple[float, float, float, float]:
    caixa = np.asarray(bbox, dtype=np.float64)
    if caixa.shape == (1, 4):
        caixa = caixa.reshape(4)
    if caixa.shape != (4,):
        raise PreprocessError(f"esperava uma caixa (x1, y1, x2, y2), recebi {caixa.shape}")
    x1, y1, x2, y2 = (float(valor) for valor in caixa)
    if not all(np.isfinite(valor) for valor in (x1, y1, x2, y2)):
        raise PreprocessError(
            f"caixa com coordenada não finita ({x1}, {y1}, {x2}, {y2}). "
            "Não dá para recortar NaN: o tensor sairia cheio de lixo e as juntas também"
        )
    if x2 <= x1 or y2 <= y1:
        raise PreprocessError(
            f"caixa degenerada (x1={x1}, y1={y1}, x2={x2}, y2={y2}): "
            "largura e altura precisam ser positivas. Sem área não há recorte, "
            "e seguir em frente divide por escala zero e espalha as juntas"
        )
    return x1, y1, x2, y2


def _fix_aspect_ratio(largura: float, altura: float, aspecto: float) -> tuple[float, float]:
    """A mesma regra de `TopdownAffine._fix_aspect_ratio`.

    `aspecto` é largura/altura do modelo (192/256). Se a caixa é larga demais,
    a altura cresce; se é alta demais, a largura cresce. Nunca encolhe — o
    treino viu a pessoa inteira, com folga, não um recorte apertado.
    """
    if largura > altura * aspecto:
        return largura, largura / aspecto
    return altura * aspecto, altura


def _valida_crop(crop: Crop) -> None:
    if crop.scale_w <= 0 or crop.scale_h <= 0 or crop.input_width <= 0 or crop.input_height <= 0:
        raise PreprocessError(
            "recorte sem escala positiva não tem inversa: a junta não volta para o frame"
        )


def _pontos(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if points.size == 0:
        vazio = np.zeros((0,), dtype=np.float64)
        return vazio, vazio
    if points.ndim != 2 or points.shape[1] != 2:
        raise PreprocessError(f"esperava pontos (N, 2), recebi {points.shape}")
    copiados = points.astype(np.float64, copy=True)
    return copiados[:, 0], copiados[:, 1]


def _warp_affine(image: np.ndarray, crop: Crop) -> np.ndarray:
    """Amostra o frame no retângulo do recorte, com borda constante 0.

    É o `cv2.warpAffine` do `TopdownAffine` (INTER_LINEAR, borderValue 0), a
    mesma chamada que gerou os pesos. O piloto amostrava em NumPy, imitando a
    quantização do OpenCV, e o benchmark na RTX 3060 mediu o preço: 6,5 ms por
    pessoa contra 1,1 ms, e com 3 pessoas o recorte era 80% do frame. Com 15
    câmeras a 3 fps, uma thread de pose não fechava a conta.

    A matriz leva o pixel do recorte ao pixel do frame, por isso
    `WARP_INVERSE_MAP`: sem ele o OpenCV inverte de novo e amostra outro
    retângulo. Dá o mesmo resultado que a matriz direta do `get_warp_matrix`
    do MMPose, sem a inversão a cada chamada.

    **A versão do OpenCV é parte do contrato.** O 5.0 amostra diferente do 4.x
    em até 7 níveis de cinza num terço dos pixels: o tensor deixa de ser o do
    treino e a junta continua parecendo certa. `pyproject.toml` fixa `<5`, e
    `tests/test_pose_referencia.py` compara o tensor com o do MMPose pelo sha256.
    """
    # Import adiado: o runtime importa a pose mesmo com ela desligada, que é o padrão,
    # e um agente sem pose não deve depender de o OpenCV carregar para subir.
    import cv2

    largura = crop.input_width
    altura = crop.input_height
    escala_x = crop.scale_w / largura
    escala_y = crop.scale_h / altura
    matriz = np.array(
        [
            [escala_x, 0.0, crop.center_x - (largura / 2.0) * escala_x],
            [0.0, escala_y, crop.center_y - (altura / 2.0) * escala_y],
        ],
        dtype=np.float64,
    )
    return cv2.warpAffine(
        image,
        matriz,
        (largura, altura),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
