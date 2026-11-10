"""Mede se uma câmera serve para o piloto do gesto (Fase 0), fora da suíte.

O plano recusa a câmera antes de qualquer modelo novo: se o corredor não mostra
a mão, o classificador treina em ruído e a sombra dispara à toa — e a conclusão
errada seria "o modelo é ruim", quando o problema era o ângulo. Esta ferramenta
roda o que o agente já tem (YOLOX, ByteTrack, RTMPose-s) sobre uma gravação e
devolve os números da régua:

- **resolução** do frame medido: o plano pede 1280×720 no pipe de detecção;
- **altura da pessoa**: mediana da caixa de cada track, ≥ 180 px;
- **punho**: fração dos tracks com mais de 1 s em que um dos pulsos aparece com
  confiança em pelo menos metade das amostras. Menos da metade dos tracks, e a
  câmera não entra.

Mede **no frame que o agente realmente vê**. Sem `--largura`, é o do arquivo;
com ela, o ffmpeg escala como o `scale` da ingestão faria. O YOLOX-s só aceita
lado maior 640, então o detector recebe uma cópia reduzida e as caixas voltam
para o frame original — onde a pose recorta e onde a altura é medida.

Serve a uma gravação do corredor de uma loja, que é o que decide a Fase 0, e a
datasets públicos, que **não** decidem nada: medem se a régua e o pipeline
funcionam, e quão longe a resolução deles está da exigida.

    uv run python tools/qualifica_camera.py ~/lince-dados/corredor.mp4 \\
        --detector models/yolox_s.onnx \\
        --pose ~/lince-modelos/rtmpose-s.onnx \\
        --saida ~/lince-dados/qualificacao.json

`fonte` pode ser um vídeo ou uma pasta; numa pasta, os vídeos das subpastas
entram, e o relatório separa por subpasta (`normal/`, `shoplifting/`).
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import statistics
import subprocess
import sys
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from lince_agent.config import DetectionOptions, PixelFormat, TrackingOptions
from lince_agent.detect.onnx import ModelError as DetectorModelError
from lince_agent.detect.onnx import OnnxDetector
from lince_agent.detect.state import Detection, DetectionResult
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.adaptador import caixas_para_pose
from lince_agent.pose.onnx import JUNTAS_COCO17, OnnxPose, PosePessoa
from lince_agent.pose.onnx import ModelError as PoseModelError
from lince_agent.pose.worker import CaixaPose
from lince_agent.track.pool import TrackerPool
from lince_agent.track.state import TrackingResult

FPS_PADRAO = 8.0
"""A amostra do piloto do gesto (seção 2 do plano). O gesto dura 0,5 a 2 s: a
3 fps sobram 2 a 6 amostras, a 8 fps sobram 4 a 16."""

PULSOS = (JUNTAS_COCO17.index("pulso_esquerdo"), JUNTAS_COCO17.index("pulso_direito"))

EXTENSOES = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".mpg", ".mpeg", ".m4v", ".ts"}


@dataclass(frozen=True, slots=True)
class Criterios:
    """A régua da Fase 0. Os números são os do plano, não preferência.

    Mudar um deles é mudar o portão: tem que ser decisão escrita, e o teste
    `test_numeros_da_regua_sao_os_do_plano` existe para isso não escorregar.
    """

    altura_min_px: float = 180
    """Pessoa em pé no meio do corredor. O RTMPose-s come um recorte de 256×192;
    caixa de 80 px não tem punho."""

    fracao_tracks_min: float = 0.5
    """"Se menos da metade dos tracks com mais de 1 s tiver punho com confiança
    decente, a câmera não entra no piloto." """

    duracao_min_s: float = 1.0
    largura_min: int = 1280
    altura_min: int = 720

    limiar_punho: float = 0.3
    """Confiança SimCC a partir da qual o pulso conta como visto. 0,3 é o
    `kpt_thr` da visualização do MMPose para o RTMPose; não é um número do
    plano, e por isso é flag."""

    fracao_punho_min: float = 0.5
    """Fração das amostras de um track com o pulso visto para o track contar
    como "punho utilizável"."""


@dataclass(frozen=True, slots=True)
class TrackMedido:
    track_id: int
    duracao_s: float
    amostras: int
    altura_mediana_px: float
    fracao_punho: float

    def punho_utilizavel(self, criterios: Criterios) -> bool:
        return self.fracao_punho >= criterios.fracao_punho_min


@dataclass(slots=True)
class _Historico:
    instantes: list[float] = field(default_factory=list)
    alturas: list[float] = field(default_factory=list)
    pulsos: list[float] = field(default_factory=list)
    """A maior confiança entre os dois pulsos, por amostra; `nan` se nenhum tem número."""


def melhor_pulso(pose: PosePessoa) -> float:
    """A confiança do pulso mais visível. `nan` quando nenhum dos dois tem número.

    Filtra antes do `max`: `max(nan, 0.8)` dá `nan` ou `0.8` conforme a ordem, e
    uma junta sem número não pode virar pulso visto por acidente de comparação.
    """
    validos = [
        pose.juntas[i].confianca
        for i in PULSOS
        if i < len(pose.juntas) and math.isfinite(pose.juntas[i].confianca)
    ]
    return max(validos) if validos else float("nan")


class Medidor:
    """Acumula, por track, a altura da caixa e o pulso de cada amostra."""

    def __init__(self) -> None:
        self._tracks: dict[int, _Historico] = {}

    def observa(self, track_id: int, instante: float, altura_px: float, pose: PosePessoa) -> None:
        historico = self._tracks.setdefault(track_id, _Historico())
        historico.instantes.append(instante)
        historico.alturas.append(altura_px)
        historico.pulsos.append(melhor_pulso(pose))

    def tracks_longos(self, criterios: Criterios) -> list[TrackMedido]:
        medidos = []
        for track_id in sorted(self._tracks):
            h = self._tracks[track_id]
            duracao = max(h.instantes) - min(h.instantes)
            if duracao < criterios.duracao_min_s:
                continue
            vistos = sum(1 for c in h.pulsos if math.isfinite(c) and c >= criterios.limiar_punho)
            medidos.append(
                TrackMedido(
                    track_id=track_id,
                    duracao_s=duracao,
                    amostras=len(h.instantes),
                    altura_mediana_px=float(statistics.median(h.alturas)),
                    fracao_punho=vistos / len(h.pulsos),
                )
            )
        return medidos


@dataclass(frozen=True, slots=True)
class Resumo:
    resolucao: tuple[int, int]
    tracks_longos: int
    altura_mediana_px: float | None
    """Mediana das alturas medianas dos tracks longos."""

    fracao_punho_utilizavel: float | None
    passa: bool
    motivos: tuple[str, ...]
    """Por que reprovou. Vem junto com os números, não no lugar deles: um vídeo
    480p reprova pela resolução e ainda diz quanto do punho se via."""


def avalia(
    tracks: Sequence[TrackMedido], resolucao: tuple[int, int], criterios: Criterios
) -> Resumo:
    motivos: list[str] = []
    largura, altura = resolucao
    if max(largura, altura) < criterios.largura_min or min(largura, altura) < criterios.altura_min:
        motivos.append(
            f"resolução {largura}×{altura} abaixo de "
            f"{criterios.largura_min}×{criterios.altura_min} (720p)"
        )

    if not tracks:
        motivos.append(
            f"nenhum track com mais de {criterios.duracao_min_s:g} s: sem gente, "
            "nada a medir — não é aprovação"
        )
        return Resumo(resolucao, 0, None, None, False, tuple(motivos))

    altura_mediana = float(statistics.median(t.altura_mediana_px for t in tracks))
    utilizaveis = sum(1 for t in tracks if t.punho_utilizavel(criterios)) / len(tracks)
    if altura_mediana < criterios.altura_min_px:
        motivos.append(
            f"pessoa com {altura_mediana:.0f} px de mediana; o piloto pede ≥ "
            f"{criterios.altura_min_px:.0f} px"
        )
    if utilizaveis < criterios.fracao_tracks_min:
        motivos.append(
            f"punho utilizável em {utilizaveis:.0%} dos tracks longos; o piloto pede ≥ "
            f"{criterios.fracao_tracks_min:.0%}"
        )
    return Resumo(
        resolucao=resolucao,
        tracks_longos=len(tracks),
        altura_mediana_px=altura_mediana,
        fracao_punho_utilizavel=utilizaveis,
        passa=not motivos,
        motivos=tuple(motivos),
    )


# --- o vídeo -------------------------------------------------------------------


class ErroQualificacao(Exception):
    """Entrada que não dá para medir. Sempre com o que fazer."""


def _binario(nome: str) -> str:
    caminho = shutil.which(nome)
    if caminho is None:
        raise ErroQualificacao(f"{nome} não está no PATH; é por ele que o vídeo é lido")
    return caminho


def _dimensoes(video: Path) -> tuple[int, int]:
    resultado = subprocess.run(  # noqa: S603 - argv montado aqui, arquivo local
        [
            _binario("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=s=x:p=0",
            str(video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    texto = resultado.stdout.strip()
    if resultado.returncode != 0 or not texto:
        detalhe = resultado.stderr.strip() or "sem stream de vídeo"
        raise ErroQualificacao(f"ffprobe falhou em {video.name}: {detalhe}")
    largura, altura = texto.splitlines()[0].split("x", 1)
    return int(largura), int(altura)


def _par(valor: float) -> int:
    return max(2, int(round(valor / 2)) * 2)


def frames_de(
    video: Path, *, fps: float, largura: int | None = None
) -> Iterator[tuple[float, np.ndarray]]:
    """Os frames BGR do vídeo, amostrados a `fps`, com o instante em segundos.

    Lê do pipe do ffmpeg frame a frame, sem carregar o vídeo na memória: uma
    gravação de 10 minutos a 720p tem mais de 6 GB de pixels crus.
    """
    origem_l, origem_a = _dimensoes(video)
    if largura is None:
        destino_l, destino_a = origem_l, origem_a
    else:
        destino_l, destino_a = largura, _par(origem_a * largura / origem_l)
    filtros = f"fps={fps}"
    if (destino_l, destino_a) != (origem_l, origem_a):
        filtros += f",scale={destino_l}:{destino_a}"
    quadro = destino_l * destino_a * 3

    processo = subprocess.Popen(  # noqa: S603
        [
            _binario("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(video),
            "-vf",
            filtros,
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert processo.stdout is not None
    indice = 0
    try:
        while True:
            dados = processo.stdout.read(quadro)
            if len(dados) < quadro:
                break
            imagem = np.frombuffer(dados, dtype=np.uint8).reshape(destino_a, destino_l, 3)
            yield indice / fps, imagem
            indice += 1
    finally:
        processo.stdout.close()
        erro = processo.stderr.read().decode("utf-8", "replace").strip() if processo.stderr else ""
        if processo.stderr:
            processo.stderr.close()
        codigo = processo.wait()
    if codigo != 0 and indice == 0:
        raise ErroQualificacao(f"ffmpeg falhou em {video.name}: {erro or codigo}")


def videos_em(fonte: Path) -> list[Path]:
    """Os vídeos de uma pasta, recursivamente, em ordem estável."""
    if fonte.is_file():
        return [fonte]
    if not fonte.is_dir():
        raise ErroQualificacao(f"não encontrei vídeo nem pasta em {fonte}")
    achados = [p for p in fonte.rglob("*") if p.is_file() and p.suffix.lower() in EXTENSOES]
    return sorted(achados, key=lambda p: p.relative_to(fonte).as_posix())


def _como_frame(imagem: np.ndarray, sequence: int) -> Frame:
    return Frame(
        data=np.ascontiguousarray(imagem).tobytes(),
        width=int(imagem.shape[1]),
        height=int(imagem.shape[0]),
        pixel_format=PixelFormat.BGR24,
        sequence=sequence,
        received_at=0.0,
    )


def _reduz_para_o_detector(imagem: np.ndarray, lado: int) -> tuple[np.ndarray, float, float]:
    """Cópia com o lado maior igual ao do detector, e as escalas para voltar."""
    altura, largura = imagem.shape[:2]
    if max(largura, altura) == lado:
        return imagem, 1.0, 1.0
    import cv2

    if largura >= altura:
        destino = (lado, max(1, round(altura * lado / largura)))
    else:
        destino = (max(1, round(largura * lado / altura)), lado)
    # INTER_AREA ao reduzir: o bilinear pula pixels e a pessoa ao fundo vira aliasing.
    reduzida = cv2.resize(imagem, destino, interpolation=cv2.INTER_AREA)
    return reduzida, largura / destino[0], altura / destino[1]


class Detector(Protocol):
    def detect(self, frame: Frame) -> tuple[Detection, ...]: ...


class Pose(Protocol):
    def estimar(self, imagem: np.ndarray, caixas: Sequence[Any]) -> tuple[PosePessoa, ...]: ...


@dataclass(frozen=True, slots=True)
class Amostra:
    """Um frame percorrido: a imagem, o que o tracker viu e a pose de quem entrou."""

    instante: float
    imagem: np.ndarray
    rastreio: TrackingResult
    pessoas: tuple[tuple[CaixaPose, PosePessoa], ...]


def percorre(
    video: Path,
    detector: Detector,
    pose: Pose,
    *,
    fps: float,
    lado_detector: int,
    largura: int | None = None,
    tracking: TrackingOptions | None = None,
) -> Iterator[Amostra]:
    """Detecção, tracking e pose sobre o vídeo, como o runtime faria com a pose ligada.

    Só vai para a pose track confirmado com detecção no frame — `caixas_para_pose`,
    a mesma regra do adaptador do agente. Medir ou desenhar a caixa prevista pelo
    Kalman mostraria um lugar onde a pessoa não está.

    É o mesmo percurso para medir (`processa_video`) e para desenhar
    (`desenha_pose.py`): o que se confere a olho tem que ser o que se mediu.
    """
    rastreio = TrackerPool(tracking or TrackingOptions())
    for sequencia, (instante, imagem) in enumerate(frames_de(video, fps=fps, largura=largura)):
        reduzida, escala_x, escala_y = _reduz_para_o_detector(imagem, lado_detector)
        deteccoes = tuple(
            Detection(
                class_id=d.class_id,
                score=d.score,
                x1=d.x1 * escala_x,
                y1=d.y1 * escala_y,
                x2=d.x2 * escala_x,
                y2=d.y2 * escala_y,
            )
            for d in detector.detect(_como_frame(reduzida, sequencia))
        )
        resultado = rastreio.update(
            DetectionResult(
                camera_id=video.name,
                sequence=sequencia,
                received_at=instante,
                detections=deteccoes,
                inference_ms=0.0,
            )
        )
        caixas = caixas_para_pose(resultado)
        poses = pose.estimar(imagem, [caixa.xyxy for caixa in caixas]) if caixas else ()
        yield Amostra(
            instante=instante,
            imagem=imagem,
            rastreio=resultado,
            pessoas=tuple(zip(caixas, poses, strict=True)),
        )


@dataclass(frozen=True, slots=True)
class VideoMedido:
    resolucao: tuple[int, int]
    frames: int
    tracks: list[TrackMedido]


def processa_video(
    video: Path,
    detector: Detector,
    pose: Pose,
    *,
    fps: float,
    lado_detector: int,
    largura: int | None = None,
    criterios: Criterios | None = None,
    tracking: TrackingOptions | None = None,
) -> VideoMedido:
    """Mede a régua da Fase 0 em cima de `percorre`."""
    criterios = criterios or Criterios()
    medidor = Medidor()
    resolucao = (0, 0)
    frames = 0
    for amostra in percorre(
        video,
        detector,
        pose,
        fps=fps,
        lado_detector=lado_detector,
        largura=largura,
        tracking=tracking,
    ):
        frames += 1
        resolucao = (int(amostra.imagem.shape[1]), int(amostra.imagem.shape[0]))
        for caixa, estimada in amostra.pessoas:
            medidor.observa(caixa.track_id, amostra.instante, caixa.y2 - caixa.y1, estimada)
    return VideoMedido(resolucao=resolucao, frames=frames, tracks=medidor.tracks_longos(criterios))


# --- o relatório ---------------------------------------------------------------


def _resumo_dict(resumo: Resumo) -> dict[str, Any]:
    dados = asdict(resumo)
    dados["resolucao"] = list(resumo.resolucao)
    dados["motivos"] = list(resumo.motivos)
    return dados


def _menor(resolucoes: Sequence[tuple[int, int]]) -> tuple[int, int]:
    """A pior resolução vista. Somar 720p com 480p e declarar 720p aprovaria a câmera errada."""
    return min(resolucoes, key=lambda r: (min(r), max(r)))


def _pasta(video: Path) -> str:
    return video.parts[0] if len(video.parts) > 1 else "."


def monta_relatorio(
    resumos: dict[Path, Resumo],
    tracks: dict[Path, list[TrackMedido]],
    resolucoes: dict[Path, tuple[int, int]],
    criterios: Criterios,
) -> dict[str, Any]:
    pastas: dict[str, list[Path]] = {}
    for video in sorted(resumos, key=lambda p: p.as_posix()):
        pastas.setdefault(_pasta(video), []).append(video)

    def agrega(videos: list[Path]) -> dict[str, Any]:
        juntos = [t for v in videos for t in tracks[v]]
        return _resumo_dict(avalia(juntos, _menor([resolucoes[v] for v in videos]), criterios))

    return {
        "criterios": asdict(criterios),
        "videos": {
            v.as_posix(): {**_resumo_dict(resumos[v]), "tracks": [asdict(t) for t in tracks[v]]}
            for v in sorted(resumos, key=lambda p: p.as_posix())
        },
        "por_pasta": {nome: agrega(videos) for nome, videos in pastas.items()},
        "total": agrega(list(resumos)),
    }


def formata(relatorio: dict[str, Any]) -> str:
    def linha(nome: str, r: dict[str, Any]) -> str:
        largura, altura = r["resolucao"]
        mediana = "—" if r["altura_mediana_px"] is None else f"{r['altura_mediana_px']:.0f}"
        punho = (
            "—" if r["fracao_punho_utilizavel"] is None else f"{r['fracao_punho_utilizavel']:.0%}"
        )
        veredito = "PASSA" if r["passa"] else "não passa"
        return (
            f"{nome[:40]:<40}  {largura:>4}×{altura:<4}  {r['tracks_longos']:>6}  "
            f"{mediana:>7}  {punho:>6}  {veredito}"
        )

    cabecalho = (
        f"{'vídeo / pasta':<40}  {'resolução':>9}  {'tracks':>6}  "
        f"{'altura':>7}  {'punho':>6}  veredito"
    )
    linhas = [cabecalho, "-" * len(cabecalho)]
    linhas += [linha(nome, r) for nome, r in relatorio["videos"].items()]
    linhas.append("-" * len(cabecalho))
    if len(relatorio["por_pasta"]) > 1:
        linhas += [linha(f"[{nome}]", r) for nome, r in relatorio["por_pasta"].items()]
    linhas.append(linha("TOTAL", relatorio["total"]))
    for motivo in relatorio["total"]["motivos"]:
        linhas.append(f"  - {motivo}")
    return "\n".join(linhas)


# --- linha de comando ----------------------------------------------------------


def _analisa(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fonte", type=Path, help="vídeo, ou pasta com vídeos")
    parser.add_argument("--detector", type=Path, required=True, help="YOLOX .onnx")
    parser.add_argument("--pose", type=Path, required=True, help="RTMPose-s COCO-17 .onnx")
    parser.add_argument("--provider", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--fps", type=float, default=FPS_PADRAO)
    parser.add_argument(
        "--largura",
        type=int,
        help="escala para esta largura antes de medir, como o `scale` da ingestão; "
        "sem ela, mede no tamanho do arquivo",
    )
    parser.add_argument("--limiar-punho", type=float, default=Criterios().limiar_punho)
    parser.add_argument("--saida", type=Path, help="JSON com os números de cada track")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _analisa(sys.argv[1:] if argv is None else argv)
    criterios = Criterios(limiar_punho=args.limiar_punho)
    providers = (
        ("CUDAExecutionProvider", "CPUExecutionProvider")
        if args.provider == "cuda"
        else ("CPUExecutionProvider",)
    )
    try:
        if args.fps <= 0:
            raise ErroQualificacao(f"--fps tem que ser positivo, recebi {args.fps}")
        videos = videos_em(args.fonte)
        if not videos:
            raise ErroQualificacao(f"nenhum vídeo em {args.fonte}")
        detector = OnnxDetector(
            args.detector,
            DetectionOptions(enabled=True, model_path=args.detector, providers=providers),
        )
        pose = OnnxPose(args.pose, providers=providers)
        try:
            info = detector.info()
            assert info is not None
            print(  # noqa: T201
                f"detector em {info.provider}, pose em {pose.info().provider}; "
                f"{len(videos)} vídeo(s) a {args.fps:g} fps",
                file=sys.stderr,
            )
            base = args.fonte if args.fonte.is_dir() else args.fonte.parent
            resumos: dict[Path, Resumo] = {}
            tracks: dict[Path, list[TrackMedido]] = {}
            resolucoes: dict[Path, tuple[int, int]] = {}
            for numero, video in enumerate(videos, start=1):
                nome = video.relative_to(base)
                print(f"[{numero}/{len(videos)}] {nome}", file=sys.stderr, flush=True)  # noqa: T201
                medido = processa_video(
                    video,
                    detector,
                    pose,
                    fps=args.fps,
                    lado_detector=info.input_size,
                    largura=args.largura,
                    criterios=criterios,
                )
                if medido.frames == 0:
                    print("  sem frames, pulado", file=sys.stderr)  # noqa: T201
                    continue
                tracks[nome] = medido.tracks
                resolucoes[nome] = medido.resolucao
                resumos[nome] = avalia(medido.tracks, medido.resolucao, criterios)
        finally:
            detector.close()
            pose.close()
        if not resumos:
            raise ErroQualificacao("nenhum vídeo produziu frames")
    except (ErroQualificacao, DetectorModelError, PoseModelError) as erro:
        print(f"qualifica_camera: {erro}", file=sys.stderr)  # noqa: T201
        return 1

    relatorio = monta_relatorio(resumos, tracks, resolucoes, criterios)
    print(formata(relatorio))  # noqa: T201
    if args.saida is not None:
        args.saida.parent.mkdir(parents=True, exist_ok=True)
        args.saida.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False) + "\n")
        print(f"json em {args.saida}")  # noqa: T201
    return 0 if relatorio["total"]["passa"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
