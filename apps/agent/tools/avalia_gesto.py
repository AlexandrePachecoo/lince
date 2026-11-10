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
import itertools
import json
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

# Rodado como script, a pasta tools/ já está no sys.path; nos testes, o módulo já
# foi carregado. O caminho de percorrer o vídeo é um só para medir, desenhar e avaliar.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import qualifica_camera as qc  # noqa: E402

from lince_agent.config import DetectionOptions  # noqa: E402
from lince_agent.detect.onnx import ModelError as DetectorModelError  # noqa: E402
from lince_agent.detect.onnx import OnnxDetector  # noqa: E402
from lince_agent.gesto.motor import (  # noqa: E402
    DisparoGesto,
    MotorGesto,
    ParametrosGesto,
    estados_dos_pulsos,
)
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


# --- onde a regra parou ------------------------------------------------------------

ETAPAS = (
    "sem_pessoa",
    "sem_tronco",
    "sem_alcance",
    "sem_zona",
    "zona_sem_alcance_antes",
    "alcance_e_zona_sem_disparo",
    "disparou",
)
"""Até onde cada vídeo chegou na regra, da etapa mais cedo à mais tarde.

Uma fração de furtos pegos baixa não diz o que mexer. Furto parado em `sem_alcance`
pede `--alcance-min` menor; parado em `sem_zona`, a zona da bolsa no lugar errado;
em `zona_sem_alcance_antes`, janela curta ou alcance que nunca é visto; e em
`alcance_e_zona_sem_disparo`, permanência exigente demais ou a mão que some antes de
entrar na zona. Se `Normal` chega às mesmas etapas que `Shoplifting`, nenhum limiar
separa os dois — e isso também é resposta.
"""

ESTADOS = ("longe", "zona", "meio", "sumido", "sem_tronco")

_FOLGA_S = 1e-6


def diagnostica_video(
    quadros: Iterable[QuadroCacheado], parametros: ParametrosGesto, *, disparou: bool
) -> tuple[str, Counter[str]]:
    """A etapa a que o vídeo chegou e quantas amostras de pulso caíram em cada estado.

    A etapa é a melhor de qualquer track e de qualquer pulso: basta uma pessoa ter
    alcançado e depois levado a mão à zona para o vídeo passar de `sem_zona`.
    """
    contagem: Counter[str] = Counter()
    series: dict[tuple[int, str], list[tuple[float, str]]] = defaultdict(list)
    for q in quadros:
        for track_id, _caixa, pose in q.pessoas:
            for lado, estado in estados_dos_pulsos(pose, parametros):
                contagem[estado] += 1
                series[(track_id, lado)].append((q.instante, estado))
    if disparou:
        return "disparou", contagem
    if not contagem:
        return "sem_pessoa", contagem
    if contagem["sem_tronco"] == sum(contagem.values()):
        return "sem_tronco", contagem
    if contagem["longe"] == 0:
        return "sem_alcance", contagem
    if contagem["zona"] == 0:
        return "sem_zona", contagem
    for serie in series.values():
        ultimo_longe: float | None = None
        for instante, estado in serie:
            if estado == "longe":
                ultimo_longe = instante
            elif (
                estado == "zona"
                and ultimo_longe is not None
                and instante - ultimo_longe <= parametros.janela_s + _FOLGA_S
            ):
                return "alcance_e_zona_sem_disparo", contagem
    return "zona_sem_alcance_antes", contagem


def monta_diagnostico(
    por_video: dict[str, tuple[str, Counter[str]]],
) -> dict[str, dict[str, Any]]:
    """Por pasta: quantos vídeos pararam em cada etapa e a fração de amostras em cada estado."""
    pastas: dict[str, dict[str, Any]] = {}
    for video in sorted(por_video):
        etapa, contagem = por_video[video]
        pasta = pastas.setdefault(
            _pasta(video),
            {"etapas": dict.fromkeys(ETAPAS, 0), "estados": dict.fromkeys(ESTADOS, 0)},
        )
        pasta["etapas"][etapa] += 1
        for estado in ESTADOS:
            pasta["estados"][estado] += contagem[estado]
    for pasta in pastas.values():
        total = sum(pasta["estados"].values())
        pasta["fracao_estados"] = {
            estado: (n / total if total else 0.0) for estado, n in pasta["estados"].items()
        }
    return pastas


def formata_diagnostico(diagnostico: dict[str, dict[str, Any]]) -> str:
    nomes = list(diagnostico)
    largura = max([26, *(len(e) for e in ETAPAS)])
    linhas = ["", f"{'onde parou':<{largura}}" + "".join(f"  {n[:12]:>12}" for n in nomes)]
    linhas.append("-" * (largura + 14 * len(nomes)))
    for etapa in ETAPAS:
        linhas.append(
            f"{etapa:<{largura}}"
            + "".join(f"  {diagnostico[n]['etapas'][etapa]:>12}" for n in nomes)
        )
    linhas += ["", f"{'amostras de pulso':<{largura}}" + "".join(f"  {n[:12]:>12}" for n in nomes)]
    linhas.append("-" * (largura + 14 * len(nomes)))
    for estado in ESTADOS:
        linhas.append(
            f"{estado:<{largura}}"
            + "".join(f"  {diagnostico[n]['fracao_estados'][estado]:>12.0%}" for n in nomes)
        )
    return "\n".join(linhas)


# --- varredura ---------------------------------------------------------------------

GRADE: dict[str, tuple[float, ...]] = {
    "alcance_min": (0.8, 1.0, 1.2, 1.5),
    "zona_y_max": (1.0, 1.4, 1.8),
    "zona_x_max": (0.8, 1.1, 1.5),
    "permanencia_min_s": (0.0, 0.25, 0.5),
}
"""108 combinações. Pequena de propósito: com 92 furtos e 90 normais, uma grade fina
acha a combinação que acerta *estes* vídeos, e o número dela não vale para câmera
nenhuma. A varredura serve para ver se **existe** região que separa as classes."""


def varre(
    quadros_por_video: dict[str, Sequence[QuadroCacheado]],
    base: ParametrosGesto,
    grade: dict[str, tuple[float, ...]] = GRADE,
) -> list[dict[str, Any]]:
    """Cada combinação da grade com a fração de vídeos com disparo por pasta.

    Ordenada pela distância entre `Shoplifting` e `Normal`, maior primeiro; sem as
    duas pastas, pela soma das frações.
    """
    nomes = list(grade)
    linhas: list[dict[str, Any]] = []
    for valores in itertools.product(*(grade[n] for n in nomes)):
        try:
            parametros = replace(base, **dict(zip(nomes, valores, strict=True)))
        except ValueError:
            continue
        com: Counter[str] = Counter()
        total: Counter[str] = Counter()
        for video, quadros in quadros_por_video.items():
            pasta = _pasta(video)
            total[pasta] += 1
            if disparos_do_video(video, quadros, parametros):
                com[pasta] += 1
        fracoes = {pasta: com[pasta] / total[pasta] for pasta in sorted(total)}
        linhas.append({"parametros": dict(zip(nomes, valores, strict=True)), "fracoes": fracoes})

    def separacao(linha: dict[str, Any]) -> float:
        f = linha["fracoes"]
        if "Shoplifting" in f and "Normal" in f:
            return f["Shoplifting"] - f["Normal"]
        return sum(f.values())

    linhas.sort(key=separacao, reverse=True)
    return linhas


def formata_varredura(linhas: list[dict[str, Any]], *, mostrar: int = 15) -> str:
    if not linhas:
        return ""
    nomes = list(linhas[0]["parametros"])
    pastas = list(linhas[0]["fracoes"])
    cabecalho = "".join(f"  {n[:12]:>12}" for n in nomes) + "".join(
        f"  {p[:12]:>12}" for p in pastas
    )
    saida = ["", f"varredura: {len(linhas)} combinações, as {mostrar} que mais separam", cabecalho]
    saida.append("-" * len(cabecalho))
    for linha in linhas[:mostrar]:
        saida.append(
            "".join(f"  {linha['parametros'][n]:>12g}" for n in nomes)
            + "".join(f"  {linha['fracoes'][p]:>12.0%}" for p in pastas)
        )
    return "\n".join(saida)


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
    parser.add_argument(
        "--varredura",
        action="store_true",
        help="roda a grade de limiares sobre o cache e mostra as que mais separam as pastas",
    )
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
        diagnosticos: dict[str, tuple[str, Counter[str]]] = {}
        quadros_por_video: dict[str, list[QuadroCacheado]] = {}
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
            disparos = disparos_do_video(nome, quadros, parametros)
            por_video[nome] = (disparos, duracao)
            diagnosticos[nome] = diagnostica_video(quadros, parametros, disparou=bool(disparos))
            if args.varredura:
                quadros_por_video[nome] = quadros
    except (qc.ErroQualificacao, DetectorModelError, PoseModelError, ValueError) as erro:
        print(f"avalia_gesto: {erro}", file=sys.stderr)  # noqa: T201
        return 1
    finally:
        modelos.fecha()

    relatorio = monta_relatorio(por_video, parametros)
    diagnostico = monta_diagnostico(diagnosticos)
    relatorio["diagnostico"] = diagnostico
    for video, (etapa, _contagem) in diagnosticos.items():
        relatorio["videos"][video]["etapa"] = etapa
    print(formata(relatorio))  # noqa: T201
    print(formata_diagnostico(diagnostico))  # noqa: T201
    if args.varredura:
        print("varrendo a grade...", file=sys.stderr, flush=True)  # noqa: T201
        relatorio["varredura"] = varre(quadros_por_video, parametros)
        print(formata_varredura(relatorio["varredura"]))  # noqa: T201
    if args.saida is not None:
        args.saida.parent.mkdir(parents=True, exist_ok=True)
        args.saida.write_text(json.dumps(relatorio, indent=2, ensure_ascii=False) + "\n")
        print(f"json em {args.saida}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
