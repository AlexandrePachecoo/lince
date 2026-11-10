"""A medida da regra do gesto contra o dataset: quantos furtos pega, quantos alarmes dá.

A regra só vale o número que ela produz. Sem esta ferramenta, "a regra funciona"
seria impressão de quem viu três vídeos. O que precisa estar certo:

- a taxa é **por vídeo e por pasta**: um vídeo de `Shoplifting` com disparo é um
  furto pego; um de `Normal` com disparo é um alerta falso;
- o cache guarda a pose de cada frame. Ajustar um limiar não pode exigir rodar
  YOLOX e RTMPose de novo nos 182 vídeos — e o cache não pode ser reaproveitado
  quando foi feito com outra taxa de amostragem, senão a medida mistura duas.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from gesto_util import pose_com

from lince_agent.detect.state import Detection

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
if not (_TOOLS / "avalia_gesto.py").is_file():
    pytest.skip(
        f"{_TOOLS} não está nesta árvore. A imagem do agente não copia tools/.",
        allow_module_level=True,
    )


def _carrega(nome: str):
    if nome in sys.modules:
        return sys.modules[nome]
    spec = importlib.util.spec_from_file_location(nome, _TOOLS / f"{nome}.py")
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[nome] = modulo
    spec.loader.exec_module(modulo)
    return modulo


_carrega("qualifica_camera")
avalia = _carrega("avalia_gesto")

LONGE, MEIO, BOLSA = (1.6, -0.1), (0.7, 0.0), (0.6, 0.7)


def _quadros(trajeto, fps=8.0, **kwargs):
    """Frames cacheados de uma pessoa com o pulso direito seguindo `trajeto`."""
    return [
        avalia.QuadroCacheado(
            instante=i / fps,
            sequence=i,
            pessoas=((1, (0.0, 0.0, 10.0, 10.0), pose_com(pulso_direito=p, **kwargs)),),
        )
        for i, p in enumerate(trajeto)
    ]


GESTO = [LONGE, LONGE, MEIO, BOLSA, BOLSA, BOLSA, BOLSA, MEIO]
PARADO = [MEIO] * 8


# --- o cache ---------------------------------------------------------------------


def test_cache_volta_igual(tmp_path: Path):
    quadros = _quadros(GESTO)
    arquivo = tmp_path / "v.json"
    avalia.grava_cache(arquivo, quadros, chave={"fps": 8.0, "largura": None, "bytes": 10})
    lido = avalia.le_cache(arquivo, chave={"fps": 8.0, "largura": None, "bytes": 10})
    assert lido is not None
    assert [q.instante for q in lido] == [q.instante for q in quadros]
    original = quadros[3].pessoas[0][2].juntas[10]
    voltou = lido[3].pessoas[0][2].juntas[10]
    assert (voltou.x, voltou.y, voltou.confianca) == pytest.approx(
        (original.x, original.y, original.confianca)
    )


def test_cache_de_outra_amostragem_nao_e_usado(tmp_path: Path):
    """Cache feito a 3 fps lido como 8 fps mediria uma janela de 2 s com 6
    amostras achando que tem 16. A regra mudaria sem ninguém tocar nela."""
    arquivo = tmp_path / "v.json"
    avalia.grava_cache(arquivo, _quadros(GESTO), chave={"fps": 3.0, "largura": None, "bytes": 10})
    assert avalia.le_cache(arquivo, chave={"fps": 8.0, "largura": None, "bytes": 10}) is None


def test_cache_de_outro_arquivo_nao_e_usado(tmp_path: Path):
    """Vídeo regravado com o mesmo nome tem outro tamanho. O cache dele é de outra cena."""
    arquivo = tmp_path / "v.json"
    avalia.grava_cache(arquivo, _quadros(GESTO), chave={"fps": 8.0, "largura": None, "bytes": 10})
    assert avalia.le_cache(arquivo, chave={"fps": 8.0, "largura": None, "bytes": 11}) is None


def test_cache_corrompido_e_refeito_e_nao_derruba(tmp_path: Path):
    arquivo = tmp_path / "v.json"
    arquivo.write_text("{ não é json")
    assert avalia.le_cache(arquivo, chave={"fps": 8.0, "largura": None, "bytes": 10}) is None


# --- a medida ---------------------------------------------------------------------


def test_disparos_por_video_saem_do_motor():
    from lince_agent.gesto.motor import ParametrosGesto

    disparos = avalia.disparos_do_video("Shoplifting/a.mp4", _quadros(GESTO), ParametrosGesto())
    assert len(disparos) == 1
    assert disparos[0].camera_id == "Shoplifting/a.mp4"
    assert avalia.disparos_do_video("Normal/b.mp4", _quadros(PARADO), ParametrosGesto()) == []


def test_parametros_mudam_o_resultado():
    """Permanência maior que a corrida na bolsa e o mesmo vídeo deixa de disparar.
    É o que a flag existe para mostrar."""
    from lince_agent.gesto.motor import ParametrosGesto

    exigente = ParametrosGesto(permanencia_min_s=2.0)
    assert avalia.disparos_do_video("a.mp4", _quadros(GESTO), exigente) == []


def test_relatorio_conta_videos_com_disparo_por_pasta():
    """Pegar 1 de 2 furtos e alarmar em 1 de 3 normais são dois números, e só
    juntos dizem se a regra presta. Um sem o outro engana para os dois lados."""
    from lince_agent.gesto.motor import ParametrosGesto

    p = ParametrosGesto()
    por_video = {
        "Shoplifting/a.mp4": (avalia.disparos_do_video("x", _quadros(GESTO), p), 1.0),
        "Shoplifting/b.mp4": ([], 1.0),
        "Normal/c.mp4": (avalia.disparos_do_video("y", _quadros(GESTO), p), 1.0),
        "Normal/d.mp4": ([], 1.0),
        "Normal/e.mp4": ([], 1.0),
    }
    relatorio = avalia.monta_relatorio(por_video, p)
    assert relatorio["por_pasta"]["Shoplifting"]["videos"] == 2
    assert relatorio["por_pasta"]["Shoplifting"]["com_disparo"] == 1
    assert relatorio["por_pasta"]["Normal"]["fracao_com_disparo"] == pytest.approx(1 / 3)
    texto = avalia.formata(relatorio)
    assert "Shoplifting" in texto and "Normal" in texto and "50%" in texto and "33%" in texto


# --- onde a regra parou --------------------------------------------------------------


def _etapa(quadros, **parametros):
    from lince_agent.gesto.motor import ParametrosGesto

    p = ParametrosGesto(**parametros)
    disparou = bool(avalia.disparos_do_video("v.mp4", quadros, p))
    return avalia.diagnostica_video(quadros, p, disparou=disparou)[0]


@pytest.mark.parametrize(
    ("quadros", "esperada"),
    [
        ([avalia.QuadroCacheado(instante=0.0, sequence=0, pessoas=())], "sem_pessoa"),
        (_quadros([BOLSA] * 8, tronco=False), "sem_tronco"),
        (_quadros(PARADO), "sem_alcance"),
        (_quadros([LONGE, MEIO] * 4), "sem_zona"),
        (_quadros([LONGE] + [MEIO] * 20 + [BOLSA] * 4), "zona_sem_alcance_antes"),
        (_quadros([LONGE, MEIO, BOLSA, MEIO, MEIO]), "alcance_e_zona_sem_disparo"),
        (_quadros(GESTO), "disparou"),
    ],
)
def test_diagnostico_diz_ate_onde_o_video_chegou(quadros, esperada):
    """Fração de furtos pegos baixa não diz o que mexer; a etapa diz. Furto parado
    antes do alcance pede outro `alcance_min`; parado antes da zona, outra zona.
    Uma etapa errada aqui manda ajustar o limiar que não tem culpa."""
    assert _etapa(quadros) == esperada


def test_diagnostico_conta_os_estados_das_amostras():
    _etapa_, contagem = avalia.diagnostica_video(
        _quadros(GESTO), avalia.ParametrosGesto(), disparou=True
    )
    # Pulso esquerdo não é visto em nenhum frame; o direito passa por longe, meio e zona.
    assert contagem["sumido"] == len(GESTO)
    assert (contagem["longe"], contagem["meio"], contagem["zona"]) == (2, 2, 4)


def test_diagnostico_por_pasta_soma_etapas_e_estados():
    """Normal chegando às mesmas etapas que Shoplifting é a resposta de que nenhum
    limiar separa as classes — a tabela precisa pôr as duas lado a lado."""
    p = avalia.ParametrosGesto()
    por_video = {
        "Shoplifting/a.mp4": avalia.diagnostica_video(_quadros(GESTO), p, disparou=True),
        "Shoplifting/b.mp4": avalia.diagnostica_video(_quadros(PARADO), p, disparou=False),
        "Normal/c.mp4": avalia.diagnostica_video(_quadros(PARADO), p, disparou=False),
    }
    d = avalia.monta_diagnostico(por_video)
    assert d["Shoplifting"]["etapas"]["disparou"] == 1
    assert d["Shoplifting"]["etapas"]["sem_alcance"] == 1
    assert d["Normal"]["etapas"]["sem_alcance"] == 1
    assert sum(d["Normal"]["fracao_estados"].values()) == pytest.approx(1.0)
    texto = avalia.formata_diagnostico(d)
    assert "sem_alcance" in texto and "Normal" in texto


# --- varredura -----------------------------------------------------------------------


def test_varredura_poe_primeiro_o_que_separa_as_pastas():
    """O furto alcança de 1,3 troncos; o normal, de 1,0. Com `alcance_min` 1,2 só o
    furto dispara; com 0,8 os dois. A primeira linha tem que ser a que separa."""
    furto = [(1.3, -0.1), (1.3, -0.1), MEIO] + [BOLSA] * 4
    normal = [(1.0, -0.1), (1.0, -0.1), MEIO] + [BOLSA] * 4
    linhas = avalia.varre(
        {"Shoplifting/a.mp4": _quadros(furto), "Normal/b.mp4": _quadros(normal)},
        avalia.ParametrosGesto(),
        {"alcance_min": (0.8, 1.2)},
    )
    assert [linha["parametros"]["alcance_min"] for linha in linhas] == [1.2, 0.8]
    assert linhas[0]["fracoes"] == {"Normal": 0.0, "Shoplifting": 1.0}
    assert "alcance_min" in avalia.formata_varredura(linhas)


def test_varredura_pula_combinacao_invalida():
    """Zona com y mínimo acima do máximo é recusada pelo motor; a grade segue."""
    linhas = avalia.varre(
        {"Normal/b.mp4": _quadros(PARADO)},
        avalia.ParametrosGesto(),
        {"zona_y_max": (0.1, 1.4)},
    )
    assert [linha["parametros"]["zona_y_max"] for linha in linhas] == [1.4]


# --- a linha de comando, com vídeo de verdade e modelos dublês -------------------


def _video(caminho: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg não está no PATH")
    caminho.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=30",
            "-t",
            "2",
            "-pix_fmt",
            "yuv420p",
            str(caminho),
        ],
        check=True,
    )
    return caminho


class Contador:
    deteccoes = 0


def _modelos_duble(monkeypatch, trajeto):
    class OnnxDetectorFalso:
        def __init__(self, caminho, opcoes) -> None:
            pass

        def detect(self, frame):
            Contador.deteccoes += 1
            return (Detection(class_id=0, score=0.9, x1=100, y1=20, x2=220, y2=230),)

        def close(self) -> None:
            pass

    class OnnxPoseFalsa:
        def __init__(self, caminho, providers) -> None:
            self.n = 0

        def estimar(self, imagem, caixas):
            posicao = trajeto[min(self.n, len(trajeto) - 1)]
            self.n += 1
            return tuple(pose_com(pulso_direito=posicao) for _ in caixas)

        def close(self) -> None:
            pass

    monkeypatch.setattr(avalia, "OnnxDetector", OnnxDetectorFalso)
    monkeypatch.setattr(avalia, "OnnxPose", OnnxPoseFalsa)


def test_segunda_rodada_usa_o_cache_e_nao_toca_nos_modelos(tmp_path: Path, monkeypatch, capsys):
    _modelos_duble(monkeypatch, [LONGE, LONGE, MEIO] + [BOLSA] * 20)
    fonte = tmp_path / "dataset"
    _video(fonte / "Shoplifting" / "a.mp4")
    cache = tmp_path / "cache"
    argv = [
        str(fonte),
        "--cache",
        str(cache),
        "--detector",
        "d.onnx",
        "--pose",
        "p.onnx",
        "--provider",
        "cpu",
        "--saida",
        str(tmp_path / "r.json"),
    ]

    Contador.deteccoes = 0
    assert avalia.main(argv) == 0
    primeira = Contador.deteccoes
    assert primeira > 0
    assert (cache / "Shoplifting" / "a.mp4.json").is_file()

    assert avalia.main(argv) == 0
    assert Contador.deteccoes == primeira
    relatorio = json.loads((tmp_path / "r.json").read_text())
    assert relatorio["por_pasta"]["Shoplifting"]["com_disparo"] == 1
    assert "Shoplifting" in capsys.readouterr().out


def test_cache_completo_dispensa_os_modelos(tmp_path: Path, monkeypatch):
    """Com tudo em cache, ajustar a regra não precisa nem de GPU nem dos .onnx."""
    fonte = tmp_path / "dataset"
    video = _video(fonte / "Normal" / "a.mp4")
    cache = tmp_path / "cache"
    chave = {"fps": 8.0, "largura": None, "bytes": video.stat().st_size}
    avalia.grava_cache(cache / "Normal" / "a.mp4.json", _quadros(PARADO), chave=chave)

    def explode(*_a, **_k):
        raise AssertionError("não deveria abrir modelo com o cache completo")

    monkeypatch.setattr(avalia, "OnnxDetector", explode)
    monkeypatch.setattr(avalia, "OnnxPose", explode)
    assert avalia.main([str(fonte), "--cache", str(cache), "--alcance-min", "1.5"]) == 0


def test_cli_mostra_o_diagnostico_e_varre_sem_modelo(tmp_path: Path, capsys):
    fonte = tmp_path / "dataset"
    video = _video(fonte / "Shoplifting" / "a.mp4")
    cache = tmp_path / "cache"
    chave = {"fps": 8.0, "largura": None, "bytes": video.stat().st_size}
    avalia.grava_cache(cache / "Shoplifting" / "a.mp4.json", _quadros(GESTO), chave=chave)
    saida = tmp_path / "r.json"
    argv = [str(fonte), "--cache", str(cache), "--varredura", "--saida", str(saida)]
    assert avalia.main(argv) == 0
    texto = capsys.readouterr().out
    assert "onde parou" in texto and "varredura" in texto
    relatorio = json.loads(saida.read_text())
    assert relatorio["videos"]["Shoplifting/a.mp4"]["etapa"] == "disparou"
    assert len(relatorio["varredura"]) > 1


def test_sem_cache_e_sem_modelo_e_erro_claro(tmp_path: Path, capsys):
    fonte = tmp_path / "dataset"
    _video(fonte / "Normal" / "a.mp4")
    assert avalia.main([str(fonte), "--cache", str(tmp_path / "cache")]) == 1
    assert "--detector" in capsys.readouterr().err
