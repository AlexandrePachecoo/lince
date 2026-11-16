"""Mede o detector no box sem placa: tempo por frame e quanto cada modelo enxerga.

É a medição que decide o critério de saída da fase 1 (§11 da arquitetura): **três
câmeras no mini PC sem descarte sustentado, com recall medido em vídeo real**. Os dois
números saem juntos de propósito. Um modelo mais rápido que não vê as pessoas não
resolve nada — só troca saturação de CPU por loja cega, e loja cega não dá erro.

O recall aqui é **relativo**: a fração das pessoas que o modelo de referência (o
primeiro `--modelo`, normalmente o YOLOX-s) acha e que o modelo medido também acha.
Não há anotação humana, então o número não diz quantas pessoas existiam no vídeo; diz
quanto se perde ao trocar o modelo grande pelo pequeno. Para isso basta.

O pipeline medido é o do agente inteiro, não só a sessão do ONNX Runtime: letterbox,
inferência, decodificação, NMS — o `OnnxDetector.detect` de produção. É ele que roda na
loja, e a diferença entre os dois é de milissegundos que importam num mini PC.

    uv run python tools/bench_deteccao.py ../../infra/videos/pessoas.mp4 \\
        --modelo models/yolox_s.onnx:640 \\
        --modelo models/yolox_tiny.onnx:416 \\
        --modelo models/yolox_nano.onnx:416 \\
        --provider openvino

O `--provider openvino` só existe com o grupo de mesmo nome instalado
(`uv sync --no-group cpu --group openvino`). O relatório grava o provider que de fato
rodou: um número de CPU com o rótulo de OpenVINO não serve para escolher o box.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from lince_agent.config import DetectionOptions, PixelFormat
from lince_agent.detect.onnx import OnnxDetector
from lince_agent.ffmpeg.rawframe import Frame

Caixa = tuple[float, float, float, float]

AQUECIMENTO = 3
"""Frames rodados antes de cronometrar. A primeira inferência aloca arenas e, no
OpenVINO, compila o grafo: contá-la inflaria a média de um vídeo curto."""

SCORE_PESSOA = 0.5
"""O corte que conta uma caixa como pessoa no recall. É o `high_threshold` padrão do
tracking: abaixo dele a caixa não cria track, então não é uma pessoa que o sistema vê."""

IOU_MESMA_PESSOA = 0.5
"""Duas caixas de modelos diferentes são a mesma pessoa a partir desta sobreposição."""


class BenchError(Exception):
    """O benchmark não tem o que medir. A mensagem diz o que falta."""


@dataclass(frozen=True, slots=True)
class Modelo:
    caminho: Path
    entrada: int

    @property
    def nome(self) -> str:
        return self.caminho.stem


@dataclass(frozen=True, slots=True)
class Medida:
    provider: str
    ms: list[float]
    caixas: list[list[Caixa]]
    """Por frame, as pessoas acima de `SCORE_PESSOA`."""


def interpreta_modelo(texto: str) -> Modelo:
    """`caminho.onnx:416`. A entrada é obrigatória: o tiny e o nano são de 416 e o s é
    de 640, e errar não é inofensivo — o detector recusa na subida, ou, num `.onnx` de
    entrada dinâmica, decodifica caixas plausíveis e erradas."""
    caminho, separador, entrada = texto.rpartition(":")
    if not separador or not caminho:
        raise BenchError(
            f"--modelo é CAMINHO:ENTRADA, como models/yolox_tiny.onnx:416; recebi {texto!r}"
        )
    try:
        lado = int(entrada)
    except ValueError as erro:
        raise BenchError(
            f"a entrada de {caminho} deve ser um inteiro, recebi {entrada!r}"
        ) from erro
    return Modelo(Path(caminho), lado)


def providers_de(nome: str) -> tuple[str, ...]:
    """O provider pedido e a CPU depois dele, como o agente faz (§10.10)."""
    pedidos = {
        "cpu": ("CPUExecutionProvider",),
        "openvino": ("OpenVINOExecutionProvider", "CPUExecutionProvider"),
        "cuda": ("CUDAExecutionProvider", "CPUExecutionProvider"),
    }
    if nome not in pedidos:
        raise BenchError(f"provider {nome!r} não é um de {sorted(pedidos)}")
    return pedidos[nome]


def decodifica(
    video: Path, *, largura: int, altura: int, fps: float, max_frames: int
) -> list[Frame]:
    """Os frames que o estágio 2 receberia: amostrados a `fps`, na resolução do
    substream, em BGR24 — a mesma cadeia `fps,scale` do `ffmpeg/command.py`."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise BenchError("ffmpeg não está no PATH")
    if not video.is_file():
        raise BenchError(f"vídeo não encontrado: {video}")
    processo = subprocess.run(
        [
            ffmpeg,
            *("-v", "error", "-i", str(video)),
            *("-vf", f"fps={fps},scale={largura}:{altura}"),
            *("-frames:v", str(max_frames), "-pix_fmt", "bgr24", "-f", "rawvideo", "-"),
        ],
        capture_output=True,
        check=False,
    )
    if processo.returncode != 0:
        raise BenchError(f"ffmpeg falhou: {processo.stderr.decode(errors='replace').strip()}")
    tamanho = PixelFormat.BGR24.frame_bytes(largura, altura)
    quantos = len(processo.stdout) // tamanho
    if quantos == 0:
        raise BenchError(f"{video} não rendeu nenhum frame")
    return [
        Frame(
            data=processo.stdout[i * tamanho : (i + 1) * tamanho],
            width=largura,
            height=altura,
            pixel_format=PixelFormat.BGR24,
            sequence=i,
            received_at=i / fps,
        )
        for i in range(quantos)
    ]


def mede(detector: Any, frames: list[Frame]) -> Medida:
    """Roda o detector em todos os frames. O aquecimento não entra no tempo, mas entra
    nas caixas: o recall compara frame a frame, e pular frames desalinharia a lista."""
    ms: list[float] = []
    caixas: list[list[Caixa]] = []
    for indice, frame in enumerate(frames):
        inicio = time.perf_counter()
        deteccoes = detector.detect(frame)
        decorrido = (time.perf_counter() - inicio) * 1000
        if indice >= AQUECIMENTO:
            ms.append(decorrido)
        caixas.append([(d.x1, d.y1, d.x2, d.y2) for d in deteccoes if d.score >= SCORE_PESSOA])
    if not ms:
        # Vídeo curto: só aquecimento. Melhor um tempo inflado que nenhum.
        raise BenchError(f"são necessários mais de {AQUECIMENTO} frames para medir tempo")
    info = detector.info()
    return Medida(provider=info.provider if info else "?", ms=ms, caixas=caixas)


def iou(a: Caixa, b: Caixa) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    intersecao = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    uniao = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - intersecao
    # União zero vira `nan` e `nan >= limiar` é falso em silêncio (armadilha já paga).
    return intersecao / uniao if uniao > 0 else 0.0


def recall_relativo(
    referencia: list[list[Caixa]], medido: list[list[Caixa]]
) -> tuple[int, int] | None:
    """(achadas, total): das pessoas da referência, quantas o modelo medido também acha.

    `None` quando a referência não achou ninguém — um vídeo sem pessoas não diz nada
    sobre recall, e devolver 100% ali seria o pior jeito de aprovar um modelo.
    """
    if len(referencia) != len(medido):
        raise BenchError("as duas medidas precisam ser do mesmo vídeo, frame a frame")
    achadas = total = 0
    for caixas_ref, caixas_med in zip(referencia, medido, strict=True):
        for caixa in caixas_ref:
            total += 1
            achadas += any(iou(caixa, outra) >= IOU_MESMA_PESSOA for outra in caixas_med)
    return None if total == 0 else (achadas, total)


def cameras_sustentadas(p95_ms: float, fps: float) -> int:
    """Quantas câmeras a `fps` cabem numa thread de inferência (ADR-007), pelo p95.

    Pelo p95 e não pela mediana: a fila do §3.2 descarta quando a inferência atrasa, e
    descarte sustentado é exatamente o que o critério da fase 1 proíbe. É também um teto
    otimista — o tracking, a pose e o decode dividem a mesma CPU, e o portão de
    movimento é que dá a folga (ADR-012).
    """
    if p95_ms <= 0:
        raise BenchError("tempo de inferência tem que ser positivo")
    return math.floor(1000.0 / (p95_ms * fps))


def monta_relatorio(
    modelos: list[Modelo], medidas: list[Medida], *, fps: float, frames: int
) -> dict[str, Any]:
    referencia = medidas[0]
    linhas = []
    for modelo, medida in zip(modelos, medidas, strict=True):
        p50 = float(np.percentile(medida.ms, 50))
        p95 = float(np.percentile(medida.ms, 95))
        recall = recall_relativo(referencia.caixas, medida.caixas)
        linhas.append(
            {
                "modelo": modelo.nome,
                "entrada": modelo.entrada,
                "provider": medida.provider,
                "p50_ms": round(p50, 1),
                "p95_ms": round(p95, 1),
                "pessoas": sum(len(caixas) for caixas in medida.caixas),
                "recall": None if recall is None else round(recall[0] / recall[1], 3),
                "cameras": cameras_sustentadas(p95, fps),
            }
        )
    return {"referencia": modelos[0].nome, "fps": fps, "frames": frames, "modelos": linhas}


def formata_tabela(relatorio: dict[str, Any]) -> str:
    cabecalho = (
        f"{'modelo':<14}{'entrada':>8}  {'provider':<28}{'p50 ms':>8}{'p95 ms':>8}"
        f"{'pessoas':>9}{'recall':>8}{'câmeras':>9}"
    )
    linhas = [
        f"referência do recall: {relatorio['referencia']}  "
        f"({relatorio['frames']} frames a {relatorio['fps']:g} fps)",
        cabecalho,
    ]
    for linha in relatorio["modelos"]:
        recall = "—" if linha["recall"] is None else f"{linha['recall']:.0%}"
        linhas.append(
            f"{linha['modelo']:<14}{linha['entrada']:>8}  {linha['provider']:<28}"
            f"{linha['p50_ms']:>8.1f}{linha['p95_ms']:>8.1f}{linha['pessoas']:>9}"
            f"{recall:>8}{linha['cameras']:>9}"
        )
    return "\n".join(linhas)


def _analisa(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("video", type=Path)
    parser.add_argument(
        "--modelo",
        action="append",
        required=True,
        metavar="ONNX:ENTRADA",
        help="repetível; o primeiro é a referência do recall",
    )
    parser.add_argument("--provider", default="cpu", help="cpu, openvino ou cuda")
    parser.add_argument("--largura", type=int, default=640)
    parser.add_argument("--altura", type=int, default=360)
    parser.add_argument("--fps", type=float, default=3.0)
    parser.add_argument("--max-frames", type=int, default=300)
    parser.add_argument("--saida", type=Path, help="grava o relatório em JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _analisa(sys.argv[1:] if argv is None else argv)
    try:
        modelos = [interpreta_modelo(texto) for texto in args.modelo]
        providers = providers_de(args.provider)
        frames = decodifica(
            args.video,
            largura=args.largura,
            altura=args.altura,
            fps=args.fps,
            max_frames=args.max_frames,
        )
        medidas = []
        for modelo in modelos:
            opcoes = DetectionOptions(
                enabled=True,
                model_path=modelo.caminho,
                input_size=modelo.entrada,
                providers=providers,
            )
            detector = OnnxDetector(modelo.caminho, opcoes)
            try:
                medidas.append(mede(detector, frames))
            finally:
                detector.close()
    except BenchError as erro:
        print(f"bench_deteccao: {erro}", file=sys.stderr)
        return 2

    relatorio = monta_relatorio(modelos, medidas, fps=args.fps, frames=len(frames))
    print(formata_tabela(relatorio))
    if args.saida is not None:
        args.saida.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
