"""Grava o vídeo com caixas, IDs e esqueleto desenhados — a pose conferida a olho.

`qualifica_camera.py` diz se o modelo estava *confiante* sobre o punho; só a
imagem diz se ele *acertou*. Esta ferramenta percorre o vídeo pelo mesmo
caminho da régua (`qualifica_camera.percorre`: YOLOX, ByteTrack, RTMPose-s, só
track confirmado com detecção no frame) e desenha, por cima do frame original:

- a caixa e o `#ID` de cada track com pose, numa cor que é função do ID — cor
  trocando no meio do percurso é troca de ID (R-2);
- os ossos entre juntas que passaram do limiar, e só esses;
- os dois pulsos em destaque: **verde** quando a régua os conta, **vermelho**
  quando a confiança ficou abaixo do limiar, com o número ao lado;
- em cinza, os tracks que não foram para a pose (provisório, perdido, sem
  detecção neste frame), para explicar por que aquela pessoa está sem esqueleto.

    .venv/bin/python tools/desenha_pose.py ~/lince-dados/shoplifting2022 \\
        --saida ~/lince-dados/desenhados \\
        --detector models/yolox_s.onnx --pose ~/lince-modelos/rtmpose-s.onnx \\
        --max-por-pasta 5

Numa pasta, a estrutura de subpastas é espelhada na saída. O vídeo sai em H.264
a 8 fps (a taxa medida), no tamanho do frame original. Vídeo desenhado é mídia
de gente: fica fora do git, como o dataset.
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

import numpy as np

# Rodado como script, a pasta tools/ já está no sys.path; nos testes, o módulo
# já foi carregado. O caminho de percorrer o vídeo é um só para medir e desenhar.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qualifica_camera as qc  # noqa: E402

from lince_agent.config import DetectionOptions  # noqa: E402
from lince_agent.detect.onnx import ModelError as DetectorModelError  # noqa: E402
from lince_agent.detect.onnx import OnnxDetector  # noqa: E402
from lince_agent.pose.onnx import JUNTAS_COCO17, OnnxPose, PosePessoa  # noqa: E402
from lince_agent.pose.onnx import ModelError as PoseModelError  # noqa: E402

_J = JUNTAS_COCO17.index
ESQUELETO: tuple[tuple[int, int], ...] = (
    (_J("ombro_esquerdo"), _J("ombro_direito")),
    (_J("ombro_esquerdo"), _J("cotovelo_esquerdo")),
    (_J("cotovelo_esquerdo"), _J("pulso_esquerdo")),
    (_J("ombro_direito"), _J("cotovelo_direito")),
    (_J("cotovelo_direito"), _J("pulso_direito")),
    (_J("ombro_esquerdo"), _J("quadril_esquerdo")),
    (_J("ombro_direito"), _J("quadril_direito")),
    (_J("quadril_esquerdo"), _J("quadril_direito")),
    (_J("quadril_esquerdo"), _J("joelho_esquerdo")),
    (_J("joelho_esquerdo"), _J("tornozelo_esquerdo")),
    (_J("quadril_direito"), _J("joelho_direito")),
    (_J("joelho_direito"), _J("tornozelo_direito")),
    (_J("nariz"), _J("olho_esquerdo")),
    (_J("nariz"), _J("olho_direito")),
    (_J("olho_esquerdo"), _J("orelha_esquerda")),
    (_J("olho_direito"), _J("orelha_direita")),
)
"""Os ossos do COCO-17. Índices pelos nomes de `JUNTAS_COCO17`: esquerdo é o da
pessoa, e um índice digitado à mão trocaria o braço desenhado sem erro."""

VERDE = (60, 220, 60)
VERMELHO = (40, 40, 230)
CINZA = (150, 150, 150)
RAIO_PULSO = 7

_PALETA = (
    (255, 160, 0),
    (0, 200, 255),
    (255, 0, 200),
    (120, 255, 120),
    (200, 120, 255),
    (0, 255, 255),
    (255, 255, 0),
    (80, 140, 255),
    (180, 255, 0),
    (255, 90, 120),
)
"""BGR. Nenhuma é verde puro, vermelho puro ou cinza: essas três cores têm
significado próprio no desenho."""


def cor_do_track(track_id: int) -> tuple[int, int, int]:
    return _PALETA[(track_id - 1) % len(_PALETA)]


def _ponto(pose: PosePessoa, indice: int, limiar: float) -> tuple[int, int] | None:
    junta = pose.juntas[indice]
    if not (
        math.isfinite(junta.confianca)
        and math.isfinite(junta.x)
        and math.isfinite(junta.y)
        and junta.confianca >= limiar
    ):
        return None
    return int(round(junta.x)), int(round(junta.y))


def _desenha_pulso(imagem: np.ndarray, pose: PosePessoa, indice: int, limiar: float) -> None:
    import cv2

    junta = pose.juntas[indice]
    if not (math.isfinite(junta.x) and math.isfinite(junta.y) and math.isfinite(junta.confianca)):
        return
    centro = int(round(junta.x)), int(round(junta.y))
    cor = VERDE if junta.confianca >= limiar else VERMELHO
    cv2.circle(imagem, centro, RAIO_PULSO, cor, 2, cv2.LINE_AA)
    cv2.putText(
        imagem,
        f"{junta.confianca:.2f}",
        (centro[0] + RAIO_PULSO + 2, centro[1] + 4),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.4,
        cor,
        1,
        cv2.LINE_AA,
    )


def desenha(amostra: qc.Amostra, *, limiar: float) -> np.ndarray:
    """Uma cópia do frame com tudo desenhado. O original fica intacto."""
    import cv2

    imagem = np.array(amostra.imagem, copy=True)
    com_pose = {caixa.track_id for caixa, _ in amostra.pessoas}

    for t in amostra.rastreio.tracks:
        if t.track_id in com_pose or not all(math.isfinite(v) for v in (t.x1, t.y1, t.x2, t.y2)):
            continue
        cv2.rectangle(
            imagem,
            (int(round(t.x1)), int(round(t.y1))),
            (int(round(t.x2)), int(round(t.y2))),
            CINZA,
            1,
        )

    for caixa, pose in amostra.pessoas:
        cor = cor_do_track(caixa.track_id)
        canto = (int(round(caixa.x1)), int(round(caixa.y1)))
        cv2.rectangle(imagem, canto, (int(round(caixa.x2)), int(round(caixa.y2))), cor, 2)
        cv2.putText(
            imagem,
            f"#{caixa.track_id}",
            (canto[0], max(12, canto[1] - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            cor,
            1,
            cv2.LINE_AA,
        )
        for a, b in ESQUELETO:
            pa, pb = _ponto(pose, a, limiar), _ponto(pose, b, limiar)
            if pa is not None and pb is not None:
                cv2.line(imagem, pa, pb, cor, 2, cv2.LINE_AA)
        for indice in range(len(pose.juntas)):
            if indice in qc.PULSOS:
                continue
            ponto = _ponto(pose, indice, limiar)
            if ponto is not None:
                cv2.circle(imagem, ponto, 3, cor, -1, cv2.LINE_AA)
        for indice in qc.PULSOS:
            _desenha_pulso(imagem, pose, indice, limiar)

    cv2.putText(
        imagem,
        f"{amostra.instante:6.2f}s  pose={len(amostra.pessoas)}  "
        f"tracks={len(amostra.rastreio.tracks)}",
        (8, 18),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (255, 255, 255),
        1,
        cv2.LINE_AA,
    )
    return imagem


def _grava(saida: Path, frames: Iterable[np.ndarray], *, fps: float) -> int:
    """H.264 por pipe, frame a frame. Abre o ffmpeg só no primeiro frame, que é
    quando o tamanho é conhecido."""
    saida.parent.mkdir(parents=True, exist_ok=True)
    processo: subprocess.Popen[bytes] | None = None
    contados = 0
    try:
        for imagem in frames:
            if processo is None:
                altura, largura = imagem.shape[:2]
                processo = subprocess.Popen(  # noqa: S603
                    [
                        qc._binario("ffmpeg"),
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "bgr24",
                        "-s",
                        f"{largura}x{altura}",
                        "-r",
                        f"{fps:g}",
                        "-i",
                        "pipe:0",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-pix_fmt",
                        "yuv420p",
                        str(saida),
                    ],
                    stdin=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            assert processo.stdin is not None
            processo.stdin.write(np.ascontiguousarray(imagem).tobytes())
            contados += 1
    finally:
        if processo is not None:
            assert processo.stdin is not None
            processo.stdin.close()
            erro = processo.stderr.read().decode("utf-8", "replace") if processo.stderr else ""
            if processo.wait() != 0:
                raise qc.ErroQualificacao(f"ffmpeg falhou ao gravar {saida.name}: {erro.strip()}")
    return contados


def desenha_video(
    video: Path,
    saida: Path,
    detector: qc.Detector,
    pose: qc.Pose,
    *,
    fps: float,
    limiar: float,
    largura: int | None = None,
) -> int:
    """Percorre o vídeo como a régua e grava o desenhado. Devolve quantos frames."""
    amostras = qc.percorre(video, detector, pose, fps=fps, largura=largura)
    return _grava(saida, (desenha(a, limiar=limiar) for a in amostras), fps=fps)


def _escolhe(videos: list[Path], base: Path, maximo: int | None) -> list[Path]:
    """No máximo `maximo` por subpasta, para a amostra ter de todas as classes."""
    if maximo is None:
        return videos
    contagem: dict[Path, int] = {}
    escolhidos = []
    for video in videos:
        pasta = video.relative_to(base).parent
        if contagem.get(pasta, 0) < maximo:
            escolhidos.append(video)
            contagem[pasta] = contagem.get(pasta, 0) + 1
    return escolhidos


def _analisa(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fonte", type=Path, help="vídeo, ou pasta com vídeos")
    parser.add_argument(
        "--saida", type=Path, required=True, help="arquivo .mp4, ou pasta quando a fonte é pasta"
    )
    parser.add_argument("--detector", type=Path, required=True, help="YOLOX .onnx")
    parser.add_argument("--pose", type=Path, required=True, help="RTMPose-s COCO-17 .onnx")
    parser.add_argument("--provider", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--fps", type=float, default=qc.FPS_PADRAO)
    parser.add_argument("--largura", type=int)
    parser.add_argument("--limiar-punho", type=float, default=qc.Criterios().limiar_punho)
    parser.add_argument("--max-por-pasta", type=int, help="desenha só os N primeiros de cada pasta")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _analisa(sys.argv[1:] if argv is None else argv)
    providers = (
        ("CUDAExecutionProvider", "CPUExecutionProvider")
        if args.provider == "cuda"
        else ("CPUExecutionProvider",)
    )
    try:
        if args.fps <= 0:
            raise qc.ErroQualificacao(f"--fps tem que ser positivo, recebi {args.fps}")
        fonte = args.fonte.resolve()
        saida = args.saida.resolve()
        if fonte.is_dir() and saida.is_relative_to(fonte):
            raise qc.ErroQualificacao(
                f"--saida {args.saida} está dentro da fonte: a próxima rodada desenharia "
                "por cima dos desenhos"
            )
        base = fonte if fonte.is_dir() else fonte.parent
        videos = _escolhe(qc.videos_em(fonte), base, args.max_por_pasta)
        if not videos:
            raise qc.ErroQualificacao(f"nenhum vídeo em {args.fonte}")
        detector = OnnxDetector(
            args.detector,
            DetectionOptions(enabled=True, model_path=args.detector, providers=providers),
        )
        pose = OnnxPose(args.pose, providers=providers)
        try:
            info = detector.info()
            assert info is not None
            for numero, video in enumerate(videos, start=1):
                destino = saida / video.relative_to(base) if fonte.is_dir() else saida
                destino = destino.with_suffix(".mp4")
                print(  # noqa: T201
                    f"[{numero}/{len(videos)}] {video.relative_to(base)} → {destino}",
                    file=sys.stderr,
                    flush=True,
                )
                desenha_video(
                    video,
                    destino,
                    detector,
                    pose,
                    fps=args.fps,
                    limiar=args.limiar_punho,
                    largura=args.largura,
                )
        finally:
            detector.close()
            pose.close()
    except (qc.ErroQualificacao, DetectorModelError, PoseModelError) as erro:
        print(f"desenha_pose: {erro}", file=sys.stderr)  # noqa: T201
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
