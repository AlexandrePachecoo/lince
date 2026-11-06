"""Decode SimCC, a saída do RTMPose, de volta a juntas no frame da câmera.

O ONNX do RTMPose não devolve coordenadas. Devolve dois vetores de
classificação por junta — um em x, um em y — no espaço do recorte, com o
eixo mais longo que a imagem pelo `simcc_split_ratio`. A config oficial do
RTMPose-s COCO-256×192 (MMPose, Apache-2.0,
`configs/body_2d_keypoint/rtmpose/coco/rtmpose-s_8xb256-420e_coco-256x192.py`)
fixa isso:

    codec = dict(type='SimCCLabel', input_size=(192, 256),
                 simcc_split_ratio=2.0, normalize=False, use_dark=False)
    head = dict(type='RTMCCHead', out_channels=17, ...)

São 17 juntas, a ordem COCO (nariz … tornozelos), não as 133 do WholeBody nem
as 136 do Halpe. `use_dark=False`: não há refinamento sub-bin. `normalize=False`
no codec e `apply_softmax` default False em `get_simcc_maximum`
(`mmpose/codecs/utils/post_processing.py`): a confiança é o menor pico cru dos
dois eixos, e um pico de 0.04 continua 0.04. Softmax transformaria junta fraca
em junta certa.

O pico dividido pelo split ratio está no recorte 192×256. Quem devolve a junta
ao frame é a inversa do `Crop` (`undo_crop`). Sem ela, o pulso fica na
coordenada do recorte — dezenas de pixels fora da pessoa — e o gesto de
ocultação lê o lugar errado sem erro nenhum.

**A sessão ONNX não abre aqui.** Não há arquivo de modelo nesta fase, e a
suíte não pode exigir um `.onnx`. Quando o RTMPose-s exportado chegar, o
carregamento segue `detect/onnx.py`: `model_version` (nome e o começo do
sha256, R-7), a mesma lista de providers (CUDA e, se a placa cair, CPU;
§3.2, §10.10) e o import adiado do onnxruntime. `recusa_modelo_fora_do_piloto`
é a trava que essa sessão vai chamar antes de abrir o arquivo — o par, para a
pose, de `recusa_pesos_agpl`.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from lince_agent.detect.onnx import ModelError as DetectorModelError
from lince_agent.detect.onnx import model_version as _model_version_detector
from lince_agent.detect.onnx import recusa_pesos_agpl
from lince_agent.pose.preprocess import Crop, undo_crop

# A mesma ordem de `DetectionOptions.providers`. CUDA primeiro; CPU se a placa
# cair (§10.10). Nada aqui abre sessão — a constante existe para a lista não
# nascer diferente da do detector no dia em que o arquivo chegar.
PROVIDERS: tuple[str, ...] = ("CUDAExecutionProvider", "CPUExecutionProvider")

NUM_JUNTAS = 17
"""`out_channels` da RTMCCHead na config do RTMPose-s COCO. Não é WholeBody."""

SPLIT_RATIO = 2.0
"""`simcc_split_ratio` da mesma config. O vetor x tem 384 bins; o y, 512."""

# Ordem COCO, que é a ordem dos canais do RTMPose-s. "Esquerdo" é o esquerdo
# da pessoa, não o da imagem. Trocar os dois coloca o gesto no pulso errado.
JUNTAS_COCO17: tuple[str, ...] = (
    "nariz",
    "olho_esquerdo",
    "olho_direito",
    "orelha_esquerda",
    "orelha_direita",
    "ombro_esquerdo",
    "ombro_direito",
    "cotovelo_esquerdo",
    "cotovelo_direito",
    "pulso_esquerdo",
    "pulso_direito",
    "quadril_esquerdo",
    "quadril_direito",
    "joelho_esquerdo",
    "joelho_direito",
    "tornozelo_esquerdo",
    "tornozelo_direito",
)

_FORA_DO_PILOTO = re.compile(r"whole[-_]?body|halpe", re.IGNORECASE)
"""WholeBody e Halpe no nome do arquivo. Olha só o nome, como
`recusa_pesos_agpl`: um diretório chamado `halpe-comparativo` não pode
impedir o box de subir."""


class ModelError(RuntimeError):
    """O arquivo não é o RTMPose-s COCO-17 do piloto."""


class DecodeError(ValueError):
    """SimCC que não dá para ler como 17 juntas. Sempre diz o que divergiu."""


@dataclass(frozen=True, slots=True)
class Junta:
    """Uma junta no frame da câmera, já com o recorte desfeito.

    Confiança ≤ 0 segue o `get_simcc_maximum`: os dois eixos viram o sentinela
    -1, dividido pelo split ratio e projetado. Não é o argmax desse vetor — o
    pico menos ruim de um mapa não positivo cairia noutro lugar, com cara de
    pulso. Confiança não finita fica sem coordenada: `nan <= limiar` é falso,
    e uma junta com x/y preenchido passaria no gesto.
    """

    indice: int
    x: float
    y: float
    confianca: float

    @property
    def nome(self) -> str:
        return JUNTAS_COCO17[self.indice]


def model_version(model_path: Path) -> str:
    """Identidade do arquivo: nome mais o começo do sha256.

    É `detect.onnx.model_version`, não uma segunda receita. Dois boxes com
    `rtmpose-s.onnx` podem ter arquivos diferentes depois de um rollout
    parcial (R-7); hashes diferentes no heartbeat esconderiam exatamente isso.
    A sessão que vai gravar este campo ainda não abre.
    """
    return _model_version_detector(model_path)


def recusa_modelo_fora_do_piloto(model_path: Path) -> None:
    """Recusa Ultralytics, COCO-WholeBody e Halpe pelo nome do arquivo.

    A sessão ainda não abre, e esta trava não exige que o arquivo exista — a
    licença e a contagem de juntas se leem do nome, como no detector. Um ONNX
    de 133 juntas decodificado como 17 não estoura: o pulso sai no índice
    errado e o gesto de ocultação olha para outra parte do corpo.
    """
    try:
        recusa_pesos_agpl(model_path)
    except DetectorModelError as erro:
        raise ModelError(str(erro)) from erro
    marca = _FORA_DO_PILOTO.search(model_path.name)
    if marca is not None:
        raise ModelError(
            f"{model_path.name} não é o RTMPose-s COCO-17 do piloto "
            f"('{marca.group(0)}' no nome). WholeBody (133 juntas) e Halpe (136) "
            "têm outra contagem; decodificar isso como 17 canais coloca o pulso "
            "no índice errado, sem erro. O piloto usa o RTMPose-s COCO-17 "
            "exportado do MMPose (Apache-2.0)"
        )


def decode_simcc(
    simcc_x: np.ndarray,
    simcc_y: np.ndarray,
    crop: Crop,
    *,
    split_ratio: float = SPLIT_RATIO,
) -> tuple[Junta, ...]:
    """De `simcc_x` e `simcc_y` para 17 juntas no frame da câmera.

    Aceita o lote de uma pessoa `(1, 17, bins)` que o ONNX devolve, ou o
    `(17, bins)` sem o lote. `bins` tem que ser `lado_do_recorte * split_ratio`
    (384 e 512 com o default). Esquecer o 2.0 coloca a junta na metade do
    caminho entre o canto do recorte e o corpo, e o número continua plausível.
    """
    if not math.isfinite(split_ratio) or split_ratio <= 0:
        raise DecodeError(
            f"simcc_split_ratio deve ser positivo (a config do RTMPose-s usa "
            f"{SPLIT_RATIO}), recebi {split_ratio}"
        )
    eixo_x = _sem_lote(np.asarray(simcc_x), "simcc_x")
    eixo_y = _sem_lote(np.asarray(simcc_y), "simcc_y")
    if eixo_x.shape[0] != NUM_JUNTAS or eixo_y.shape[0] != NUM_JUNTAS:
        raise DecodeError(
            f"RTMPose-s COCO-17 tem {NUM_JUNTAS} juntas; recebi simcc_x {eixo_x.shape} "
            f"e simcc_y {eixo_y.shape}. WholeBody (133) e Halpe (136) não são este modelo"
        )
    bins_x = int(round(crop.input_width * split_ratio))
    bins_y = int(round(crop.input_height * split_ratio))
    if eixo_x.shape[1] != bins_x or eixo_y.shape[1] != bins_y:
        raise DecodeError(
            f"SimCC com split ratio {split_ratio} sobre recorte "
            f"{crop.input_width}×{crop.input_height} (largura×altura) tem vetores "
            f"({bins_x}, {bins_y}); recebi {eixo_x.shape[1]} e {eixo_y.shape[1]}. "
            f"Esquecer o split ratio de {SPLIT_RATIO} coloca a junta na metade do "
            "caminho, sem erro nenhum"
        )

    # Pico cru, como `get_simcc_maximum` com apply_softmax=False. A cópia é o
    # que impede esta leitura de escrever no buffer de quem chamou: um softmax
    # in-place mudaria a confiança e o tensor ainda pareceria o mesmo.
    eixo_x = np.array(eixo_x, dtype=np.float64, copy=True)
    eixo_y = np.array(eixo_y, dtype=np.float64, copy=True)
    bin_x = np.argmax(eixo_x, axis=1).astype(np.float64)
    bin_y = np.argmax(eixo_y, axis=1).astype(np.float64)
    confianca = np.minimum(np.max(eixo_x, axis=1), np.max(eixo_y, axis=1))
    # `locs[vals <= 0] = -1` no get_simcc_maximum, e o decode divide isso pelo
    # split ratio. O argmax de um vetor não positivo não entra.
    sem_pico = confianca <= 0
    bin_x[sem_pico] = -1.0
    bin_y[sem_pico] = -1.0
    no_recorte = np.stack((bin_x / split_ratio, bin_y / split_ratio), axis=1).astype(np.float32)
    no_frame = undo_crop(no_recorte, crop)

    juntas: list[Junta] = []
    for indice in range(NUM_JUNTAS):
        valor = float(confianca[indice])
        if not math.isfinite(valor):
            x = y = math.nan
        else:
            x = float(no_frame[indice, 0])
            y = float(no_frame[indice, 1])
        juntas.append(Junta(indice=indice, x=x, y=y, confianca=valor))
    return tuple(juntas)


def _sem_lote(tensor: np.ndarray, nome: str) -> np.ndarray:
    if tensor.ndim == 3:
        if tensor.shape[0] != 1:
            raise DecodeError(
                f"{nome} tem lote {tensor.shape[0]}; o decode é de uma pessoa por recorte, "
                f"recebi {tensor.shape}"
            )
        tensor = tensor[0]
    if tensor.ndim != 2:
        raise DecodeError(
            f"{nome} deve ter forma (juntas, bins) ou (1, juntas, bins), recebi {tensor.shape}"
        )
    return tensor
