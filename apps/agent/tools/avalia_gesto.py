"""Mede a regra do gesto contra um dataset: quantos furtos pega, quantos alarmes dá.

Roda o mesmo caminho do agente (`qualifica_camera.percorre`: YOLOX, ByteTrack,
RTMPose-s) sobre cada vídeo, passa as poses pelo `MotorGesto` e conta, **por
subpasta**, quantos vídeos tiveram pelo menos um disparo. Num dataset separado em
`Shoplifting/` e `Normal/`, a primeira fração é a sensibilidade e a segunda é a
taxa de alerta falso por vídeo — e só os dois números juntos dizem se a regra presta.

A pose de cada frame vai para um **cache** (`--cache`). Ajustar um limiar da regra
(`--alcance-min`, `--zona-y-max`, ...) e medir de novo leva segundos, sem GPU e sem
os `.onnx`. O cache é refeito quando a taxa de amostragem, a largura ou o tamanho
do vídeo mudam: cache de outra amostragem mediria outra regra.

    .venv/bin/python tools/avalia_gesto.py ~/lince-dados/shoplifting2022 \\
        --cache ~/lince-dados/cache-poses \\
        --detector models/yolox_s.onnx --pose ~/lince-modelos/rtmpose-s.onnx \\
        --saida ~/lince-dados/avaliacao-gesto.json

    # depois, só a regra, com outro limiar:
    .venv/bin/python tools/avalia_gesto.py ~/lince-dados/shoplifting2022 \\
        --cache ~/lince-dados/cache-poses --permanencia 0.5

Dataset encenado mede a regra, não aprova produção: o número que decide limiar é o
da câmera alvo (plano, Fase 0).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Rodado como script, a pasta tools/ já está no sys.path; nos testes, o módulo já
# foi carregado. O caminho de percorrer o vídeo é um só para medir, desenhar e avaliar.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qualifica_camera as qc  # noqa: E402

from lince_agent.config import DetectionOptions  # noqa: E402
from lince_agent.detect.onnx import ModelError as DetectorModelError  # noqa: E402
from lince_agent.detect.onnx import OnnxDetector  # noqa: E402
from lince_agent.gesto.motor import DisparoGesto, MotorGesto, ParametrosGesto  # noqa: E402
from lince_agent.pose.onnx import Junta, OnnxPose, PosePessoa  # noqa: E402
from lince_agent.pose.onnx import ModelError as PoseModelError  # noqa: E402
from lince_agent.pose.worker import PessoaEstimada, ResultadoPose  # noqa: E402

VERSAO_CACHE = 1


@dataclass(frozen=True, slots=True)
class QuadroCacheado:
    instante: float
    sequence: int
    pessoas: tuple[tuple[int, tuple[float, float, float, float], PosePessoa], ...]
    """(track_id, caixa x1 y1 x2 y2, pose) de quem foi para a pose neste frame."""


def _quadro_de(amostra: qc.Amostra) -> QuadroCacheado:
    return QuadroCacheado(
        instante=amostra.instante,
        sequence=amostra.rastreio.sequence,
        pessoas=tuple(
            (caixa.track_id, (caixa.x1, caixa.y1, caixa.x2, caixa.y2), pose)
            for caixa, pose in amostra.pessoas
        ),
    )


def grava_cache(arquivo: Path, quadros: Sequence[QuadroCacheado], *, chave: dict) -> None:
    dados = {
        "versao": VERSAO_CACHE,
        "chave": chave,
        "quadros": [
            {
                "t": q.instante,
                "seq": q.sequence,
                "pessoas": [
                    {
                        "track_id": track_id,
                        "caixa": list(caixa),
                        "juntas": [[j.x, j.y, j.confianca] for j in pose.juntas],
                    }
                    for track_id, caixa, pose in q.pessoas
                ],
            }
            for q in quadros
        ],
    }
    arquivo.parent.mkdir(parents=True, exist_ok=True)
    # Para um temporário ao lado: interromper no meio não pode deixar um cache truncado
    # que a próxima rodada leria como "vídeo sem ninguém".
    parcial = arquivo.with_name(arquivo.name + ".parcial")
    parcial.write_text(json.dumps(dados, allow_nan=True))
    parcial.replace(arquivo)


def le_cache(arquivo: Path, *, chave: dict) -> list[QuadroCacheado] | None:
    """O cache do vídeo, ou `None` se não existe, está corrompido ou é de outra amostragem."""
    try:
        dados = json.loads(arquivo.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(dados, dict) or dados.get("versao") != VERSAO_CACHE:
        return None
    if dados.get("chave") != chave:
        return None
    try:
        return [
            QuadroCacheado(
                instante=float(q["t"]),
                sequence=int(q["seq"]),
                pessoas=tuple(
                    (
                        int(p["track_id"]),
                        tuple(float(v) for v in p["caixa"]),
                        PosePessoa(
                            juntas=tuple(
                                Junta(indice=i, x=float(x), y=float(y), confianca=float(c))
                                for i, (x, y, c) in enumerate(p["juntas"])
                            )
                        ),
                    )
                    for p in q["pessoas"]
                ),
            )
            for q in dados["quadros"]
        ]
    except (KeyError, TypeError, ValueError):
        return None


def disparos_do_video(
    camera_id: str, quadros: Iterable[QuadroCacheado], parametros: ParametrosGesto
) -> list[DisparoGesto]:
    """Um motor novo por vídeo: o estado de um clipe não vaza para o seguinte."""
    motor = MotorGesto(parametros)
    disparos: list[DisparoGesto] = []
    for q in quadros:
        disparos += motor.observa(
            ResultadoPose(
                camera_id=camera_id,
                received_at=q.instante,
                sequence=q.sequence,
                pessoas=tuple(
                    PessoaEstimada(track_id=t, x1=c[0], y1=c[1], x2=c[2], y2=c[3], pose=p)
                    for t, c, p in q.pessoas
                ),
            )
        )
    return disparos


def _pasta(video: str) -> str:
    partes = Path(video).parts
    return partes[0] if len(partes) > 1 else "."


def monta_relatorio(
    por_video: dict[str, tuple[list[DisparoGesto], float]], parametros: ParametrosGesto
) -> dict[str, Any]:
    """`por_video`: nome → (disparos, duração em segundos)."""
    pastas: dict[str, dict[str, Any]] = {}
    for video in sorted(por_video):
        disparos, duracao = por_video[video]
        pasta = pastas.setdefault(
            _pasta(video), {"videos": 0, "com_disparo": 0, "disparos": 0, "duracao_s": 0.0}
        )
        pasta["videos"] += 1
        pasta["com_disparo"] += 1 if disparos else 0
        pasta["disparos"] += len(disparos)
        pasta["duracao_s"] += duracao
    for pasta in pastas.values():
        pasta["fracao_com_disparo"] = pasta["com_disparo"] / pasta["videos"]
        minutos = pasta["duracao_s"] / 60
        pasta["disparos_por_minuto"] = pasta["disparos"] / minutos if minutos > 0 else None
    return {
        "parametros": asdict(parametros),
        "por_pasta": pastas,
        "videos": {
            video: {
                "duracao_s": duracao,
                "disparos": [asdict(d) for d in disparos],
            }
            for video, (disparos, duracao) in sorted(por_video.items())
        },
    }


def formata(relatorio: dict[str, Any]) -> str:
    linhas = [
        f"{'pasta':<20}  {'vídeos':>6}  {'com disparo':>11}  {'fração':>6}  "
        f"{'disparos':>8}  {'por min':>7}",
        "-" * 68,
    ]
    for nome, p in relatorio["por_pasta"].items():
        por_min = "—" if p["disparos_por_minuto"] is None else f"{p['disparos_por_minuto']:.2f}"
        linhas.append(
            f"{nome[:20]:<20}  {p['videos']:>6}  {p['com_disparo']:>11}  "
            f"{p['fracao_com_disparo']:>6.0%}  {p['disparos']:>8}  {por_min:>7}"
        )
    return "\n".join(linhas)


# --- linha de comando ----------------------------------------------------------


def _analisa(argv: list[str]) -> argparse.Namespace:
    padrao = ParametrosGesto()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fonte", type=Path, help="vídeo, ou pasta com subpastas por classe")
    parser.add_argument("--cache", type=Path, help="pasta do cache de poses")
    parser.add_argument("--detector", type=Path, help="YOLOX .onnx (só se faltar cache)")
    parser.add_argument("--pose", type=Path, help="RTMPose-s .onnx (só se faltar cache)")
    parser.add_argument("--provider", choices=("cuda", "cpu"), default="cuda")
    parser.add_argument("--fps", type=float, default=qc.FPS_PADRAO)
    parser.add_argument("--largura", type=int)
    parser.add_argument("--saida", type=Path, help="JSON com cada disparo")
    regra = parser.add_argument_group("regra (defaults do motor)")
    regra.add_argument("--janela", type=float, default=padrao.janela_s)
    regra.add_argument("--alcance-min", type=float, default=padrao.alcance_min)
    regra.add_argument("--zona-x-max", type=float, default=padrao.zona_x_max)
    regra.add_argument("--zona-y-min", type=float, default=padrao.zona_y_min)
    regra.add_argument("--zona-y-max", type=float, default=padrao.zona_y_max)
    regra.add_argument("--permanencia", type=float, default=padrao.permanencia_min_s)
    regra.add_argument("--limiar-junta", type=float, default=padrao.limiar_junta)
    return parser.parse_args(argv)


class _Modelos:
    """Abre detector e pose só na primeira vez que um vídeo não estiver no cache."""

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args
        self.detector: Any = None
        self.pose: Any = None

    def abre(self) -> None:
        if self.detector is not None:
            return
        if self._args.detector is None or self._args.pose is None:
            raise qc.ErroQualificacao(
                "há vídeo sem pose em cache: passe --detector e --pose para calculá-la"
            )
        providers = (
            ("CUDAExecutionProvider", "CPUExecutionProvider")
            if self._args.provider == "cuda"
            else ("CPUExecutionProvider",)
        )
        self.detector = OnnxDetector(
            self._args.detector,
            DetectionOptions(enabled=True, model_path=self._args.detector, providers=providers),
        )
        self.pose = OnnxPose(self._args.pose, providers=providers)

    def fecha(self) -> None:
        for modelo in (self.detector, self.pose):
            if modelo is not None:
                modelo.close()


def main(argv: list[str] | None = None) -> int:
    args = _analisa(sys.argv[1:] if argv is None else argv)
    modelos = _Modelos(args)
    try:
        parametros = ParametrosGesto(
            janela_s=args.janela,
            alcance_min=args.alcance_min,
            zona_x_max=args.zona_x_max,
            zona_y_min=args.zona_y_min,
            zona_y_max=args.zona_y_max,
            permanencia_min_s=args.permanencia,
            limiar_junta=args.limiar_junta,
        )
        if args.fps <= 0:
            raise qc.ErroQualificacao(f"--fps tem que ser positivo, recebi {args.fps}")
        fonte = args.fonte.resolve()
        base = fonte if fonte.is_dir() else fonte.parent
        videos = qc.videos_em(fonte)
        if not videos:
            raise qc.ErroQualificacao(f"nenhum vídeo em {args.fonte}")

        por_video: dict[str, tuple[list[DisparoGesto], float]] = {}
        for numero, video in enumerate(videos, start=1):
            nome = video.relative_to(base).as_posix()
            chave = {"fps": args.fps, "largura": args.largura, "bytes": video.stat().st_size}
            arquivo = None if args.cache is None else args.cache / f"{nome}.json"
            quadros = None if arquivo is None else le_cache(arquivo, chave=chave)
            origem = "cache"
            if quadros is None:
                origem = "modelos"
                modelos.abre()
                quadros = [
                    _quadro_de(a)
                    for a in qc.percorre(
                        video, modelos.detector, modelos.pose, fps=args.fps, largura=args.largura
                    )
                ]
                if arquivo is not None:
                    grava_cache(arquivo, quadros, chave=chave)
            print(  # noqa: T201
                f"[{numero}/{len(videos)}] {nome} ({origem})", file=sys.stderr, flush=True
            )
            duracao = len(quadros) / args.fps
            por_video[nome] = (disparos_do_video(nome, quadros, parametros), duracao)
    except (qc.ErroQualificacao, DetectorModelError, PoseModelError, ValueError) as erro:
        print(f"avalia_gesto: {erro}", file=sys.stderr)  # noqa: T201
        return 1
    finally:
        modelos.fecha()

    relatorio = monta_relatorio(por_video, parametros)
    print(formata(relatorio))  # noqa: T201
    if args.saida is not None:
        args.saida.parent.mkdir(parents=True, exist_ok=True)
        args.saida.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False) + "\n")
        print(f"json em {args.saida}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
