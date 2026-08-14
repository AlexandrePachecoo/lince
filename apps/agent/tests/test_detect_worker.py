"""A thread do estágio 2 e a fronteira que protege a ingestão.

O que este arquivo guarda não é a inferência — é a promessa de que ela nunca segura a
thread despachante do ffmpeg. Um `submit` que bloqueia enche o pipe de 64 KB, e um pipe
cheio trava o processo ffmpeg inteiro: para o buffer do clipe junto e faz o RTSP perder
pacotes no socket. O sintoma seria "as câmeras degradaram quando ligamos o YOLO".
"""

from __future__ import annotations

import pytest
from deteccoes import (
    DetectorFalso,
    DetectorQueFalha,
    DetectorTravado,
    Relogio,
    frame,
    pessoa,
)

from lince_agent.config import DetectionOptions
from lince_agent.detect.detector import NullDetector
from lince_agent.detect.state import DetectionResult
from lince_agent.detect.worker import DetectorWorker


def worker(detector=None, *, queue_size: int = 4, clock=None, on_result=None) -> DetectorWorker:
    return DetectorWorker(
        detector or DetectorFalso((pessoa(),)),
        options=DetectionOptions(queue_size=queue_size, fps_window=8),
        on_result=on_result,
        clock=clock or Relogio(),
    )


# --- a fronteira com a ingestão -------------------------------------------------


def test_submit_nao_bloqueia_com_a_inferencia_travada():
    """GPU saturada com a thread presa dentro de `detect`. Os `submit` seguintes têm que
    voltar na hora — quem chama é a thread despachante do ffmpeg, e segurá-la trava o
    processo inteiro, inclusive a saída de fragmentos do §3.5."""
    travado = DetectorTravado()
    unidade = worker(travado, queue_size=2)
    unidade.start()
    try:
        unidade.submit("cam1", frame(1))
        assert travado.entrou.wait(timeout=5.0)

        # A thread está presa: ninguém drena. Estes vão todos para a fila e a estouram.
        for sequencia in range(2, 10):
            unidade.submit("cam1", frame(sequencia))

        assert unidade.stats().queue_depth == 2
        assert unidade.stats().dropped == 6
    finally:
        travado.solta.set()
        unidade.stop(timeout=5.0)


def test_descarta_o_frame_mais_antigo():
    """*Drop oldest*, como o §3.2 prescreve: com a GPU atrasada, o frame velho não vale
    mais nada — o §3.4 vai medir tempo em zona com o instante do frame, e trabalhar
    sobre o antigo em vez do novo só aumenta a latência do alerta."""
    detector = DetectorFalso()
    unidade = worker(detector, queue_size=2)

    for sequencia in (1, 2, 3):
        unidade.submit("cam1", frame(sequencia))

    assert unidade.tick() and unidade.tick()
    assert [visto.sequence for visto in detector.vistos] == [2, 3]


def test_descarte_e_cobrado_da_camera_que_o_perdeu():
    """A fila é uma só para todas as câmeras (ADR-007), e o §5.3 pede `dropped_frames`
    **por câmera**. Um agregado esconderia qual delas está saturando o box (R-4)."""
    unidade = worker(queue_size=1)
    unidade.submit("cam1", frame(1))
    unidade.submit("cam2", frame(1))

    por_camera = {camera.camera_id: camera.dropped for camera in unidade.stats().cameras}
    assert por_camera == {"cam1": 1, "cam2": 0}


def test_fila_vazia_nao_da_trabalho():
    assert worker().tick() is False


# --- sobrevivência --------------------------------------------------------------


def test_excecao_na_inferencia_nao_para_as_outras_cameras():
    """A thread é uma só para o agente inteiro. Morrer num frame corrompido apagaria a
    detecção da loja toda até alguém reiniciar o container."""
    detector = DetectorQueFalha(falhas=1)
    unidade = worker(detector)

    unidade.submit("cam1", frame(1))
    unidade.submit("cam2", frame(2))
    assert unidade.tick() and unidade.tick()

    stats = unidade.stats()
    assert stats.errors == 1
    assert stats.frames_inferred == 1
    assert {camera.camera_id: camera.errors for camera in stats.cameras} == {"cam1": 1, "cam2": 0}


def test_consumidor_que_falha_nao_derruba_a_thread():
    """`on_result` é o estágio 3 (§3.3). Deixá-lo derrubar esta thread daria o mesmo
    estrago que uma inferência ruim."""

    def explode(resultado: DetectionResult) -> None:
        raise RuntimeError("o tracker não gostou")

    unidade = worker(on_result=explode)
    unidade.submit("cam1", frame(1))

    assert unidade.tick() is True
    assert unidade.stats().errors == 1
    assert unidade.stats().frames_inferred == 1


def test_start_duas_vezes_e_erro():
    unidade = worker()
    unidade.start()
    try:
        with pytest.raises(RuntimeError, match="já está rodando"):
            unidade.start()
    finally:
        unidade.stop(timeout=5.0)


def test_stop_fecha_o_detector():
    """O detector segura a sessão do ONNX Runtime, que segura arenas de memória e, num
    box com GPU, VRAM."""
    detector = DetectorFalso()
    unidade = worker(detector)
    unidade.start()
    unidade.stop(timeout=5.0)
    assert detector.fechado is True


def test_stop_drena_o_que_ja_estava_na_fila():
    """São no máximo `queue_size` frames. Descartá-los na parada jogaria fora trabalho
    que já estava pronto — e a parada limpa é o caso comum (atualização por watchtower,
    §3.8), não a exceção."""
    detector = DetectorFalso()
    unidade = worker(detector, queue_size=4)
    for sequencia in (1, 2, 3):
        unidade.submit("cam1", frame(sequencia))

    unidade.start()
    unidade.stop(timeout=5.0)
    assert [visto.sequence for visto in detector.vistos] == [1, 2, 3]


# --- o que sai ------------------------------------------------------------------


def test_resultado_carrega_o_instante_do_frame_nao_o_da_inferencia():
    """O §3.4 vai medir tempo em zona com este timestamp. Usar o instante da inferência
    embutiria a latência da GPU no relógio das regras, e a fila do §3.2 varia com a
    carga — os limiares de tempo do motor mudariam sozinhos conforme o box enche."""
    recebidos: list[DetectionResult] = []
    unidade = worker(clock=Relogio(inicio=1000.0, passo=0.05), on_result=recebidos.append)

    unidade.submit("cam1", frame(7, received_at=3.5))
    unidade.tick()

    assert recebidos[0].received_at == 3.5
    assert recebidos[0].sequence == 7
    assert recebidos[0].camera_id == "cam1"
    assert recebidos[0].inference_ms == pytest.approx(50.0)


def test_deteccoes_chegam_ao_consumidor():
    recebidos: list[DetectionResult] = []
    unidade = worker(DetectorFalso((pessoa(), pessoa(x1=300.0))), on_result=recebidos.append)

    unidade.submit("cam1", frame(1))
    unidade.tick()

    assert len(recebidos[0].detections) == 2
    assert unidade.stats().detections == 2


def test_sem_consumidor_a_inferencia_ainda_conta():
    """O runtime pode subir sem o estágio 3 ligado, que é exatamente o estado de hoje.
    A telemetria do §5.3 não pode depender de alguém estar escutando."""
    unidade = worker(on_result=None)
    unidade.submit("cam1", frame(1))
    unidade.tick()
    assert unidade.stats().frames_inferred == 1


# --- telemetria do §5.3 ---------------------------------------------------------


def test_register_faz_a_camera_aparecer_antes_do_primeiro_frame():
    """Uma câmera que nunca entregou nada é a que mais interessa ao alerta técnico do
    §5.3. Sem isto, ela seria indistinguível de uma câmera que não existe."""
    unidade = worker()
    unidade.register("cam1")

    stats = unidade.stats()
    assert [camera.camera_id for camera in stats.cameras] == ["cam1"]
    assert stats.cameras[0].frames_in == 0
    assert stats.cameras[0].last_frame_at is None


def test_stats_separam_as_cameras():
    unidade = worker(queue_size=8)
    unidade.submit("cam1", frame(1))
    unidade.submit("cam1", frame(2))
    unidade.submit("cam2", frame(1))
    for _ in range(3):
        unidade.tick()

    por_camera = {camera.camera_id: camera.frames_inferred for camera in unidade.stats().cameras}
    assert por_camera == {"cam1": 2, "cam2": 1}


def test_inference_fps_e_medido_na_janela():
    """Três inferências espaçadas de 1/3 s dão 3 fps, que é a taxa amostrada do §3.2."""
    relogio = Relogio()
    unidade = worker(clock=relogio, queue_size=8)

    for sequencia in (1, 2, 3):
        if sequencia > 1:
            relogio.avanca(1 / 3)
        unidade.submit("cam1", frame(sequencia))
        unidade.tick()

    assert unidade.stats().cameras[0].inference_fps == pytest.approx(3.0)


def test_inference_fps_cai_quando_a_camera_para_de_entregar():
    """Com a janela sozinha, uma câmera que morreu há dez minutos continuaria reportando
    a taxa que tinha no instante em que morreu — e o §5.3 usa este campo justamente para
    detectar câmera degradada."""
    relogio = Relogio()
    unidade = worker(clock=relogio, queue_size=8)
    for sequencia in (1, 2, 3):
        if sequencia > 1:
            relogio.avanca(1 / 3)
        unidade.submit("cam1", frame(sequencia))
        unidade.tick()

    relogio.avanca(60.0)
    assert unidade.stats().cameras[0].inference_fps < 0.1


def test_uma_inferencia_so_nao_inventa_taxa():
    unidade = worker()
    unidade.submit("cam1", frame(1))
    unidade.tick()
    assert unidade.stats().cameras[0].inference_fps == 0.0


def test_stats_expoem_a_fila_e_o_modelo():
    """Profundidade e teto juntos: o §3.2 manda expor os dois para detectar GPU saturada
    **antes** de o descarte começar."""
    unidade = worker(queue_size=4)
    unidade.submit("cam1", frame(1))

    stats = unidade.stats()
    assert (stats.queue_depth, stats.queue_size) == (1, 4)
    assert stats.enabled is True
    assert stats.info is not None and stats.info.input_size == 640


# --- o detector nulo ------------------------------------------------------------


def test_null_detector_nao_detecta_e_se_declara_desligado():
    """Não é dublê: é o que roda numa câmera que o R-3 marcou como inelegível para IA, e
    num box sem modelo no disco (§3.9)."""
    unidade = worker(NullDetector())
    unidade.submit("cam1", frame(1))
    unidade.tick()

    stats = unidade.stats()
    assert stats.enabled is False
    assert stats.info is None
    assert stats.frames_inferred == 1
    assert stats.detections == 0
