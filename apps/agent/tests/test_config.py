"""O tamanho do frame é o contrato do pipe de detecção: errar não dá erro,
embaralha todos os frames."""

from __future__ import annotations

import pytest

from lince_agent.config import CameraConfig, DecodeOptions, HwAccel, PixelFormat


def test_bgr24_sao_tres_bytes_por_pixel():
    assert PixelFormat.BGR24.frame_bytes(640, 480) == 921_600


def test_planares_420_sao_um_e_meio():
    assert PixelFormat.NV12.frame_bytes(640, 480) == 460_800
    assert PixelFormat.YUV420P.frame_bytes(640, 480) == 460_800


def test_planar_exige_dimensoes_pares():
    with pytest.raises(ValueError, match="pares"):
        PixelFormat.NV12.frame_bytes(641, 480)


def test_o_buffer_comprimido_e_ordens_de_grandeza_menor():
    """A conta que justifica o desenho de duas saídas: 30 s de BGR24 não cabem em
    RAM, 30 s de H.264 de substream cabem folgados."""
    trinta_segundos_bgr24 = DecodeOptions().frame_bytes * 15 * 30
    assert trinta_segundos_bgr24 > 400_000_000
    h264_substream = 512_000 // 8 * 30
    assert trinta_segundos_bgr24 / h264_substream > 100


def test_fps_precisa_ser_positivo():
    with pytest.raises(ValueError, match="sample_fps"):
        DecodeOptions(sample_fps=0)


def test_gpu_filter_exige_nv12():
    with pytest.raises(ValueError, match="NV12"):
        DecodeOptions(hwaccel=HwAccel.CUDA_GPU_FILTER)


def test_camera_precisa_de_id_e_url():
    with pytest.raises(ValueError, match="camera_id"):
        CameraConfig(camera_id="", url="rtsp://x")
    with pytest.raises(ValueError, match="url"):
        CameraConfig(camera_id="cam1", url="")
