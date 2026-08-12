"""Detecção de aceleração por hardware, sem depender de ter GPU.

A saída do ffmpeg é fixada como texto real: é assim que a máquina de desenvolvimento
consegue testar o caminho NVDEC, que ela não roda.
"""

from __future__ import annotations

import pytest

from lince_agent.config import HwAccel
from lince_agent.ffmpeg import capabilities
from lince_agent.ffmpeg.capabilities import (
    available_decoders,
    available_hwaccels,
    hwaccel_works,
    select_hwaccel,
)

HWACCELS_COM_CUDA = """Hardware acceleration methods:
vdpau
cuda
vaapi
qsv
drm
opencl
vulkan
"""

HWACCELS_SEM_CUDA = """Hardware acceleration methods:
vaapi
drm
"""

DECODERS = """Decoders:
 V..... = Video
 -------
 VFS..D h264                 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10
 V..... h264_cuvid           Nvidia CUVID H264 decoder (codec h264)
 V....D hevc_qsv             HEVC video (Intel Quick Sync Video acceleration)
 A....D aac                  AAC (Advanced Audio Coding)
"""


@pytest.fixture(autouse=True)
def limpa_cache():
    """As funções são `functools.cache`; sem isto um teste veria a resposta do
    anterior."""
    cacheadas = (
        available_hwaccels,
        available_decoders,
        hwaccel_works,
        capabilities._synthetic_h264,
    )
    for função in cacheadas:
        função.cache_clear()
    yield
    for função in cacheadas:
        função.cache_clear()


def test_le_a_lista_de_hwaccels_ignorando_o_cabecalho(monkeypatch):
    monkeypatch.setattr(capabilities, "_run", lambda _: HWACCELS_COM_CUDA)
    accels = available_hwaccels("ffmpeg")
    assert "cuda" in accels
    assert "Hardware acceleration methods:" not in accels


def test_le_os_decoders_ignorando_legenda_e_cabecalho(monkeypatch):
    monkeypatch.setattr(capabilities, "_run", lambda _: DECODERS)
    decoders = available_decoders("ffmpeg")
    assert {"h264", "h264_cuvid", "hevc_qsv", "aac"} <= decoders
    assert "=" not in "".join(decoders)


def test_ffmpeg_ausente_nao_explode(monkeypatch):
    def falha(_):
        raise OSError("ffmpeg não encontrado")

    monkeypatch.setattr(capabilities, "_run", falha)
    assert available_hwaccels("ffmpeg") == frozenset()


def test_cpu_nao_precisa_de_verificacao():
    assert hwaccel_works(HwAccel.NONE) is True
    assert select_hwaccel(HwAccel.NONE) is HwAccel.NONE


def test_build_sem_cuda_reprova_na_lista(monkeypatch):
    monkeypatch.setattr(capabilities, "_run", lambda _: HWACCELS_SEM_CUDA)
    assert hwaccel_works(HwAccel.CUDA, "ffmpeg") is False


def test_build_com_cuda_mas_sem_o_decoder_reprova(monkeypatch):
    """Um ffmpeg pode listar `cuda` como método e não ter o decoder NVDEC."""

    def responde(argv):
        return HWACCELS_COM_CUDA if "-hwaccels" in argv else "Decoders:\n VFS..D h264   H.264\n"

    monkeypatch.setattr(capabilities, "_run", responde)
    assert hwaccel_works(HwAccel.CUDA, "ffmpeg") is False


def test_lista_completa_nao_basta_sem_driver(monkeypatch):
    """A razão de existir o teste de decode real: `-hwaccels` reflete o build, não
    a máquina. Um ffmpeg compilado com CUDA lista `cuda` num servidor sem GPU."""

    def responde(argv):
        return HWACCELS_COM_CUDA if "-hwaccels" in argv else DECODERS

    monkeypatch.setattr(capabilities, "_run", responde)
    monkeypatch.setattr(capabilities, "_synthetic_h264", lambda _: b"clipe-falso")

    class Falhou:
        returncode = 1
        stderr = b"Cannot load libcuda.so.1"

    monkeypatch.setattr(capabilities.subprocess, "run", lambda *a, **k: Falhou())
    assert hwaccel_works(HwAccel.CUDA, "ffmpeg") is False


def test_decode_de_teste_bem_sucedido_aprova(monkeypatch):
    def responde(argv):
        return HWACCELS_COM_CUDA if "-hwaccels" in argv else DECODERS

    monkeypatch.setattr(capabilities, "_run", responde)
    monkeypatch.setattr(capabilities, "_synthetic_h264", lambda _: b"clipe-falso")

    class Passou:
        returncode = 0
        stderr = b""

    monkeypatch.setattr(capabilities.subprocess, "run", lambda *a, **k: Passou())
    assert hwaccel_works(HwAccel.CUDA, "ffmpeg") is True


def test_sem_libx264_confia_na_lista(monkeypatch):
    """Build mínimo não consegue montar o clipe de teste. Recusar a aceleração aí
    seria pior: deixaria o box com GPU decodificando em CPU por causa de um
    encoder que ele nem usa."""

    def responde(argv):
        return HWACCELS_COM_CUDA if "-hwaccels" in argv else DECODERS

    monkeypatch.setattr(capabilities, "_run", responde)
    monkeypatch.setattr(capabilities, "_synthetic_h264", lambda _: None)
    assert hwaccel_works(HwAccel.CUDA, "ffmpeg") is True


def test_select_cai_para_cpu_em_vez_de_recusar_subir(monkeypatch):
    """Degradar é o comportamento certo: um agente que se recusa a subir por falta
    de driver deixa a loja sem detecção nenhuma (§3.2)."""
    monkeypatch.setattr(capabilities, "_run", lambda _: HWACCELS_SEM_CUDA)
    assert select_hwaccel(HwAccel.CUDA, ffmpeg_bin="ffmpeg") is HwAccel.NONE


def test_select_mantem_a_aceleracao_que_funciona(monkeypatch):
    monkeypatch.setattr(capabilities, "hwaccel_works", lambda *a, **k: True)
    assert select_hwaccel(HwAccel.CUDA, ffmpeg_bin="ffmpeg") is HwAccel.CUDA
