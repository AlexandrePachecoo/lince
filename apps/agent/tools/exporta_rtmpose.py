"""Exporta o RTMPose-s COCO-17 oficial para ONNX, fora do repositório.

Não é dependência do agente. Cria um venv à parte, baixa o checkpoint
Apache-2.0 (coco ou body8, 17 juntas) e roda o mmdeploy. Nenhum peso entra
no git: checkpoint, clone e o `.onnx` ficam num caminho que este script
recusa se estiver dentro do repositório.

O export é o `pose-detection_simcc_onnxruntime_dynamic.py` do mmdeploy, com
`export_postprocess=False`. Sai `simcc_x`/`simcc_y` crus e o lote dinâmico —
o que `OnnxPose` e `decode_simcc` leem. O zip `onnx_sdk` e o rtmlib não
servem aqui: o rtmlib não exporta o grafo, só baixa um ONNX que pode trazer
o pós-processamento, e aí a saída deixa de ser SimCC.

WholeBody (133) e Halpe (26 ou 136) não estão entre as variantes. O padrão
é o body8, o COCO-17 do metafile `rtmpose_body8-coco`: o arquivo oficial se
chama `body7` porque foi assim que o OpenMMLab publicou, e a cabeça tem 17
canais. Não é o `body8-halpe26`. O sha256 dele começa com `acd4a1ef`, que é
o prefixo do nome — a convenção do OpenMMLab.

O coco (`…-8edcf0d7_…`) fica bloqueado. Nesse catálogo o prefixo do nome é o
início do sha256, e o arquivo publicado não começa com `8edcf0d7`. Sem essa
coincidência não dá para confiar no pickle. Ele só segue com
`--aceita-coco-nao-verificado` e com o sha256 que quem chama conferiu,
passado em `--sha256`. Esse hash não fica gravado aqui.

Torch de CPU chega para exportar. A GPU do fundador entra no benchmark, pelo
ONNX Runtime, não neste venv.

    uv run --no-project --python 3.11 python tools/exporta_rtmpose.py \
        --destino ~/lince-modelos/rtmpose-s.onnx --variante body8
    python tools/exporta_rtmpose.py --destino D:\\lince-modelos\\rtmpose-s.onnx --variante body8
    python tools/exporta_rtmpose.py --destino ~/lince-modelos/rtmpose-s.onnx --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

MMPOSE_GIT = "https://github.com/open-mmlab/mmpose.git"
MMPOSE_REF = "v1.3.2"
MMDEPLOY_GIT = "https://github.com/open-mmlab/mmdeploy.git"
MMDEPLOY_REF = "v1.3.1"
DEPLOY_CONFIG = "configs/mmpose/pose-detection_simcc_onnxruntime_dynamic.py"

# O venv do export não é o do agente e não precisa do Python dele. O que manda
# é o mmcv: o mmpose 1.3.2 aceita só <2.2, e o mmcv 2.1.0 de CPU só tem roda
# no índice `torch2.1`, até o Python 3.11. O índice `torch2.2` traz apenas o
# 2.2.0; com ele o pip cai no código-fonte e quebra no `pkg_resources`. Por
# isso o torch é o 2.1 e o Python do export é o 3.11. numpy<2 porque essa roda
# do mmcv não é a do numpy 2.
PACOTES_BASE = (
    "numpy>=1.23,<2",
    "onnx==1.16.2",
    "onnxruntime==1.19.2",
    "opencv-python-headless==4.9.0.80",
    "mmengine==0.10.5",
    "mmdet==3.2.0",
    "mmdeploy==1.3.1",
)
MMPOSE = "mmpose==1.3.2"
# O `requirements/runtime.txt` do mmpose 1.3.2 menos o `chumpy`, o `numpy`, o
# `opencv-python` e o `torchvision`, que já entraram fixados acima. O chumpy só
# existe como código-fonte e o `setup.py` dele importa o pip, que não está no
# ambiente isolado de build: o pip para ali. Ele só serve ao SMPL, que o
# RTMPose não usa. Por isso o mmpose entra com `--no-deps`, depois disto.
MMPOSE_DEPENDENCIAS = (
    "numpy>=1.23,<2",
    "json_tricks",
    "matplotlib",
    "munkres",
    "pillow",
    "scipy",
    "xtcocotools>=1.12",
)
TORCH = ("torch==2.1.2", "torchvision==0.16.2")
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
MMCV = "mmcv==2.1.0"
MMCV_INDEX = "https://download.openmmlab.com/mmcv/dist/cpu/torch2.1/index.html"
PYTHON_MINIMO = (3, 8)
PYTHON_MAXIMO = (3, 11)
"""As versões do Python com roda do mmcv 2.1.0 no índice acima."""


@dataclass(frozen=True, slots=True)
class Variante:
    """Um checkpoint oficial de 17 juntas. A URL é pública; não há credencial.

    `sha256` é do arquivo `.pth` inteiro. O mmdeploy abre isso com torch, e
    torch abre pickle: um arquivo trocado no meio do caminho executa código
    antes de qualquer checagem de forma. O hash é conferido depois do
    download e antes desse load.
    """

    config_relativa: str
    checkpoint_url: str
    sha256: str | None
    """Hash conferido do `.pth`. `None` no coco: o prefixo do nome não é o
    início do sha256 do arquivo publicado, e um hash medido daqui não entra
    como se fosse confiável."""


VARIANTE_PADRAO = "body8"

# O body8 tem sha256 conferido: o token do nome é sha256[:8]. O coco não tem
# hash neste dicionário. Quem insistir passa `--sha256` na linha de comando.
VARIANTES: dict[str, Variante] = {
    "coco": Variante(
        config_relativa=(
            "configs/body_2d_keypoint/rtmpose/coco/rtmpose-s_8xb256-420e_coco-256x192.py"
        ),
        checkpoint_url=(
            "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/"
            "rtmpose-s_simcc-coco_pt-aic-coco_420e-256x192-8edcf0d7_20230127.pth"
        ),
        sha256=None,
    ),
    "body8": Variante(
        config_relativa=(
            "configs/body_2d_keypoint/rtmpose/body8/rtmpose-s_8xb256-420e_body8-256x192.py"
        ),
        checkpoint_url=(
            "https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/"
            "rtmpose-s_simcc-body7_pt-body7_420e-256x192-acd4a1ef_20230504.pth"
        ),
        sha256="acd4a1efd8f01669cadd699ce0cceeebfc0f57a008074858e0d3063110c73c94",
    ),
}

# A mesma família de `recusa_modelo_fora_do_piloto`, copiada de propósito:
# este script roda num interpretador que não tem o agente instalado.
_FORA = re.compile(r"whole[-_]?body|halpe|yolov[58]|yolo\d{2}|ultralytics", re.IGNORECASE)


class ExportaError(Exception):
    """O export não pode seguir. A mensagem diz o que trocar."""


@dataclass(frozen=True, slots=True)
class Plano:
    destino: Path
    cache: Path
    venv: Path
    variante: str
    config_relativa: str
    checkpoint_url: str
    sha256: str
    forcar: bool

    def texto(self) -> str:
        python = python_do_venv(self.venv)
        return "\n".join(
            [
                f"variante: {self.variante} (Apache-2.0, 17 juntas COCO, 256×192)",
                f"config: {self.config_relativa}",
                f"checkpoint: {self.checkpoint_url}",
                f"sha256: {self.sha256}",
                f"destino: {self.destino}",
                f"cache: {self.cache}",
                f"venv: {self.venv}",
                "mmpose: " + MMPOSE_GIT + " @ " + MMPOSE_REF,
                "mmdeploy: " + MMDEPLOY_GIT + " @ " + MMDEPLOY_REF,
                "deploy: " + DEPLOY_CONFIG + " --device cpu",
                "comando:",
                "  "
                + " ".join(
                    comando_deploy(
                        python,
                        self.cache / "mmpose",
                        self.cache / "mmdeploy",
                        self.config_relativa,
                        self.cache / "checkpoints" / nome_do_url(self.checkpoint_url),
                        self.cache / "amostra.bmp",
                        self.cache / "work" / self.variante,
                    )
                ),
                "O ONNX sai com lote dinâmico e SimCC cru (export_postprocess=False).",
                "A normalização fica no agente, em pose/preprocess.py, não no grafo.",
            ]
        )


def cache_padrao() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "lince" / "rtmpose-export"
    return Path.home() / ".cache" / "lince" / "rtmpose-export"


def raiz_do_repositorio() -> Path:
    aqui = Path(__file__).resolve()
    for pai in aqui.parents:
        if (pai / ".git").exists():
            return pai
    # tools/ → agent/ → apps/ → raiz. O .git ausente (cópia solta do script)
    # ainda assim não pode gravar peso ao lado do código.
    return aqui.parents[3]


def dentro_do_repositorio(caminho: Path) -> bool:
    try:
        caminho.resolve().relative_to(raiz_do_repositorio().resolve())
    except ValueError:
        return False
    return True


def nome_do_url(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def python_do_venv(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def recusa_nome(nome: str, o_que: str) -> None:
    marca = _FORA.search(Path(nome).name)
    if marca is not None:
        raise ExportaError(
            f"{o_que} '{Path(nome).name}' não é o RTMPose-s COCO-17 do piloto "
            f"('{marca.group(0)}'). WholeBody, Halpe e Ultralytics ficam de fora. "
            "A variante do piloto é body8, 17 juntas. O coco fica bloqueado"
        )


def comando_deploy(
    python: Path,
    mmpose: Path,
    mmdeploy: Path,
    config_relativa: str,
    checkpoint: Path,
    imagem: Path,
    work: Path,
) -> list[str]:
    """Argv do mmdeploy. Sem `--show`: num box sem tela o export escreveria o
    ONNX e sairia com erro, parecendo que o arquivo não nasceu."""
    return [
        str(python),
        str(mmdeploy / "tools" / "deploy.py"),
        str(mmdeploy / DEPLOY_CONFIG),
        str(mmpose / config_relativa),
        str(checkpoint),
        str(imagem),
        "--work-dir",
        str(work),
        "--device",
        "cpu",
    ]


def _versao_do_interpretador() -> tuple[int, int]:
    return (sys.version_info.major, sys.version_info.minor)


def versao_do_venv(venv: Path) -> tuple[int, int] | None:
    """A versão gravada no `pyvenv.cfg`, sem executar o interpretador do venv."""
    try:
        linhas = (venv / "pyvenv.cfg").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for linha in linhas:
        chave, _, valor = linha.partition("=")
        if chave.strip() in {"version", "version_info"}:
            partes = valor.strip().split(".")
            if len(partes) >= 2 and partes[0].isdigit() and partes[1].isdigit():
                return (int(partes[0]), int(partes[1]))
    return None


def confere_python_do_export(versao: tuple[int, int]) -> None:
    if PYTHON_MINIMO <= versao <= PYTHON_MAXIMO:
        return
    raise ExportaError(
        f"o export precisa de Python {PYTHON_MINIMO[0]}.{PYTHON_MINIMO[1]} a "
        f"{PYTHON_MAXIMO[0]}.{PYTHON_MAXIMO[1]}, e este é {versao[0]}.{versao[1]}. "
        f"O {MMCV} só tem roda de CPU até o 3.11. O venv do export é separado do "
        "agente: rode com `uv run --no-project --python 3.11 python "
        "apps/agent/tools/exporta_rtmpose.py ...`"
    )


def escreve_bmp(caminho: Path, largura: int = 64, altura: int = 64) -> None:
    """BMP 24 bits, cinza. O deploy.py só precisa de uma imagem para o traço;
    a forma do ONNX vem do config (192×256), não deste arquivo."""
    linha = (b"\x80\x80\x80" * largura) + (b"\x00" * ((4 - (largura * 3) % 4) % 4))
    pixels = linha * altura
    cabecalho = b"BM" + (14 + 40 + len(pixels)).to_bytes(4, "little")
    cabecalho += b"\x00\x00\x00\x00" + (14 + 40).to_bytes(4, "little")
    info = (40).to_bytes(4, "little")
    info += largura.to_bytes(4, "little", signed=True)
    info += altura.to_bytes(4, "little", signed=True)
    info += (1).to_bytes(2, "little") + (24).to_bytes(2, "little")
    info += b"\x00" * 24
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_bytes(cabecalho + info + pixels)


def _normaliza_sha256(valor: str | None) -> str | None:
    if valor is None:
        return None
    texto = valor.strip().lower()
    if not _sha256_completo(texto):
        raise ExportaError(
            f"--sha256 tem que ser 64 hex, recebi {valor!r}. "
            "É o hash do .pth inteiro, conferido por quem chama"
        )
    return texto


def recusa_coco_nao_verificado(*, aceita: bool, sha256: str | None) -> str:
    """O coco só segue com a flag e com um sha256 que não está neste arquivo.

    No OpenMMLab o prefixo do nome é o início do sha256. O arquivo
    `…-8edcf0d7_…` publicado não começa com esse prefixo, então o pickle não
    é confiável. O hash que libera o download vem de `--sha256`, não daqui.
    """
    if not aceita or sha256 is None:
        raise ExportaError(
            "a variante coco está bloqueada. O nome do checkpoint traz 8edcf0d7, "
            "e no OpenMMLab esse prefixo é o início do sha256 — o arquivo publicado "
            "nesse URL não começa com 8edcf0d7. Sem essa coincidência o .pth, que é "
            "pickle, não é confiável. O padrão é --variante body8. Para seguir mesmo "
            "assim, passe --aceita-coco-nao-verificado e --sha256 com o hash que você "
            "conferiu; ele não fica gravado neste script"
        )
    return sha256


def montar_plano(
    *,
    destino: Path,
    variante: str = VARIANTE_PADRAO,
    cache: Path | None = None,
    venv: Path | None = None,
    forcar: bool = False,
    aceita_coco_nao_verificado: bool = False,
    sha256: str | None = None,
) -> Plano:
    if variante not in VARIANTES:
        raise ExportaError(
            f"variante {variante!r} não é body8 nem coco. "
            "WholeBody e Halpe não são opção: outra contagem de juntas"
        )
    escolhida = VARIANTES[variante]
    sha256 = _normaliza_sha256(sha256)
    if variante == "coco":
        sha256_efetivo = recusa_coco_nao_verificado(
            aceita=aceita_coco_nao_verificado, sha256=sha256
        )
    else:
        if escolhida.sha256 is None or not escolhida.sha256.startswith(
            token_do_checkpoint(escolhida.checkpoint_url)
        ):
            raise ExportaError(
                f"a variante {variante} não tem sha256 conferido contra o prefixo do nome"
            )
        if sha256 is not None and sha256 != escolhida.sha256:
            raise ExportaError(
                f"a variante {variante} já tem sha256 conferido ({escolhida.sha256}). "
                "Não substitua por --sha256"
            )
        sha256_efetivo = escolhida.sha256
    destino = destino.expanduser()
    if destino.suffix.lower() != ".onnx":
        raise ExportaError(f"o destino tem que ser um .onnx, recebi {destino.name}")
    recusa_nome(destino.name, "destino")
    recusa_nome(escolhida.config_relativa, "config")
    recusa_nome(nome_do_url(escolhida.checkpoint_url), "checkpoint")

    cache = (cache or cache_padrao()).expanduser()
    venv = (venv or (cache / "venv")).expanduser()
    for caminho, o_que in ((destino, "destino"), (cache, "cache"), (venv, "venv")):
        if dentro_do_repositorio(caminho):
            raise ExportaError(
                f"o {o_que} {caminho} está dentro do repositório. "
                "Peso não entra no git, nem em pasta ignorada: use um caminho fora, "
                "por exemplo ~/lince-modelos ou D:\\lince-modelos"
            )
    return Plano(
        destino=destino,
        cache=cache,
        venv=venv,
        variante=variante,
        config_relativa=escolhida.config_relativa,
        checkpoint_url=escolhida.checkpoint_url,
        sha256=sha256_efetivo,
        forcar=forcar,
    )


def _roda(argv: list[str], *, cwd: Path | None, o_que: str) -> None:
    print(f"==> {o_que}", flush=True)
    resultado = subprocess.run(argv, cwd=cwd, check=False)  # noqa: S603 - argv nosso
    if resultado.returncode != 0:
        raise ExportaError(f"{o_que} falhou (código {resultado.returncode})")


def sha256_arquivo(caminho: Path) -> str:
    digest = hashlib.sha256()
    with caminho.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1 << 20), b""):
            digest.update(bloco)
    return digest.hexdigest()


def token_do_checkpoint(url: str) -> str:
    """Os 8 hex do nome oficial, antes da data (`…-8edcf0d7_20230127.pth`)."""
    nome = nome_do_url(url).removesuffix(".pth")
    return nome.rsplit("_", 1)[0].rsplit("-", 1)[-1]


def _sha256_completo(valor: str) -> bool:
    return len(valor) == 64 and all(caractere in "0123456789abcdef" for caractere in valor)


def baixa_checkpoint(
    url: str,
    destino: Path,
    sha256: str,
    *,
    baixar: Callable[[str, Path], None] | None = None,
) -> None:
    """Baixa o `.pth` e confere o sha256 antes de qualquer load.

    O arquivo é pickle. Sem esta conferência, o mmdeploy executa o que estiver
    dentro. Hash errado apaga o arquivo e aborta — não segue para o torch.
    """
    if not _sha256_completo(sha256):
        raise ExportaError(f"sha256 do checkpoint não é 64 hex, recebi {sha256!r}")
    if destino.is_file():
        if sha256_arquivo(destino) == sha256:
            print(f"==> checkpoint já está em {destino}", flush=True)
            return
        destino.unlink()
        raise ExportaError(
            f"checkpoint em {destino.name} não bate com o sha256 oficial "
            f"(esperava {sha256}). Apaguei o arquivo; rode de novo para baixar. "
            "Um .pth é pickle: carregar sem conferir executa o que estiver dentro"
        )

    destino.parent.mkdir(parents=True, exist_ok=True)
    parcial = destino.with_suffix(destino.suffix + ".parcial")
    print(f"==> baixando {url}", flush=True)
    try:
        if baixar is None:
            urllib.request.urlretrieve(url, parcial)  # noqa: S310 - URL pública, sem credencial
        else:
            baixar(url, parcial)
    except OSError as erro:
        parcial.unlink(missing_ok=True)
        raise ExportaError(f"download falhou: {erro}") from erro
    if not parcial.is_file():
        raise ExportaError(f"o download não escreveu {parcial.name}")
    obtido = sha256_arquivo(parcial)
    if obtido != sha256:
        parcial.unlink(missing_ok=True)
        destino.unlink(missing_ok=True)
        raise ExportaError(
            f"sha256 de {destino.name} não bate (esperava {sha256}, veio {obtido}). "
            "Apaguei o arquivo. Um .pth é pickle e não pode ser carregado assim"
        )
    parcial.replace(destino)


def _clone(url: str, ref: str, destino: Path) -> None:
    if (destino / ".git").exists():
        print(f"==> {destino.name} já clonado em {destino}", flush=True)
        return
    destino.parent.mkdir(parents=True, exist_ok=True)
    _roda(
        ["git", "clone", "--depth", "1", "--branch", ref, url, str(destino)],
        cwd=None,
        o_que=f"clonando {url} @{ref}",
    )


def executar(plano: Plano) -> None:
    if plano.destino.is_file() and not plano.forcar:
        print(f"já existe: {plano.destino}", flush=True)
        print("passe --forcar para exportar de novo", flush=True)
        return

    # Antes de clonar e baixar: o Python errado só aparecia no mmcv, depois do
    # torch inteiro.
    if plano.venv.exists():
        versao = versao_do_venv(plano.venv)
        if versao is None or not PYTHON_MINIMO <= versao <= PYTHON_MAXIMO:
            achada = "desconhecida" if versao is None else f"{versao[0]}.{versao[1]}"
            raise ExportaError(
                f"o venv em {plano.venv} é de Python {achada}, e o {MMCV} só tem roda "
                "até o 3.11. Apague essa pasta (ou passe outro --venv) e rode de novo "
                "com `uv run --no-project --python 3.11 python ...`"
            )
    else:
        confere_python_do_export(_versao_do_interpretador())

    _clone(MMPOSE_GIT, MMPOSE_REF, plano.cache / "mmpose")
    _clone(MMDEPLOY_GIT, MMDEPLOY_REF, plano.cache / "mmdeploy")
    checkpoint = plano.cache / "checkpoints" / nome_do_url(plano.checkpoint_url)
    baixa_checkpoint(plano.checkpoint_url, checkpoint, plano.sha256)
    imagem = plano.cache / "amostra.bmp"
    escreve_bmp(imagem)

    if not plano.venv.exists():
        _roda(
            [sys.executable, "-m", "venv", str(plano.venv)],
            cwd=None,
            o_que=f"criando venv em {plano.venv}",
        )
    python = python_do_venv(plano.venv)
    if not python.is_file():
        raise ExportaError(f"venv sem interpretador em {python}")

    _roda(
        [str(python), "-m", "pip", "install", "--upgrade", "pip"],
        cwd=None,
        o_que="atualizando pip do venv de exportação",
    )
    _roda(
        [str(python), "-m", "pip", "install", *TORCH, "--index-url", TORCH_INDEX],
        cwd=None,
        o_que="instalando torch de CPU (a GPU não entra no export)",
    )
    _roda(
        # Só roda pronta: sem ela o pip compila o C++ do mmcv por minutos e
        # falha com um erro de build que não diz que faltou a roda.
        [str(python), "-m", "pip", "install", MMCV, "--only-binary", "mmcv", "-f", MMCV_INDEX],
        cwd=None,
        o_que="instalando mmcv de CPU para torch 2.1",
    )
    _roda(
        [str(python), "-m", "pip", "install", *PACOTES_BASE],
        cwd=None,
        o_que="instalando mmdet e mmdeploy no venv à parte",
    )
    _roda(
        [str(python), "-m", "pip", "install", *MMPOSE_DEPENDENCIAS],
        cwd=None,
        o_que="instalando o que o mmpose usa, sem o chumpy",
    )
    _roda(
        [str(python), "-m", "pip", "install", "--no-deps", MMPOSE],
        cwd=None,
        o_que="instalando o mmpose sem dependências",
    )

    work = plano.cache / "work" / plano.variante
    if work.exists():
        shutil.rmtree(work)
    argv = comando_deploy(
        python,
        plano.cache / "mmpose",
        plano.cache / "mmdeploy",
        plano.config_relativa,
        checkpoint,
        imagem,
        work,
    )
    _roda(argv, cwd=plano.cache / "mmdeploy", o_que="exportando para ONNX")
    produzido = work / "end2end.onnx"
    if not produzido.is_file():
        raise ExportaError(
            f"o mmdeploy terminou sem escrever {produzido}. "
            "O piloto espera o end2end.onnx com SimCC cru, não um zip do onnx_sdk"
        )
    plano.destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(produzido, plano.destino)
    print(f"onnx em {plano.destino} ({plano.destino.stat().st_size} bytes)", flush=True)


def _analisa(argv: list[str]) -> argparse.Namespace:
    analise = argparse.ArgumentParser(description=__doc__)
    analise.add_argument(
        "--destino", type=Path, required=True, help="caminho do .onnx, fora do git"
    )
    analise.add_argument("--variante", choices=tuple(VARIANTES), default=VARIANTE_PADRAO)
    analise.add_argument(
        "--cache", type=Path, default=None, help="clones e checkpoint; fora do git"
    )
    analise.add_argument(
        "--venv", type=Path, default=None, help="venv à parte; o padrão é <cache>/venv"
    )
    analise.add_argument(
        "--forcar", action="store_true", help="exporta de novo mesmo se o destino existir"
    )
    analise.add_argument("--dry-run", action="store_true", help="mostra o plano e não baixa nada")
    analise.add_argument(
        "--aceita-coco-nao-verificado",
        action="store_true",
        help="libera a variante coco, cujo prefixo do nome não é o sha256 do arquivo",
    )
    analise.add_argument(
        "--sha256",
        default=None,
        help="sha256 completo do .pth; obrigatório junto com --aceita-coco-nao-verificado",
    )
    return analise.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _analisa(sys.argv[1:] if argv is None else argv)
    try:
        plano = montar_plano(
            destino=args.destino,
            variante=args.variante,
            cache=args.cache,
            venv=args.venv,
            forcar=args.forcar,
            aceita_coco_nao_verificado=args.aceita_coco_nao_verificado,
            sha256=args.sha256,
        )
        if args.dry_run:
            print(plano.texto())
            return 0
        executar(plano)
    except ExportaError as erro:
        print(f"exporta_rtmpose: {erro}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
