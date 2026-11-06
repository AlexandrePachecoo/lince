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
    for variante in exporta.VARIANTES.values():
        exporta.recusa_nome(variante.config_relativa, "config")
        exporta.recusa_nome(exporta.nome_do_url(variante.checkpoint_url), "checkpoint")
        texto = f"{variante.config_relativa} {variante.checkpoint_url}".lower()
        assert "halpe" not in texto
        assert "whole" not in texto
        assert "ultralytics" not in texto
        assert "256x192" in variante.config_relativa


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
    plano = exporta.montar_plano(destino=tmp_path / "rtmpose-s.onnx", variante="body8")
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
    assert "8edcf0d7" in saida
    assert "pose-detection_simcc_onnxruntime_dynamic.py" in saida
    assert "wholebody" not in saida.lower()
    assert "halpe" not in saida.lower()


def test_destino_existente_nao_e_reescrito(tmp_path: Path, capsys):
    destino = tmp_path / "rtmpose-s.onnx"
    destino.write_bytes(b"ja exportei")
    codigo = exporta.main(["--destino", str(destino), "--cache", str(tmp_path / "cache")])
    assert codigo == 0
    assert destino.read_bytes() == b"ja exportei"
    assert "já existe" in capsys.readouterr().out


def test_bmp_de_amostra_comeca_com_bm(tmp_path: Path):
    caminho = tmp_path / "amostra.bmp"
    exporta.escreve_bmp(caminho, 8, 4)
    assert caminho.read_bytes().startswith(b"BM")
