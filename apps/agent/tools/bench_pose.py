"""Mede o recorte, a inferência e o decode da pose, fora da suíte.

Roda o YOLOX que o agente já usa e o RTMPose-s carregado por `OnnxPose`.
O tempo sai em ms, p50 e p95, por pessoa e por frame, com 1, 3 e 6 pessoas.
O recorte NumPy é o de `crop_bbox`. O `cv2.warpAffine` só entra se o opencv
já estiver instalado neste ambiente — não é dependência do agente. Sem ele,
o estágio é pulado com um aviso e o resto da tabela segue.

Funciona com CUDAExecutionProvider e com CPU. `--provider cuda` pede a placa
e cai para CPU se ela não subir, que é o mesmo fallback do detector (§10.10).
O JSON grava o provider que de fato rodou: um número de CPU com rótulo de
GPU não serve para decidir o box.

Um frame com menos gente do que N repete as caixas até N. O que se mede é o
lote, não quantas pessoas distintas o vídeo tinha. Sem repetir, "6 pessoas"
ficaria vazio num corredor com duas.

    uv run python tools/bench_pose.py video.mp4 \\
        --detector models/yolox_s.onnx \\
        --pose ~/lince-modelos/rtmpose-s.onnx \\
        --provider cuda \\
        --saida ~/lince-modelos/bench_pose.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from lince_agent.config import DetectionOptions, PixelFormat
from lince_agent.detect.onnx import ModelError as DetectorModelError
from lince_agent.detect.onnx import OnnxDetector
from lince_agent.detect.state import Detection
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.onnx import ModelError as PoseModelError
from lince_agent.pose.onnx import OnnxPose, decode_simcc
from lince_agent.pose.preprocess import RTMPOSE, crop_bbox, plano_crop

PESSOAS_PADRAO = (1, 3, 6)
EXTENSOES = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".ppm"}


class BenchError(Exception):
    """O benchmark não tem o que medir. A mensagem diz o que falta."""


def providers_de(nome: str) -> tuple[str, ...]:
    if nome == "cpu":
        return ("CPUExecutionProvider",)
    if nome == "cuda":
        # CUDA primeiro, CPU se a placa cair. A mesma ordem do detector.
        return ("CUDAExecutionProvider", "CPUExecutionProvider")
    raise BenchError(
        f"provider {nome!r} não é cuda nem cpu. "
        "cuda pede CUDAExecutionProvider e cai para CPU se a placa falhar; "
        "cpu força CPUExecutionProvider"
    )


def interpreta_pessoas(texto: str) -> tuple[int, ...]:
    try:
        pessoas = tuple(int(parte) for parte in texto.split(",") if parte.strip())
    except ValueError as erro:
        raise BenchError(
            f"pessoas deve ser inteiros separados por vírgula, recebi {texto!r}"
        ) from erro
    if not pessoas or any(n < 1 for n in pessoas):
        raise BenchError(f"pessoas tem que ser ao menos 1, recebi {texto!r}")
    return pessoas


def escolhe_caixas(
    caixas: list[tuple[float, float, float, float]], n: int
) -> list[tuple[float, float, float, float]]:
    """Repete as detecções até N. Lista vazia continua vazia: não há pessoa para clonar."""
    if not caixas:
        return []
    return [caixas[i % len(caixas)] for i in range(n)]


def percentis(amostras: list[float]) -> dict[str, float | int]:
    if not amostras:
        raise BenchError("percentil sem amostras")
    arr = np.asarray(amostras, dtype=np.float64)
    return {
        "p50": float(np.percentile(arr, 50)),
        "p95": float(np.percentile(arr, 95)),
        "n": int(arr.size),
    }


def opencv_disponivel() -> bool:
    try:
        import cv2
    except ImportError:
        return False
    return hasattr(cv2, "warpAffine")


def recorte_warp_affine(imagem: np.ndarray, caixa: tuple[float, float, float, float]) -> np.ndarray:
    """O mesmo plano do recorte NumPy, amostrado pelo `cv2.warpAffine`.

    Fica neste script de propósito. Puxar opencv para `pose/preprocess.py`
    faria o agente da loja depender de uma biblioteca que o ffmpeg já substitui
    na leitura, só para comparar um warp.
    """
    import cv2

    crop = plano_crop(imagem, caixa, spec=RTMPOSE)
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
    # A matriz leva o pixel do recorte ao pixel do frame — o mesmo mapa do
    # `_warp_bilinear`. Sem WARP_INVERSE_MAP o OpenCV inverte de novo e amostra
    # outro retângulo; o tensor normalizado diverge em unidades e o benchmark
    # cronometra um recorte que não é o da pose.
    recortado = cv2.warpAffine(
        imagem,
        matriz,
        (largura, altura),
        flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=(0, 0, 0),
    )
    rgb = recortado[:, :, ::-1]
    media = np.asarray(RTMPOSE.mean_rgb, dtype=np.float32).reshape(1, 1, 3)
    desvio = np.asarray(RTMPOSE.std_rgb, dtype=np.float32).reshape(1, 1, 3)
    normalizado = (rgb.astype(np.float32) - media) / desvio
    return np.ascontiguousarray(normalizado.transpose(2, 0, 1)[np.newaxis, ...])


def mede_caixas(
    imagem: np.ndarray,
    caixas: list[tuple[float, float, float, float]],
    pose: Any,
    *,
    usar_opencv: bool,
) -> dict[str, Any]:
    """Tempos em ms. Pessoa é cada caixa; frame é o wall clock das caixas juntas.

    A inferência em lote não separa uma pessoa da outra: o tempo por pessoa é
    o do frame dividido por N. No lote fixo, cada chamada é uma pessoa e o
    frame é a soma. Os dois números estão no JSON para não misturar.
    """
    pessoa_numpy: list[float] = []
    tensores = []
    crops = []
    inicio = time.perf_counter()
    for caixa in caixas:
        t0 = time.perf_counter()
        tensor, crop = crop_bbox(imagem, caixa)
        pessoa_numpy.append((time.perf_counter() - t0) * 1000)
        tensores.append(tensor)
        crops.append(crop)
    frame_numpy = (time.perf_counter() - inicio) * 1000

    pessoa_warp: list[float] | None = None
    frame_warp: float | None = None
    if usar_opencv:
        pessoa_warp = []
        inicio = time.perf_counter()
        for caixa in caixas:
            t0 = time.perf_counter()
            recorte_warp_affine(imagem, caixa)
            pessoa_warp.append((time.perf_counter() - t0) * 1000)
        frame_warp = (time.perf_counter() - inicio) * 1000

    pessoa_inf: list[float] = []
    inicio = time.perf_counter()
    if pose.lote_dinamico and len(tensores) > 1:
        t0 = time.perf_counter()
        simcc_x, simcc_y = pose.inferir(np.concatenate(tensores, axis=0))
        dt = (time.perf_counter() - t0) * 1000
        pessoa_inf = [dt / len(tensores)] * len(tensores)
    else:
        eixos_x = []
        eixos_y = []
        for tensor in tensores:
            t0 = time.perf_counter()
            simcc_x_i, simcc_y_i = pose.inferir(tensor)
            pessoa_inf.append((time.perf_counter() - t0) * 1000)
            eixos_x.append(simcc_x_i)
            eixos_y.append(simcc_y_i)
        simcc_x = np.concatenate(eixos_x, axis=0)
        simcc_y = np.concatenate(eixos_y, axis=0)
    frame_inf = (time.perf_counter() - inicio) * 1000

    pessoa_dec: list[float] = []
    inicio = time.perf_counter()
    for i, crop in enumerate(crops):
        t0 = time.perf_counter()
        decode_simcc(simcc_x[i : i + 1], simcc_y[i : i + 1], crop)
        pessoa_dec.append((time.perf_counter() - t0) * 1000)
    frame_dec = (time.perf_counter() - inicio) * 1000

    return {
        "recorte_numpy_pessoa": pessoa_numpy,
        "recorte_numpy_frame": frame_numpy,
        "recorte_warp_pessoa": pessoa_warp,
        "recorte_warp_frame": frame_warp,
        "inferencia_pessoa": pessoa_inf,
        "inferencia_frame": frame_inf,
        "decode_pessoa": pessoa_dec,
        "decode_frame": frame_dec,
    }


def _bloco(pessoa: list[float], frame: list[float]) -> dict[str, Any]:
    return {"por_pessoa": percentis(pessoa), "por_frame": percentis(frame)}


def monta_relatorio(
    *,
    fonte: Path,
    frames_lidos: int,
    frames_com_pessoa: int,
    limiar: float,
    aquecimento: int,
    pessoas: tuple[int, ...],
    detector_versao: str,
    detector_provider: str,
    pose_versao: str,
    pose_provider: str,
    lote_dinamico: bool,
    opencv: bool,
    nota_opencv: str | None,
    estagios: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    return {
        "unidade": "ms",
        "fonte": str(fonte),
        "frames_lidos": frames_lidos,
        "frames_com_pessoa": frames_com_pessoa,
        "limiar": limiar,
        "aquecimento": aquecimento,
        "pessoas_pedidas": list(pessoas),
        "caixas": "repete as detecções do frame até N, para medir o lote e não o vídeo",
        "detector": {"model_version": detector_versao, "provider": detector_provider},
        "pose": {
            "model_version": pose_versao,
            "provider": pose_provider,
            "lote_dinamico": lote_dinamico,
        },
        "opencv": opencv,
        "nota_opencv": nota_opencv,
        "estagios_ms": {str(n): bloco for n, bloco in estagios.items()},
    }


def formata_tabela(relatorio: dict[str, Any]) -> str:
    linhas = [
        f"{'pessoas':>7}  {'estágio':<18}  {'p50/pessoa':>10}  {'p95/pessoa':>10}  "
        f"{'p50/frame':>10}  {'p95/frame':>10}",
    ]
    if not relatorio["opencv"]:
        linhas.append(relatorio["nota_opencv"] or "cv2.warpAffine pulado")
    for n, bloco in relatorio["estagios_ms"].items():
        for rotulo, chave in (
            ("recorte NumPy", "recorte_numpy_ms"),
            ("warpAffine", "recorte_warp_affine_ms"),
            ("inferência", "inferencia_ms"),
            ("decode", "decode_ms"),
        ):
            estagio = bloco[chave]
            if estagio is None:
                celulas = ("pulado",) * 4
            else:
                celulas = (
                    f"{estagio['por_pessoa']['p50']:10.3f}",
                    f"{estagio['por_pessoa']['p95']:10.3f}",
                    f"{estagio['por_frame']['p50']:10.3f}",
                    f"{estagio['por_frame']['p95']:10.3f}",
                )
            linhas.append(
                f"{n:>7}  {rotulo:<18}  {celulas[0]:>10}  {celulas[1]:>10}  "
                f"{celulas[2]:>10}  {celulas[3]:>10}"
            )
    return "\n".join(linhas)


def _binario(nome: str) -> str:
    caminho = shutil.which(nome)
    if caminho is None:
        raise BenchError(
            f"{nome} não está no PATH. O benchmark lê o vídeo por ele, "
            "sem acrescentar opencv ao agente"
        )
    return caminho


def _dimensoes(caminho: Path) -> tuple[int, int]:
    ffprobe = _binario("ffprobe")
    resultado = subprocess.run(  # noqa: S603 - argv montado aqui, arquivo local
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(caminho),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if resultado.returncode != 0 or not resultado.stdout.strip():
        detalhe = resultado.stderr.strip() or "sem stream de vídeo"
        raise BenchError(f"ffprobe falhou em {caminho.name}: {detalhe}")
    texto = resultado.stdout.strip().splitlines()[0]
    largura_s, altura_s = texto.split("x", 1)
    return int(largura_s), int(altura_s)


def dimensoes_para(largura: int, altura: int, lado_maior: int) -> tuple[int, int]:
    """O tamanho que o agente pediria ao ffmpeg: lado maior igual ao do
    detector, proporção mantida, lados pares.

    O letterbox do YOLOX recusa frame cujo lado maior não é o do modelo (§3.1):
    no agente quem ajusta é o filtro `scale` que já existe na ingestão, e o
    benchmark tem que medir sobre o mesmo frame que a loja vai ver.
    """

    def par(valor: float) -> int:
        return max(2, int(round(valor / 2)) * 2)

    if largura >= altura:
        return lado_maior, par(altura * lado_maior / largura)
    return par(largura * lado_maior / altura), lado_maior


def _decodifica(caminho: Path, largura: int, altura: int, max_frames: int) -> list[np.ndarray]:
    ffmpeg = _binario("ffmpeg")
    resultado = subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-v",
            "error",
            "-i",
            str(caminho),
            "-vf",
            f"scale={largura}:{altura}",
            "-frames:v",
            str(max_frames),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    if resultado.returncode != 0:
        detalhe = resultado.stderr.decode("utf-8", "replace").strip()
        raise BenchError(f"ffmpeg falhou em {caminho.name}: {detalhe}")
    quadro = largura * altura * 3
    if quadro == 0 or len(resultado.stdout) < quadro:
        raise BenchError(f"{caminho.name} não produziu um frame BGR {largura}×{altura}")
    n = len(resultado.stdout) // quadro
    return [
        np.frombuffer(resultado.stdout[i * quadro : (i + 1) * quadro], dtype=np.uint8)
        .reshape(altura, largura, 3)
        .copy()
        for i in range(n)
    ]


def le_frames(fonte: Path, max_frames: int, *, lado_maior: int) -> list[np.ndarray]:
    if fonte.is_dir():
        arquivos = sorted(p for p in fonte.iterdir() if p.suffix.lower() in EXTENSOES)
        if not arquivos:
            raise BenchError(f"nenhuma imagem em {fonte}")
        frames = []
        for arquivo in arquivos[:max_frames]:
            largura, altura = dimensoes_para(*_dimensoes(arquivo), lado_maior)
            frames.extend(_decodifica(arquivo, largura, altura, 1))
        return frames
    if not fonte.is_file():
        raise BenchError(f"não encontrei vídeo nem pasta em {fonte}")
    largura, altura = dimensoes_para(*_dimensoes(fonte), lado_maior)
    return _decodifica(fonte, largura, altura, max_frames)


def _como_frame(imagem: np.ndarray, sequence: int) -> Frame:
    return Frame(
        data=np.ascontiguousarray(imagem).tobytes(),
        width=int(imagem.shape[1]),
        height=int(imagem.shape[0]),
        pixel_format=PixelFormat.BGR24,
        sequence=sequence,
        received_at=0.0,
    )


def _xyxy(deteccao: Detection) -> tuple[float, float, float, float]:
    return (deteccao.x1, deteccao.y1, deteccao.x2, deteccao.y2)


def _acumula(
    destino: dict[str, list[float]],
    chave_pessoa: str,
    chave_frame: str,
    medida: dict[str, Any],
    origem_pessoa: str,
    origem_frame: str,
) -> None:
    destino[chave_pessoa].extend(medida[origem_pessoa])
    destino[chave_frame].append(medida[origem_frame])


def roda(
    *,
    fonte: Path,
    detector: Path,
    pose_caminho: Path,
    saida: Path,
    provider: str,
    pessoas: tuple[int, ...],
    max_frames: int,
    limiar: float,
    aquecimento: int,
) -> dict[str, Any]:
    providers = providers_de(provider)
    opcoes = DetectionOptions(
        enabled=True,
        model_path=detector,
        providers=providers,
        score_threshold=limiar,
    )
    yolox = OnnxDetector(detector, opcoes)
    pose: OnnxPose | None = None
    try:
        info = yolox.info()
        assert info is not None
        frames = le_frames(fonte, max_frames, lado_maior=info.input_size)
        pose = OnnxPose(pose_caminho, providers=providers)
        com_pessoa: list[tuple[np.ndarray, list[tuple[float, float, float, float]]]] = []
        for indice, imagem in enumerate(frames):
            deteccoes = yolox.detect(_como_frame(imagem, indice))
            if deteccoes:
                com_pessoa.append((imagem, [_xyxy(deteccao) for deteccao in deteccoes]))
        if not com_pessoa:
            raise BenchError(
                "nenhuma pessoa no recorte lido. Baixe o vídeo de "
                "`bash scripts/modelo.sh` ou desça --limiar (o piso do detector é 0,10)"
            )

        usar_opencv = opencv_disponivel()
        nota = None
        if not usar_opencv:
            nota = (
                "opencv não está neste ambiente; cv2.warpAffine foi pulado. "
                "O recorte NumPy segue na tabela"
            )
            print(f"aviso: {nota}", file=sys.stderr)

        info_pose = pose.info()
        info_det = yolox.info()
        if info_pose is None or info_det is None:
            raise BenchError("detector ou pose subiu sem identidade de modelo")

        for _ in range(aquecimento):
            imagem, caixas = com_pessoa[0]
            mede_caixas(imagem, escolhe_caixas(caixas, pessoas[0]), pose, usar_opencv=usar_opencv)

        estagios: dict[int, dict[str, Any]] = {}
        for n in pessoas:
            baldes: dict[str, list[float]] = {
                "numpy_pessoa": [],
                "numpy_frame": [],
                "warp_pessoa": [],
                "warp_frame": [],
                "inf_pessoa": [],
                "inf_frame": [],
                "dec_pessoa": [],
                "dec_frame": [],
            }
            for imagem, caixas in com_pessoa:
                medida = mede_caixas(
                    imagem, escolhe_caixas(caixas, n), pose, usar_opencv=usar_opencv
                )
                _acumula(
                    baldes,
                    "numpy_pessoa",
                    "numpy_frame",
                    medida,
                    "recorte_numpy_pessoa",
                    "recorte_numpy_frame",
                )
                _acumula(
                    baldes,
                    "inf_pessoa",
                    "inf_frame",
                    medida,
                    "inferencia_pessoa",
                    "inferencia_frame",
                )
                _acumula(
                    baldes,
                    "dec_pessoa",
                    "dec_frame",
                    medida,
                    "decode_pessoa",
                    "decode_frame",
                )
                if medida["recorte_warp_pessoa"] is not None:
                    _acumula(
                        baldes,
                        "warp_pessoa",
                        "warp_frame",
                        medida,
                        "recorte_warp_pessoa",
                        "recorte_warp_frame",
                    )
            estagios[n] = {
                "amostras_frame": len(com_pessoa),
                "recorte_numpy_ms": _bloco(baldes["numpy_pessoa"], baldes["numpy_frame"]),
                "recorte_warp_affine_ms": (
                    None if not usar_opencv else _bloco(baldes["warp_pessoa"], baldes["warp_frame"])
                ),
                "inferencia_ms": _bloco(baldes["inf_pessoa"], baldes["inf_frame"]),
                "decode_ms": _bloco(baldes["dec_pessoa"], baldes["dec_frame"]),
            }
        relatorio = monta_relatorio(
            fonte=fonte,
            frames_lidos=len(frames),
            frames_com_pessoa=len(com_pessoa),
            limiar=limiar,
            aquecimento=aquecimento,
            pessoas=pessoas,
            detector_versao=info_det.model_version,
            detector_provider=info_det.provider,
            pose_versao=info_pose.model_version,
            pose_provider=info_pose.provider,
            lote_dinamico=pose.lote_dinamico,
            opencv=usar_opencv,
            nota_opencv=nota,
            estagios=estagios,
        )
    finally:
        if pose is not None:
            pose.close()
        yolox.close()

    print(formata_tabela(relatorio))
    saida.parent.mkdir(parents=True, exist_ok=True)
    saida.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"json em {saida}")
    return relatorio


def _analisa(argv: list[str]) -> argparse.Namespace:
    analise = argparse.ArgumentParser(description=__doc__)
    analise.add_argument("fonte", type=Path, help="vídeo ou pasta de frames")
    analise.add_argument("--detector", type=Path, required=True, help="YOLOX .onnx, fora do git")
    analise.add_argument("--pose", type=Path, required=True, help="RTMPose-s COCO-17 .onnx")
    analise.add_argument(
        "--saida", type=Path, required=True, help="JSON dos percentis, de preferência fora do git"
    )
    analise.add_argument("--provider", choices=("cuda", "cpu"), default="cuda")
    analise.add_argument("--pessoas", default="1,3,6")
    analise.add_argument("--max-frames", type=int, default=60)
    analise.add_argument("--limiar", type=float, default=0.10)
    analise.add_argument("--aquecimento", type=int, default=3)
    return analise.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _analisa(sys.argv[1:] if argv is None else argv)
    try:
        if args.max_frames < 1:
            raise BenchError(f"--max-frames tem que ser ao menos 1, recebi {args.max_frames}")
        if args.aquecimento < 0:
            raise BenchError(f"--aquecimento não pode ser negativo, recebi {args.aquecimento}")
        if not 0.0 <= args.limiar <= 1.0:
            raise BenchError(f"--limiar tem que estar entre 0 e 1, recebi {args.limiar}")
        roda(
            fonte=args.fonte,
            detector=args.detector,
            pose_caminho=args.pose,
            saida=args.saida,
            provider=args.provider,
            pessoas=interpreta_pessoas(args.pessoas),
            max_frames=args.max_frames,
            limiar=args.limiar,
            aquecimento=args.aquecimento,
        )
    except (BenchError, DetectorModelError, PoseModelError) as erro:
        print(f"bench_pose: {erro}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
