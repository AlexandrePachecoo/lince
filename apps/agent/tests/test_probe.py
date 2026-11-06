"""Leitura do ffprobe: o que a câmera entrega de fato.

O item 1 do §10 — taxa real de frames do substream — sai daqui, e é a base do
dimensionamento de decode.
"""

from __future__ import annotations

import json
import logging
import subprocess
import traceback

import pytest

from lince_agent.config import DecodeOptions
from lince_agent.ffmpeg import probe as probe_mod
from lince_agent.ffmpeg.probe import ProbeError, StreamInfo, _parse_rate, probe_stream

URL = "rtsp://cam/substream"


def resposta(**stream) -> str:
    base = {
        "codec_name": "h264",
        "width": 640,
        "height": 480,
        "avg_frame_rate": "15/1",
        "r_frame_rate": "15/1",
    }
    return json.dumps({"streams": [base | stream]})


class Saida:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def finge(monkeypatch, saida: Saida) -> None:
    monkeypatch.setattr(probe_mod.subprocess, "run", lambda *a, **k: saida)


@pytest.mark.parametrize(
    ("valor", "esperado"),
    [
        ("15/1", 15.0),
        ("30000/1001", pytest.approx(29.97, abs=0.01)),
        ("0/0", 0.0),
        ("", 0.0),
        (None, 0.0),
        ("lixo", 0.0),
    ],
)
def test_taxa_vem_como_fracao(valor, esperado):
    """`avg_frame_rate` é fração, e "0/0" significa desconhecido — não zero fps."""
    assert _parse_rate(valor) == esperado


def test_le_o_stream(monkeypatch):
    finge(monkeypatch, Saida(stdout=resposta()))
    info = probe_stream(URL, DecodeOptions())
    assert info == StreamInfo(
        codec="h264", width=640, height=480, avg_frame_rate=15.0, r_frame_rate=15.0
    )


def test_divergencia_entre_taxa_medida_e_nominal_e_preservada(monkeypatch):
    """Câmera CFTV declara uma taxa e entrega outra sob carga. A diferença entre as
    duas é informação, não erro a normalizar."""
    finge(monkeypatch, Saida(stdout=resposta(avg_frame_rate="9/1", r_frame_rate="15/1")))
    info = probe_stream(URL, DecodeOptions())
    assert (info.avg_frame_rate, info.r_frame_rate) == (9.0, 15.0)


def test_resolucao_diferente_da_configurada_gera_aviso(monkeypatch, caplog):
    """O filtro `scale` reescalaria em CPU a cada frame, em silêncio, e o custo só
    apareceria no benchmark."""
    finge(monkeypatch, Saida(stdout=resposta(width=1280, height=720)))
    with caplog.at_level(logging.WARNING):
        info = probe_stream(URL, DecodeOptions())
    assert not info.matches(DecodeOptions())
    assert "reescala" in caplog.text


def test_resolucao_igual_nao_avisa(monkeypatch, caplog):
    finge(monkeypatch, Saida(stdout=resposta()))
    with caplog.at_level(logging.WARNING):
        probe_stream(URL, DecodeOptions())
    assert "reescala" not in caplog.text


def test_ffprobe_com_erro(monkeypatch):
    finge(monkeypatch, Saida(returncode=1, stderr="Connection refused"))
    with pytest.raises(ProbeError, match="Connection refused"):
        probe_stream(URL, DecodeOptions())


def test_sem_stream_de_video(monkeypatch):
    finge(monkeypatch, Saida(stdout=json.dumps({"streams": []})))
    with pytest.raises(ProbeError, match="nenhum stream"):
        probe_stream(URL, DecodeOptions())


def test_saida_que_nao_e_json(monkeypatch):
    finge(monkeypatch, Saida(stdout="isto não é json"))
    with pytest.raises(ProbeError, match="JSON"):
        probe_stream(URL, DecodeOptions())


def test_timeout_vira_erro_com_o_prazo(monkeypatch):
    """Câmera que aceita a conexão e nunca responde não pode segurar a subida do
    agente para sempre."""

    def estoura(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=20)

    monkeypatch.setattr(probe_mod.subprocess, "run", estoura)
    with pytest.raises(ProbeError, match="não respondeu"):
        probe_stream(URL, DecodeOptions(), timeout_s=20.0)


def test_erro_nao_vaza_a_credencial_da_url(monkeypatch):
    """O ffprobe devolve a URL no stderr (`rtsp://user:pass@host/cam: Connection
    refused`) e a ProbeError a repetia na mensagem. As duas iam parar no log da
    subida. O argv que o processo recebe continua com a senha — sem ela a câmera
    não autentica."""
    senha = "s3nha-super-secreta"
    url = f"rtsp://operador:{senha}@cam.loja:554/substream"
    vistos: list[list[str]] = []

    def executa(argv, **kwargs):  # noqa: ANN001 - a mesma assinatura do subprocess.run
        vistos.append(argv)
        return Saida(returncode=1, stderr=f"{url}: Server returned 401 Unauthorized\n")

    monkeypatch.setattr(probe_mod.subprocess, "run", executa)
    with pytest.raises(ProbeError) as erro:
        probe_stream(url, DecodeOptions())

    assert vistos and url in vistos[0]
    mensagem = str(erro.value)
    assert senha not in mensagem
    assert "operador" not in mensagem
    assert "cam.loja:554/substream" in mensagem


def test_timeout_nao_vaza_a_credencial_no_traceback(monkeypatch):
    """`TimeoutExpired` carrega o argv no `str()`. Encadear essa causa na ProbeError
    republicaria a senha no primeiro traceback, mesmo com a mensagem já redigida."""
    senha = "s3nha-super-secreta"
    url = f"rtsp://operador:{senha}@cam.loja:554/substream"

    def estoura(*args, **kwargs):  # noqa: ANN002 - a mesma assinatura do subprocess.run
        raise subprocess.TimeoutExpired(cmd=["ffprobe", "-i", url], timeout=20)

    monkeypatch.setattr(probe_mod.subprocess, "run", estoura)
    with pytest.raises(ProbeError) as erro:
        probe_stream(url, DecodeOptions(), timeout_s=20.0)

    texto = "".join(traceback.format_exception(erro.value))
    assert senha not in texto
    assert "operador:" not in texto
    assert "cam.loja" in str(erro.value)
    assert erro.value.__cause__ is None


def test_ffprobe_ausente(monkeypatch):
    def falha(*a, **k):
        raise OSError("No such file or directory")

    monkeypatch.setattr(probe_mod.subprocess, "run", falha)
    with pytest.raises(ProbeError, match="não consegui executar"):
        probe_stream(URL, DecodeOptions())
