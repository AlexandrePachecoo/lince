"""O benchmark da pose, sem vídeo e sem modelo.

O que se mede aqui é a separação dos tempos e o lote, não o percentil de uma
GPU. Um `inferir` que empilha quando o modelo é de lote 1 quebraria o número
que decide quantas pessoas por frame cabem no box.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

_CAMINHO = Path(__file__).resolve().parents[1] / "tools" / "bench_pose.py"
# O estágio `teste` da imagem copia tests/ e não tools/. São scripts offline
# e não vão para a loja; sem este pulo a coleta quebra com FileNotFoundError
# e o job da imagem fica vermelho. No CI do repositório, onde tools/ existe,
# a suíte roda.
if not _CAMINHO.is_file():
    pytest.skip(
        f"{_CAMINHO} não está nesta árvore. A imagem do agente não copia tools/.",
        allow_module_level=True,
    )
_SPEC = importlib.util.spec_from_file_location("bench_pose", _CAMINHO)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
sys.modules["bench_pose"] = bench
_SPEC.loader.exec_module(bench)


class _Pose:
    def __init__(self, dinamico: bool) -> None:
        self.lote_dinamico = dinamico
        self.chamadas: list[int] = []

    def inferir(self, tensor: np.ndarray):
        self.chamadas.append(int(tensor.shape[0]))
        n = int(tensor.shape[0])
        return (
            np.zeros((n, 17, 384), dtype=np.float32),
            np.zeros((n, 17, 512), dtype=np.float32),
        )


def test_lote_dinamico_empilha_e_o_fixo_repete():
    imagem = np.zeros((120, 160, 3), dtype=np.uint8)
    caixas = [(20.0, 10.0, 80.0, 100.0), (40.0, 30.0, 90.0, 110.0), (10.0, 10.0, 40.0, 80.0)]
    dinamica = _Pose(True)
    medida = bench.mede_caixas(imagem, caixas, dinamica)
    assert dinamica.chamadas == [3]
    assert len(medida["recorte_pessoa"]) == 3
    assert len(medida["inferencia_pessoa"]) == 3
    # O tempo por pessoa no lote é o do frame dividido por N. O relógio de
    # fora inclui umas instruções a mais; a folga é isso, não outro estágio.
    assert sum(medida["inferencia_pessoa"]) == pytest.approx(medida["inferencia_frame"], abs=5.0)

    estatica = _Pose(False)
    medida_fixa = bench.mede_caixas(imagem, caixas, estatica)
    assert estatica.chamadas == [1, 1, 1]
    assert len(medida_fixa["decode_pessoa"]) == 3


def test_repete_caixas_ate_o_lote_e_nao_inventa_pessoa():
    caixas = [(0.0, 0.0, 10.0, 20.0), (1.0, 1.0, 2.0, 3.0)]
    saida = bench.escolhe_caixas(caixas, 6)
    assert len(saida) == 6
    assert saida[0] == caixas[0]
    assert saida[5] == caixas[1]
    assert bench.escolhe_caixas([], 6) == []


def test_percentis_e_providers():
    bloco = bench.percentis([1.0, 2.0, 3.0, 4.0, 10.0])
    assert bloco["n"] == 5
    assert bloco["p50"] == pytest.approx(3.0)
    assert bloco["p50"] <= bloco["p95"]
    assert bench.providers_de("cpu") == ("CPUExecutionProvider",)
    assert bench.providers_de("cuda") == ("CUDAExecutionProvider", "CPUExecutionProvider")
    assert bench.interpreta_pessoas("1,3,6") == (1, 3, 6)
    with pytest.raises(bench.BenchError):
        bench.providers_de("tensorrt")
    with pytest.raises(bench.BenchError):
        bench.interpreta_pessoas("0,3")


def test_tabela_traz_os_tres_estagios():
    recorte_ms = {
        "por_pessoa": bench.percentis([1.0, 2.0, 3.0]),
        "por_frame": bench.percentis([3.0, 4.0]),
    }
    inferencia = {
        "por_pessoa": bench.percentis([5.0]),
        "por_frame": bench.percentis([5.0]),
    }
    decode = {
        "por_pessoa": bench.percentis([0.2]),
        "por_frame": bench.percentis([0.2]),
    }
    relatorio = bench.monta_relatorio(
        fonte=Path("video.mp4"),
        frames_lidos=4,
        frames_com_pessoa=2,
        limiar=0.1,
        aquecimento=3,
        pessoas=(1,),
        detector_versao="yolox_s@abc",
        detector_provider="CPUExecutionProvider",
        pose_versao="rtmpose-s@def",
        pose_provider="CPUExecutionProvider",
        lote_dinamico=True,
        estagios={
            1: {
                "amostras_frame": 2,
                "recorte_ms": recorte_ms,
                "inferencia_ms": inferencia,
                "decode_ms": decode,
            }
        },
    )
    texto = bench.formata_tabela(relatorio)
    for estagio in ("recorte", "inferência", "decode"):
        assert estagio in texto
    assert "2.000" in texto
    assert relatorio["unidade"] == "ms"
    assert relatorio["pose"]["lote_dinamico"] is True


def _video(caminho: Path, largura: int, altura: int, frames: int = 3) -> Path:
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg não está no PATH")
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={largura}x{altura}:rate=3",
            "-frames:v",
            str(frames),
            "-pix_fmt",
            "yuv420p",
            str(caminho),
        ],
        check=True,
    )
    return caminho


def test_video_maior_que_o_detector_chega_com_o_lado_maior_igual_a_ele(tmp_path: Path):
    """O `pessoas.mp4` do `scripts/modelo.sh` é 768×432, e o letterbox do YOLOX
    recusa frame cujo lado maior não é o do modelo — no agente quem ajusta é o
    filtro do ffmpeg (§3.1). O benchmark lia o vídeo cru e parava no primeiro
    frame, sem medir nada. Escalando como o agente escala, 768×432 vira 640×360."""
    frames = bench.le_frames(_video(tmp_path / "v.mp4", 768, 432), 2, lado_maior=640)
    assert len(frames) == 2
    assert {f.shape for f in frames} == {(360, 640, 3)}


def test_video_em_pe_escala_pela_altura(tmp_path: Path):
    frames = bench.le_frames(_video(tmp_path / "v.mp4", 432, 768), 1, lado_maior=640)
    assert frames[0].shape == (640, 360, 3)


def test_video_menor_que_o_detector_tambem_e_ajustado(tmp_path: Path):
    """O letterbox recusa nos dois sentidos: 320×240 também não é 640."""
    frames = bench.le_frames(_video(tmp_path / "v.mp4", 320, 240), 1, lado_maior=640)
    assert frames[0].shape == (480, 640, 3)


def test_pasta_de_imagens_tambem_e_ajustada(tmp_path: Path):
    import shutil
    import subprocess

    pasta = tmp_path / "frames"
    pasta.mkdir()
    _video(tmp_path / "v.mp4", 768, 432, frames=1)
    subprocess.run(  # noqa: S603
        [
            shutil.which("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(tmp_path / "v.mp4"),
            str(pasta / "a.png"),
        ],
        check=True,
    )
    frames = bench.le_frames(pasta, 5, lado_maior=640)
    assert frames[0].shape == (360, 640, 3)
