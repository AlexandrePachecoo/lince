"""O tamanho do frame é o contrato do pipe de detecção: errar não dá erro,
embaralha todos os frames."""

from __future__ import annotations

import pytest

from lince_agent.config import CameraConfig, ClipOptions, DecodeOptions, HwAccel, PixelFormat


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


def test_a_janela_precisa_caber_o_corte_inteiro():
    """A janela não é enfeite: durante a espera do pós-roll o buffer continua
    podando, então ela precisa cobrir pré-roll **mais** pós-roll **mais** a
    tolerância. Uma janela de 6 s teria descartado o pré-roll antes de o corte
    acontecer, e o clipe sairia sem o que interessa (§3.5)."""
    with pytest.raises(ValueError, match="window_s"):
        ClipOptions(window_s=6.0)


def test_defaults_implementam_o_clipe_de_quinze_segundos():
    """Os números do §3.5: 5 s antes, 10 s depois, 30 s de buffer."""
    options = ClipOptions()
    assert options.pre_roll_s == 5.0
    assert options.post_roll_s == 10.0
    assert options.window_s == 30.0
    assert options.clip_duration_s == 15.0


def test_rolls_precisam_ser_positivos():
    with pytest.raises(ValueError, match="pre_roll_s"):
        ClipOptions(pre_roll_s=0)
    with pytest.raises(ValueError, match="post_roll_s"):
        ClipOptions(post_roll_s=-1)


def test_teto_de_bytes_precisa_caber_a_janela_de_uma_camera():
    """Teto menor que um fragmento transformaria o buffer em descarte contínuo."""
    with pytest.raises(ValueError, match="max_bytes"):
        ClipOptions(max_bytes=0)


def test_camera_carrega_a_configuracao_de_clipe():
    """Os limiares são por câmera (§3.4): uma loja com câmera de GOP longo na
    saída precisa de tolerância maior sem mexer nas outras."""
    camera = CameraConfig(camera_id="cam1", url="rtsp://x")
    assert camera.clip == ClipOptions()

    ajustada = CameraConfig(
        camera_id="cam2", url="rtsp://y", clip=ClipOptions(post_roll_grace_s=12.0)
    )
    assert ajustada.clip.post_roll_grace_s == 12.0
