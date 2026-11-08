"""O export do RTMPose-s, sem baixar peso e sem mmpose.

O script existe para rodar na máquina do fundador. Aqui o que se trava é o
que não pode passar: destino dentro do git, WholeBody, Halpe, Ultralytics, e
um plano que aponte para outro checkpoint que não os dois oficiais de 17 juntas.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_CAMINHO = Path(__file__).resolve().parents[1] / "tools" / "exporta_rtmpose.py"
# O estágio `teste` da imagem copia tests/ e não tools/. São scripts offline
# e não vão para a loja; sem este pulo a coleta quebra com FileNotFoundError
# e o job da imagem fica vermelho. No CI do repositório, onde tools/ existe,
# a suíte roda.
if not _CAMINHO.is_file():
    pytest.skip(
        f"{_CAMINHO} não está nesta árvore. A imagem do agente não copia tools/.",
        allow_module_level=True,
    )
_SPEC = importlib.util.spec_from_file_location("exporta_rtmpose", _CAMINHO)
assert _SPEC is not None and _SPEC.loader is not None
exporta = importlib.util.module_from_spec(_SPEC)
sys.modules["exporta_rtmpose"] = exporta
_SPEC.loader.exec_module(exporta)

RAIZ = Path(__file__).resolve().parents[2]


def test_variantes_oficiais_sao_coco17_e_nao_halpe():
    assert set(exporta.VARIANTES) == {"coco", "body8"}
    assert exporta.VARIANTE_PADRAO == "body8"
    for variante in exporta.VARIANTES.values():
        exporta.recusa_nome(variante.config_relativa, "config")
        exporta.recusa_nome(exporta.nome_do_url(variante.checkpoint_url), "checkpoint")
        texto = f"{variante.config_relativa} {variante.checkpoint_url}".lower()
        assert "halpe" not in texto
        assert "whole" not in texto
        assert "ultralytics" not in texto
        assert "256x192" in variante.config_relativa
    # No body8 o token do nome é sha256[:8]. O coco não tem hash gravado:
    # o prefixo 8edcf0d7 não é o início do sha256 do arquivo publicado.
    assert exporta.VARIANTES["coco"].sha256 is None
    assert exporta.token_do_checkpoint(exporta.VARIANTES["coco"].checkpoint_url) == "8edcf0d7"
    corpo = _CAMINHO.read_text(encoding="utf-8")
    assert "29dcacbb" not in corpo
    assert exporta.token_do_checkpoint(exporta.VARIANTES["body8"].checkpoint_url) == "acd4a1ef"
    assert exporta.VARIANTES["body8"].sha256 is not None
    assert exporta.VARIANTES["body8"].sha256.startswith("acd4a1ef")


def test_destino_dentro_do_repositorio_e_recusado(tmp_path: Path):
    with pytest.raises(exporta.ExportaError, match="repositório"):
        exporta.montar_plano(destino=RAIZ / "apps" / "agent" / "rtmpose-s.onnx")
    with pytest.raises(exporta.ExportaError, match="repositório"):
        exporta.montar_plano(
            destino=tmp_path / "rtmpose-s.onnx",
            cache=RAIZ / "apps" / "agent" / "models",
        )


def test_nome_wholebody_halpe_e_ultralytics_e_recusado(tmp_path: Path):
    for nome in (
        "rtmpose-s_coco-wholebody.onnx",
        "rtmpose-s_halpe26.onnx",
        "yolov8n-pose.onnx",
    ):
        with pytest.raises(exporta.ExportaError):
            exporta.montar_plano(destino=tmp_path / nome)


def test_plano_body8_aponta_para_o_checkpoint_de_17_juntas(tmp_path: Path):
    plano = exporta.montar_plano(destino=tmp_path / "rtmpose-s.onnx")
    assert plano.variante == "body8"
    assert "acd4a1ef" in plano.checkpoint_url
    assert plano.config_relativa.endswith("rtmpose-s_8xb256-420e_body8-256x192.py")
    assert not exporta.dentro_do_repositorio(plano.destino)
    assert not exporta.dentro_do_repositorio(plano.cache)
    argv = exporta.comando_deploy(
        Path("python"),
        Path("/cache/mmpose"),
        Path("/cache/mmdeploy"),
        plano.config_relativa,
        Path("/cache/peso.pth"),
        Path("/cache/amostra.bmp"),
        Path("/cache/work"),
    )
    assert any(parte.endswith("pose-detection_simcc_onnxruntime_dynamic.py") for parte in argv)
    assert "--device" in argv and "cpu" in argv
    assert "--show" not in argv
    assert any("body8-256x192.py" in parte for parte in argv)
    assert all("halpe" not in parte.lower() for parte in argv)
    assert all("wholebody" not in parte.lower() for parte in argv)


def test_dry_run_nao_cria_o_onnx(tmp_path: Path, capsys):
    destino = tmp_path / "rtmpose-s.onnx"
    codigo = exporta.main(["--destino", str(destino), "--dry-run"])
    saida = capsys.readouterr().out
    assert codigo == 0
    assert not destino.exists()
    assert "acd4a1ef" in saida
    assert "body8" in saida
    assert "pose-detection_simcc_onnxruntime_dynamic.py" in saida
    assert "8edcf0d7" not in saida
    assert "wholebody" not in saida.lower()
    assert "halpe" not in saida.lower()


def test_coco_sem_flag_e_sem_sha256_e_recusado(tmp_path: Path, capsys):
    """O prefixo 8edcf0d7 não é o sha256 do arquivo. Sem os dois destravamentos
    o script não pode baixar o pickle."""
    destino = tmp_path / "rtmpose-s.onnx"
    with pytest.raises(exporta.ExportaError, match="8edcf0d7"):
        exporta.montar_plano(destino=destino, variante="coco")
    with pytest.raises(exporta.ExportaError, match="8edcf0d7"):
        exporta.montar_plano(
            destino=destino,
            variante="coco",
            aceita_coco_nao_verificado=True,
        )
    with pytest.raises(exporta.ExportaError, match="8edcf0d7"):
        exporta.montar_plano(destino=destino, variante="coco", sha256="ab" * 32)

    codigo = exporta.main(["--destino", str(destino), "--variante", "coco"])
    erro = capsys.readouterr().err
    assert codigo == 1
    assert "8edcf0d7" in erro
    assert "--aceita-coco-nao-verificado" in erro
    assert "--sha256" in erro
    assert not destino.exists()


def test_coco_so_segue_com_flag_e_sha256_informado(tmp_path: Path):
    destino = tmp_path / "rtmpose-s.onnx"
    informado = "ab" * 32
    plano = exporta.montar_plano(
        destino=destino,
        variante="coco",
        aceita_coco_nao_verificado=True,
        sha256=informado,
    )
    assert plano.sha256 == informado
    assert plano.variante == "coco"
    assert "8edcf0d7" in plano.checkpoint_url
    assert exporta.VARIANTES["coco"].sha256 is None


def test_destino_existente_nao_e_reescrito(tmp_path: Path, capsys):
    destino = tmp_path / "rtmpose-s.onnx"
    destino.write_bytes(b"ja exportei")
    codigo = exporta.main(["--destino", str(destino), "--cache", str(tmp_path / "cache")])
    assert codigo == 0
    assert destino.read_bytes() == b"ja exportei"
    assert "já existe" in capsys.readouterr().out


def test_download_falso_com_hash_errado_apaga_o_pth(tmp_path: Path):
    """O .pth é pickle. Um download que não bate com o sha256 oficial não
    pode ficar no disco para o mmdeploy abrir em seguida."""
    destino = tmp_path / "rtmpose-s.pth"

    def falso(_url: str, caminho: Path) -> None:
        Path(caminho).write_bytes(b"pickle-forjado")

    with pytest.raises(exporta.ExportaError, match="sha256"):
        exporta.baixa_checkpoint(
            exporta.VARIANTES["body8"].checkpoint_url,
            destino,
            exporta.VARIANTES["body8"].sha256 or "",
            baixar=falso,
        )
    assert not destino.exists()
    assert not destino.with_suffix(".pth.parcial").exists()
    assert list(tmp_path.glob("*.pth*")) == []


def test_bmp_de_amostra_comeca_com_bm(tmp_path: Path):
    caminho = tmp_path / "amostra.bmp"
    exporta.escreve_bmp(caminho, 8, 4)
    assert caminho.read_bytes().startswith(b"BM")


# --- o ambiente do export precisa existir de verdade ----------------------------


def test_mmcv_vem_do_indice_do_mesmo_torch_que_o_venv_instala():
    """O índice do OpenMMLab é por versão do torch, e o `mmcv` de cada índice é
    compilado contra ela. Com o torch 2.2 e o índice `torch2.2`, o mmcv 2.1.0
    simplesmente não existe ali — só o 2.2.0, que o mmpose 1.3.2 recusa. O pip
    então caiu no código-fonte e quebrou no `pkg_resources`, na máquina do
    fundador, depois de baixar o torch inteiro. Versão do torch e índice do mmcv
    têm que andar juntos."""
    torch = next(pacote for pacote in exporta.TORCH if pacote.startswith("torch=="))
    major, minor = torch.removeprefix("torch==").split(".")[:2]
    assert f"/torch{major}.{minor}/" in exporta.MMCV_INDEX
    assert exporta.MMCV == "mmcv==2.1.0"
    assert exporta.MMCV_INDEX == "https://download.openmmlab.com/mmcv/dist/cpu/torch2.1/index.html"


@pytest.mark.parametrize("versao", [(3, 12), (3, 13), (3, 7)])
def test_python_sem_roda_do_mmcv_e_recusado_antes_de_baixar(versao):
    """O mmcv 2.1.0 tem roda até o 3.11. Num Python fora disso o export só
    descobre a falta depois de baixar torch e clonar dois repositórios."""
    with pytest.raises(exporta.ExportaError, match="3.11"):
        exporta.confere_python_do_export(versao)


@pytest.mark.parametrize("versao", [(3, 8), (3, 11)])
def test_python_com_roda_do_mmcv_passa(versao):
    exporta.confere_python_do_export(versao)


def _falsifica_rede(monkeypatch, chamadas: list[list[str]]):
    """Nada de git, download ou pip: só registra o que seria rodado."""

    def roda(argv: list[str], *, cwd, o_que: str) -> None:
        chamadas.append(list(argv))
        if argv[1:3] == ["-m", "venv"]:
            python = exporta.python_do_venv(Path(argv[3]))
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("")

    monkeypatch.setattr(exporta, "_roda", roda)
    monkeypatch.setattr(exporta, "_clone", lambda url, *_a, **_k: chamadas.append(["clone", url]))
    monkeypatch.setattr(
        exporta, "baixa_checkpoint", lambda url, *_a, **_k: chamadas.append(["baixa", url])
    )


def test_venv_antigo_de_python_312_e_recusado_sem_rodar_pip(tmp_path: Path, monkeypatch):
    """A tentativa que falhou deixa um venv 3.12 no cache. Reaproveitá-lo
    repetiria a mesma falha do mmcv, então o script para e diz o que apagar."""
    chamadas: list[list[str]] = []
    _falsifica_rede(monkeypatch, chamadas)
    monkeypatch.setattr(exporta, "_versao_do_interpretador", lambda: (3, 11))
    venv = tmp_path / "cache" / "venv"
    venv.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.13\n")
    plano = exporta.montar_plano(destino=tmp_path / "rtmpose-s.onnx", cache=tmp_path / "cache")

    with pytest.raises(exporta.ExportaError, match=str(venv).replace("\\", "\\\\")):
        exporta.executar(plano)
    assert chamadas == []


def test_interpretador_312_e_recusado_antes_de_criar_o_venv(tmp_path: Path, monkeypatch):
    chamadas: list[list[str]] = []
    _falsifica_rede(monkeypatch, chamadas)
    monkeypatch.setattr(exporta, "_versao_do_interpretador", lambda: (3, 12))
    plano = exporta.montar_plano(destino=tmp_path / "rtmpose-s.onnx", cache=tmp_path / "cache")

    with pytest.raises(exporta.ExportaError, match="--python 3.11"):
        exporta.executar(plano)
    assert chamadas == []


def test_mmcv_so_entra_como_roda_pronta(tmp_path: Path, monkeypatch):
    """Sem `--only-binary`, roda ausente vira compilação do código-fonte: minutos
    de C++ e um erro de `pkg_resources` que não diz qual é o problema. Com ele, o
    pip responde na hora que não há mmcv para este Python e este torch."""
    chamadas: list[list[str]] = []
    _falsifica_rede(monkeypatch, chamadas)
    monkeypatch.setattr(exporta, "_versao_do_interpretador", lambda: (3, 11))
    plano = exporta.montar_plano(destino=tmp_path / "rtmpose-s.onnx", cache=tmp_path / "cache")

    with pytest.raises(exporta.ExportaError, match="end2end.onnx"):
        exporta.executar(plano)
    mmcv = [argv for argv in chamadas if exporta.MMCV in argv]
    assert len(mmcv) == 1
    assert "--only-binary" in mmcv[0]
    assert mmcv[0][mmcv[0].index("--only-binary") + 1] == "mmcv"
