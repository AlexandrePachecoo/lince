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
    RuleOptions,
    TrackingOptions,
)
from lince_agent.rules.geometry import LinhaOrientada, Poligono


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


# --- estágio 3: tracking (§3.3) ---------------------------------------------


def test_tracking_nasce_ligado():
    """Diferente da detecção, que precisa de um modelo no disco, o tracking não tem
    pré-requisito nenhum: se há detecções, há o que rastrear."""
    assert TrackingOptions().enabled is True


def test_gate_de_track_novo_e_o_mais_frouxo_dos_tres():
    """Não é descuido, é aritmética: um track recém-nascido não tem velocidade
    estimada, então a previsão do Kalman é a caixa parada. A 3 fps uma pessoa andando
    percorre ~55 px sobre uma caixa de 80, o que dá IoU de 0,19 — qualquer gate acima
    disso faria o agente só rastrear quem está parado."""
    opcoes = TrackingOptions()
    assert opcoes.iou_min_novo < opcoes.iou_min < opcoes.iou_min_baixa


def test_janelas_de_tempo_sao_em_segundos():
    """As implementações de referência contam frames porque assumem 30 fps fixos. Aqui
    a cadência é 3 fps e varia com o descarte do §3.2 — contar frames faria a tolerância
    a oclusão mudar sozinha conforme a carga do box."""
    assert TrackingOptions().max_perdido_s == pytest.approx(2.0)


@pytest.mark.parametrize("campo", ["high_threshold", "iou_min", "iou_min_baixa", "iou_min_novo"])
def test_limiares_de_tracking_fora_de_0_1_sao_recusados(campo: str):
    with pytest.raises(ValueError, match=campo):
        TrackingOptions(**{campo: 1.4})


def test_min_hits_precisa_de_ao_menos_uma_associacao():
    with pytest.raises(ValueError, match="min_hits"):
        TrackingOptions(min_hits=0)


def test_janela_de_perdido_negativa_e_recusada():
    with pytest.raises(ValueError, match="max_perdido_s"):
        TrackingOptions(max_perdido_s=-1.0)


def test_teto_de_tracks_precisa_ser_positivo():
    with pytest.raises(ValueError, match="max_tracks"):
        TrackingOptions(max_tracks=0)


def test_piso_do_detector_e_menor_que_o_corte_do_tracker():
    """A relação que faz o ByteTrack funcionar: o detector deixa passar caixas fracas
    (piso 0,10) e o tracker é quem decide o que é pessoa (0,50). Inverter isso esconderia
    do estágio 3 exatamente o dado de que ele mais precisa."""
    assert DetectionOptions().score_threshold < TrackingOptions().high_threshold


# --- zonas e limiares do estágio 4 (§3.4) ---------------------------------------

LINHA = LinhaOrientada(origem=(0.0, 400.0), destino=(640.0, 400.0))
CAIXA = Poligono(((100.0, 420.0), (340.0, 420.0), (340.0, 470.0), (100.0, 470.0)))


def regra(**kwargs) -> RuleOptions:
    padrao = {"enabled": True, "linha_saida": LINHA, "zonas_caixa": (CAIXA,)}
    return RuleOptions(**{**padrao, **kwargs})


def test_regra_desligada_nao_exige_nada():
    """É o estado de toda câmera no dia da instalação: ela ingere, detecta e rastreia
    antes de alguém ter desenhado uma linha sobre o frame."""
    assert RuleOptions().enabled is False


def test_regra_habilitada_exige_linha_de_saida():
    with pytest.raises(ValueError, match="linha_saida"):
        RuleOptions(enabled=True, zonas_caixa=(CAIXA,))


def test_regra_habilitada_exige_zona_de_caixa():
    """Sem zona de caixa o tempo acumulado é sempre zero, e **toda** saída da loja vira
    alerta. Uma câmera assim alerta a loja inteira até alguém desligar a notificação —
    que é exatamente como o R-1 mata o produto. Recusar na subida é o único momento em
    que isso custa barato."""
    with pytest.raises(ValueError, match="zona de caixa"):
        RuleOptions(enabled=True, linha_saida=LINHA)


def test_tempo_de_caixa_zero_e_recusado():
    """Com N = 0, a comparação `tempo < N` nunca é verdadeira e a câmera fica muda. É o
    oposto do erro anterior e igualmente silencioso."""
    with pytest.raises(ValueError, match="tempo_caixa_min_s"):
        regra(tempo_caixa_min_s=0.0)


def test_zona_fora_do_quadro_e_recusada_na_subida():
    """**O erro de resolução.**

    As zonas são desenhadas sobre um frame no dashboard e avaliadas contra caixas no
    espaço do frame decodificado (640x480). Quem desenhar sobre um frame 1080p produz um
    polígono que não contém ninguém: `contem` devolve `False` sempre, o tempo de caixa
    fica em zero e a câmera passa a alertar para todo cliente que sai. Nada disso levanta
    erro em runtime — o único sintoma é o volume de alertas.
    """
    with pytest.raises(ValueError, match="fora do quadro"):
        CameraConfig(
            camera_id="cam1",
            url="rtsp://camera/stream",
            rules=regra(linha_saida=LinhaOrientada(origem=(0.0, 900.0), destino=(1900.0, 900.0))),
        )


def test_regra_em_camera_inelegivel_para_ia_e_recusada():
    """Uma câmera com `detect=False` (R-3) não produz track nenhum, então a regra nunca
    avaliaria nada. Aceitar a configuração criaria uma câmera que parece vigiada no
    dashboard e é muda na prática."""
    with pytest.raises(ValueError, match="detect=False"):
        CameraConfig(camera_id="cam1", url="rtsp://camera/stream", detect=False, rules=regra())


def test_zonas_sao_por_camera():
    """A linha de saída de uma câmera é o corredor de outra (ADR-002): recalibrar uma
    delas não pode mexer nas demais."""
    porta = CameraConfig(camera_id="porta", url="rtsp://camera/1", rules=regra())
    corredor = CameraConfig(camera_id="corredor", url="rtsp://camera/2")
    assert porta.rules.enabled and not corredor.rules.enabled
