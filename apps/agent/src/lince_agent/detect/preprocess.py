"""Do frame da câmera ao tensor de entrada do modelo, e das caixas de volta.

**É aqui que mora o erro silencioso mais caro do estágio.** Nada nesta conversão
levanta exceção quando está errado: preenchimento no canto trocado, canal invertido ou
escala esquecida produzem um tensor perfeitamente válido, o modelo devolve caixas
perfeitamente plausíveis, e elas caem alguns pixels — ou algumas dezenas — fora do
lugar. O sintoma aparece três estágios depois, como zona errada no §3.4, que é falso
positivo que ninguém consegue explicar (R-1).

Por isso a convenção de cada modelo é dado explícito (`PreprocessSpec`) em vez de
constante embutida, e por isso o teste de ida e volta existe: caixa conhecida →
letterbox → `undo_letterbox` → a mesma caixa.

**Nenhum redimensionamento acontece aqui.** O ffmpeg do estágio 1 já tem um filtro
`scale` na cadeia (§3.1), então escalar de novo em Python seria pagar duas vezes por
um trabalho que a fonte faz melhor. O que sobra é preenchimento, e no caso de
referência — 640x480 para um modelo de 640 — a escala dá exatamente 1.0: são 160
linhas de cinza e mais nada.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from lince_agent.config import PixelFormat
from lince_agent.ffmpeg.rawframe import Frame

PAD_VALUE = 114
"""Cinza médio, a convenção que YOLOX e a família YOLO usam no treino. Preto (0) daria
uma borda de alto contraste que o modelo nunca viu e que produz detecção fantasma
na moldura."""


@dataclass(frozen=True, slots=True)
class PreprocessSpec:
    """A convenção de entrada de uma família de modelos.

    Existe porque **cada família faz diferente e nenhuma reclama quando você erra**.
    Trocar o modelo alvo (ADR-006) sem trocar isto junto é a maneira mais rápida de
    ter um agente que detecta pouco e não avisa.
    """

    centered: bool = False
    """Onde a imagem fica dentro do quadrado. YOLOX encosta no canto superior
    esquerdo e preenche à direita e embaixo; a linhagem YOLOv5/v8 centraliza."""

    to_rgb: bool = False
    """YOLOX foi treinado em BGR e consome o frame como o ffmpeg entrega. YOLOv8
    espera RGB — para ele, esquecer a troca custa recall sem sintoma nenhum."""

    normalize: bool = False
    """YOLOX embute a normalização no próprio grafo e recebe 0..255. YOLOv8 recebe
    0..1."""


YOLOX = PreprocessSpec()
"""O default do agente: Apache-2.0, sem o acoplamento AGPL da linhagem Ultralytics."""

YOLOV8 = PreprocessSpec(centered=True, to_rgb=True, normalize=True)
"""Aqui para o dia em que o modelo alvo mudar — e para o teste que prova que as duas
convenções produzem tensores diferentes, que é o que impede alguém de assumir que
"letterbox é letterbox"."""


@dataclass(frozen=True, slots=True)
class Letterbox:
    """A transformação aplicada, guardada para poder ser desfeita.

    Viaja junto do tensor porque desfazer com números remontados depois é como o
    deslocamento entra: basta uma câmera com outra resolução para a conta divergir.
    """

    size: int
    scale: float
    pad_x: int
    pad_y: int
    source_width: int
    source_height: int


class PreprocessError(ValueError):
    """Frame que não pode virar entrada de modelo. Sempre com o que fazer a respeito."""


def frame_to_bgr(frame: Frame) -> np.ndarray:
    """Extrai a matriz BGR de um `Frame`, recusando o que não sabe converter.

    NV12 e YUV420P chegam aqui quando o box roda `HwAccel.CUDA_GPU_FILTER`, que é a
    configuração ótima do §3.1 justamente por manter os frames na VRAM. Converter
    planar 4:2:0 para BGR em NumPy custaria por frame o que o `format` do ffmpeg faz
    de graça na cadeia de filtros — então a recusa aponta para lá em vez de embutir
    uma conversão cara e pior.
    """
    if frame.pixel_format is not PixelFormat.BGR24:
        raise PreprocessError(
            f"o detector consome {PixelFormat.BGR24}, e este frame é {frame.pixel_format}. "
            "Acrescente `format=bgr24` ao fim da cadeia de filtros do ffmpeg "
            "(ffmpeg/command.py) ou configure DecodeOptions.pixel_format"
        )
    return frame.as_array()


def letterbox(
    image: np.ndarray, *, size: int, spec: PreprocessSpec = YOLOX
) -> tuple[np.ndarray, Letterbox]:
    """Encaixa a imagem num quadrado `size × size` preservando a proporção.

    Devolve o tensor NCHW float32 pronto para a sessão ONNX e a `Letterbox` que desfaz
    a conta. Recusa qualquer entrada que exigiria redimensionamento — ver o docstring
    do módulo.
    """
    if image.ndim != 3 or image.shape[2] != 3:
        raise PreprocessError(f"esperava uma imagem (altura, largura, 3), recebi {image.shape}")
    if size <= 0:
        raise PreprocessError(f"size deve ser positivo, recebi {size}")

    height, width = image.shape[0], image.shape[1]
    scale = size / max(width, height)
    if scale != 1.0:
        raise PreprocessError(
            f"frame {width}x{height} não cabe num modelo de {size} sem redimensionar "
            f"(escala {scale:.4f}). O maior lado do frame tem que ser exatamente {size}: "
            f"ajuste DecodeOptions.width/height, que o ffmpeg escala de graça no filtro "
            f"que já existe (§3.1)"
        )

    pad_total_x = size - width
    pad_total_y = size - height
    pad_x = pad_total_x // 2 if spec.centered else 0
    pad_y = pad_total_y // 2 if spec.centered else 0

    # `image` é uma vista somente leitura sobre os bytes do pipe (`Frame.as_array`),
    # então o destino é sempre alocado. Preencher primeiro e copiar depois é o que
    # deixa a borda com PAD_VALUE sem uma segunda passada.
    canvas = np.full((size, size, 3), PAD_VALUE, dtype=np.uint8)
    canvas[pad_y : pad_y + height, pad_x : pad_x + width] = image

    if spec.to_rgb:
        canvas = canvas[:, :, ::-1]

    # NCHW contíguo: `transpose` só troca strides, e uma sessão ONNX alimentada com
    # array não contíguo copia por baixo dos panos a cada inferência.
    tensor = np.ascontiguousarray(canvas.transpose(2, 0, 1), dtype=np.float32)
    if spec.normalize:
        tensor /= 255.0

    return tensor[np.newaxis, ...], Letterbox(
        size=size,
        scale=scale,
        pad_x=pad_x,
        pad_y=pad_y,
        source_width=width,
        source_height=height,
    )


def undo_letterbox(boxes: np.ndarray, box: Letterbox) -> np.ndarray:
    """Leva caixas `xyxy` do espaço do modelo de volta ao do frame da câmera.

    O recorte na moldura é deliberado: o modelo detecta pessoa encostada na borda com
    a caixa transbordando, e coordenada negativa atravessaria o pipeline até o §3.4
    testar se um ponto fora do frame está dentro de um polígono.
    """
    if boxes.size == 0:
        return boxes.reshape(0, 4).astype(np.float32)
    if boxes.ndim != 2 or boxes.shape[1] != 4:
        raise PreprocessError(f"esperava caixas (N, 4) em xyxy, recebi {boxes.shape}")

    saida = boxes.astype(np.float32, copy=True)
    saida[:, [0, 2]] -= box.pad_x
    saida[:, [1, 3]] -= box.pad_y
    saida /= box.scale

    # Fatia com passo, não indexação por lista: a segunda produz cópia, e um `out=`
    # apontado para ela escreveria num array temporário — o recorte sumiria em silêncio.
    saida[:, 0::2] = np.clip(saida[:, 0::2], 0.0, box.source_width)
    saida[:, 1::2] = np.clip(saida[:, 1::2], 0.0, box.source_height)
    return saida
