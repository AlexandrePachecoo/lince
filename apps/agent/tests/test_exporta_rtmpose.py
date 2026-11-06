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
