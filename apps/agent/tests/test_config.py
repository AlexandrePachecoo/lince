"""O tamanho do frame é o contrato do pipe de detecção: errar não dá erro,
embaralha todos os frames."""

from __future__ import annotations

import pytest

from lince_agent.config import (
    AgentConfig,
    CameraConfig,
    ClipOptions,
    DecodeOptions,
    DetectionOptions,
    HwAccel,
    PixelFormat,
)


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


# --- estágio 2: detecção (§3.2) ---------------------------------------------


def test_deteccao_nasce_desligada():
    """Sem modelo no disco o agente ainda é um gravador de clipes útil, e o §3.9 exige
    que o mesmo artefato suba numa máquina sem GPU."""
    assert DetectionOptions().enabled is False


def test_habilitar_sem_modelo_e_recusado():
    with pytest.raises(ValueError, match="model_path"):
        DetectionOptions(enabled=True)


@pytest.mark.parametrize("lado", [0, -640, 100, 641])
def test_input_size_precisa_ser_multiplo_de_32(lado: int):
    """32 é o maior stride da grade de âncoras. Um lado que não é múltiplo produz
    grade fracionária, e a decodificação sai plausível e errada."""
    with pytest.raises(ValueError, match="múltiplo"):
        DetectionOptions(input_size=lado)


def test_providers_vazio_e_recusado():
    with pytest.raises(ValueError, match="providers"):
        DetectionOptions(providers=())


@pytest.mark.parametrize("campo", ["score_threshold", "iou_threshold"])
def test_limiares_fora_de_0_1_sao_recusados(campo: str):
    with pytest.raises(ValueError, match=campo):
        DetectionOptions(**{campo: 1.5})


def test_fps_window_precisa_de_duas_amostras():
    """Com uma amostra só não existe intervalo, e taxa sem intervalo é invenção."""
    with pytest.raises(ValueError, match="fps_window"):
        DetectionOptions(fps_window=1)


def test_camera_nasce_elegivel_para_ia():
    assert CameraConfig(camera_id="cam1", url="rtsp://x").detect is True


def agente(tmp_path, **kwargs) -> AgentConfig:
    return AgentConfig(
        tenant_id="rede",
        store_id="loja",
        clips_dir=tmp_path,
        **kwargs,
    )


def test_resolucao_que_nao_bate_com_o_modelo_derruba_a_subida(tmp_path):
    """Sem esta recusa o agente subiria, ingeriria, gravaria clipe e falharia uma vez
    por frame no letterbox — num log que ninguém lê. O conserto é no filtro do ffmpeg,
    que já escala de graça (§3.1)."""
    camera = CameraConfig(
        camera_id="cam1", url="rtsp://x", decode=DecodeOptions(width=1280, height=720)
    )
    with pytest.raises(ValueError, match="maior lado"):
        agente(
            tmp_path,
            cameras=(camera,),
            detection=DetectionOptions(enabled=True, model_path=tmp_path / "m.onnx"),
        )


def test_formato_planar_derruba_a_subida_com_deteccao_ligada(tmp_path):
    """NV12 é o que o `HwAccel.CUDA_GPU_FILTER` entrega — a configuração ótima do box.
    O aviso tem que dizer as duas saídas: marcar a câmera como inelegível, ou pedir
    bgr24 ao ffmpeg."""
    camera = CameraConfig(
        camera_id="cam1",
        url="rtsp://x",
        decode=DecodeOptions(pixel_format=PixelFormat.NV12, hwaccel=HwAccel.CUDA_GPU_FILTER),
    )
    with pytest.raises(ValueError, match="detect=False"):
        agente(
            tmp_path,
            cameras=(camera,),
            detection=DetectionOptions(enabled=True, model_path=tmp_path / "m.onnx"),
        )


def test_camera_inelegivel_escapa_das_exigencias_do_modelo(tmp_path):
    """É justamente o que `detect=False` serve para resolver: a câmera do R-3 continua
    no agente, com a resolução e o formato que quiser."""
    camera = CameraConfig(
        camera_id="cam1",
        url="rtsp://x",
        decode=DecodeOptions(width=1280, height=720),
        detect=False,
    )
    config = agente(
        tmp_path,
        cameras=(camera,),
        detection=DetectionOptions(enabled=True, model_path=tmp_path / "m.onnx"),
    )
    assert config.detection.enabled is True


def test_sem_deteccao_a_resolucao_nao_e_conferida(tmp_path):
    """O agente de hoje, sem modelo: nada do estágio 2 pode restringir o estágio 1."""
    camera = CameraConfig(
        camera_id="cam1", url="rtsp://x", decode=DecodeOptions(width=1920, height=1080)
    )
    assert agente(tmp_path, cameras=(camera,)).detection.enabled is False
