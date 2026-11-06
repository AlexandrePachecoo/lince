"""Gera a fixture de referência da pose com o MMPose oficial.

Não é dependência do agente e não entra no `pyproject.toml`. Precisa de um
venv à parte com mmpose 1.3.x (e o que ele importa: torch, mmcv, mmengine).
Não baixa peso: o frame e o SimCC são sintéticos. WholeBody, Halpe e
Ultralytics não entram.

O que a fixture crava, e o round-trip `to_crop`/`undo_crop` não consegue:

- o tensor, via `GetBBoxCenterScale` + `TopdownAffine` + `PoseDataPreprocessor`
  da config RTMPose-s COCO 256×192;
- as juntas, via `SimCCLabel.decode` e a conta de
  `TopdownPoseEstimator.add_pred_to_datasample`
  (`ponto / input_size * escala + centro - 0.5 * escala`).

    cd apps/agent && /caminho/do/venv-mmpose/bin/python tools/gera_referencia_pose.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import numpy as np

SEED = 0
ALTURA = 480
LARGURA = 640
INPUT_LARGURA = 192
INPUT_ALTURA = 256
SPLIT_RATIO = 2.0
NUM_JUNTAS = 17

# Uma dentro do quadro, uma saindo por cima/esquerda, uma saindo por baixo/direita.
CAIXAS = np.array(
    [
        [80.0, 40.0, 240.0, 360.0],
        [-36.0, -24.0, 100.0, 180.0],
        [400.0, 300.0, 700.0, 500.0],
    ],
    dtype=np.float32,
)

MEAN = [123.675, 116.28, 103.53]
STD = [58.395, 57.12, 57.375]

_RAIZ = Path(__file__).resolve().parents[1]
_DESTINO = _RAIZ / "tests" / "fixtures" / "pose_rtmpose_s.npz"


def frame_sintetico() -> np.ndarray:
    """O mesmo gerador do teste. `default_rng(0).integers` bate no NumPy 1.26 e no 2.x."""
    return np.random.default_rng(SEED).integers(0, 256, size=(ALTURA, LARGURA, 3), dtype=np.uint8)


def simcc_sintetico() -> tuple[np.ndarray, np.ndarray]:
    """Picos fixos, iguais para as três caixas: o frame é que tem que mudar.

    O nariz fica negativo de propósito. O argmax estaria nos bins (10, 20);
    o `SimCCLabel` troca isso pelo sentinela -1.
    """
    eixo_x = np.zeros((1, NUM_JUNTAS, INPUT_LARGURA * 2), dtype=np.float32)
    eixo_y = np.zeros((1, NUM_JUNTAS, INPUT_ALTURA * 2), dtype=np.float32)
    eixo_x[0, 9, 100] = 0.9
    eixo_y[0, 9, 80] = 0.4
    eixo_x[0, 10, 0] = 0.2
    eixo_y[0, 10, -1] = 0.05
    eixo_x[0, 5, 50] = 1.5
    eixo_y[0, 5, 60] = 0.7
    eixo_x[0, 0, :] = -1.0
    eixo_y[0, 0, :] = -1.0
    eixo_x[0, 0, 10] = -0.3
    eixo_y[0, 0, 20] = -0.15
    return eixo_x, eixo_y


def _preprocessor():
    """`PoseDataPreprocessor` mora em `mmpose.models`, cujo `__init__` também
    importa cabeças que puxam o mmdet. Num venv só com o que o recorte usa,
    esse import quebra depois de a classe já estar carregada."""
    try:
        from mmpose.models.data_preprocessors import PoseDataPreprocessor
    except ModuleNotFoundError:
        modulo = sys.modules.get("mmpose.models.data_preprocessors.data_preprocessor")
        if modulo is None:
            raise SystemExit(
                "Este script precisa do MMPose oficial (PoseDataPreprocessor). "
                "Não é dependência do agente: instale mmpose num venv à parte."
            ) from None
        return modulo.PoseDataPreprocessor
    return PoseDataPreprocessor


class _Amostra:
    def set_metainfo(self, meta: dict) -> None:
        self.meta = meta


def _tensor(imagem_bgr: np.ndarray, preprocessor: object) -> np.ndarray:
    import torch

    chw = torch.from_numpy(np.ascontiguousarray(imagem_bgr.transpose(2, 0, 1)))
    saida = preprocessor({"inputs": [chw], "data_samples": [_Amostra()]}, training=False)
    return saida["inputs"].detach().cpu().numpy()


def _para_o_frame(
    pontos: np.ndarray, centro: np.ndarray, escala: np.ndarray, tamanho: tuple[int, int]
) -> np.ndarray:
    """`TopdownPoseEstimator.add_pred_to_datasample`, mmpose 1.3.2.

    O codec devolve o ponto no recorte. Quem leva ao frame é esta conta, não
    o `undo_crop` daqui — senão a fixture repetiria o código que ela vigia.
    """
    tamanho_arr = np.asarray(tamanho, dtype=np.float32)
    return pontos / tamanho_arr * escala + centro - 0.5 * escala


def _amostra_indices() -> tuple[np.ndarray, np.ndarray]:
    linhas = np.unique(np.concatenate([np.arange(0, INPUT_ALTURA, 16), [INPUT_ALTURA - 1]]))
    colunas = np.unique(np.concatenate([np.arange(0, INPUT_LARGURA, 16), [INPUT_LARGURA - 1]]))
    grade_y, grade_x = np.meshgrid(linhas, colunas, indexing="ij")
    return grade_y.ravel().astype(np.int32), grade_x.ravel().astype(np.int32)


def main() -> None:
    try:
        import mmpose
        from mmpose.codecs import SimCCLabel
        from mmpose.datasets.transforms.common_transforms import GetBBoxCenterScale
        from mmpose.datasets.transforms.topdown_transforms import TopdownAffine
    except ModuleNotFoundError as erro:
        raise SystemExit(
            "Este script precisa do MMPose oficial (GetBBoxCenterScale, "
            "TopdownAffine, SimCCLabel). Não é dependência do agente: "
            f"instale mmpose num venv à parte. Faltou: {erro.name}"
        ) from erro

    codec = SimCCLabel(
        input_size=(INPUT_LARGURA, INPUT_ALTURA),
        simcc_split_ratio=SPLIT_RATIO,
        normalize=False,
        use_dark=False,
    )
    preprocessor = _preprocessor()(
        mean=MEAN,
        std=STD,
        bgr_to_rgb=True,
    )
    imagem = frame_sintetico()
    eixo_x, eixo_y = simcc_sintetico()
    pontos_recorte, confianca = codec.decode(eixo_x, eixo_y)
    linhas, colunas = _amostra_indices()

    tensores = []
    juntas = []
    centros = []
    escalas = []
    for caixa in CAIXAS:
        resultado = {"img": imagem.copy(), "bbox": caixa.reshape(1, 4).astype(np.float32)}
        resultado = GetBBoxCenterScale()(resultado)
        resultado = TopdownAffine(input_size=(INPUT_LARGURA, INPUT_ALTURA))(resultado)
        tensores.append(_tensor(resultado["img"], preprocessor))
        centro = np.asarray(resultado["input_center"], dtype=np.float32)
        escala = np.asarray(resultado["input_scale"], dtype=np.float32)
        centros.append(centro)
        escalas.append(escala)
        juntas.append(_para_o_frame(pontos_recorte, centro, escala, resultado["input_size"])[0])

    bloco = np.stack(tensores)
    amostras = np.stack(
        [
            np.stack([tensor[0, :, int(y), int(x)] for y, x in zip(linhas, colunas, strict=True)])
            for tensor in bloco
        ]
    ).astype(np.float32)
    checksums = np.array(
        [
            hashlib.sha256(np.ascontiguousarray(tensor, dtype=np.float32).tobytes()).hexdigest()
            for tensor in bloco
        ]
    )
    _DESTINO.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        _DESTINO,
        mmpose=np.str_(mmpose.__version__),
        seed=np.int64(SEED),
        altura=np.int64(ALTURA),
        largura=np.int64(LARGURA),
        caixas=CAIXAS,
        simcc_x=np.repeat(eixo_x, len(CAIXAS), axis=0),
        simcc_y=np.repeat(eixo_y, len(CAIXAS), axis=0),
        juntas=np.stack(juntas).astype(np.float32),
        confiancas=np.repeat(confianca, len(CAIXAS), axis=0).astype(np.float32),
        centros=np.stack(centros),
        escalas=np.stack(escalas),
        amostra_y=linhas,
        amostra_x=colunas,
        amostras=amostras,
        checksums=checksums,
    )
    print(f"escrevi {_DESTINO} ({_DESTINO.stat().st_size} bytes, mmpose {mmpose.__version__})")


if __name__ == "__main__":
    main()
