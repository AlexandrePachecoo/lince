"""Um tracker por câmera, e a telemetria que denuncia fragmentação."""

from __future__ import annotations

import pytest
from deteccoes import Relogio

from lince_agent.config import TrackingOptions
from lince_agent.detect.state import Detection, DetectionResult
from lince_agent.track.pool import TrackerPool

PASSO_S = 1 / 3


def pessoa(x: float, *, altura: float = 200.0, score: float = 0.9) -> Detection:
    return Detection(
        class_id=0, score=score, x1=x, y1=250.0, x2=x + altura * 0.4, y2=250.0 + altura
    )


def frame(
    camera_id: str, deteccoes: tuple[Detection, ...], *, sequence: int, at: float
) -> DetectionResult:
    return DetectionResult(
        camera_id=camera_id,
        sequence=sequence,
        received_at=at,
        detections=deteccoes,
        inference_ms=10.0,
    )


def pool(**kwargs) -> TrackerPool:
    return TrackerPool(TrackingOptions(**kwargs), clock=Relogio())


def caminha(unidade: TrackerPool, camera_id: str, passos: int, *, inicio_seq: int = 1, x0=100.0):
    resultado = None
    for i in range(passos):
        resultado = unidade.update(
            frame(
                camera_id,
                (pessoa(x0 + 30 * i),),
                sequence=inicio_seq + i,
                at=i * PASSO_S,
            )
        )
    return resultado


# --- isolamento entre câmeras ---------------------------------------------------


def test_cada_camera_tem_o_proprio_tracker():
    """O §3.3 é explícito: o ID é local à câmera e não há re-identificação entre
    câmeras. Um tracker compartilhado associaria pessoas entre câmeras sem ninguém ter
    decidido isso — e o §9 exclui exatamente essa capacidade do v1."""
    unidade = pool()
    unidade.update(frame("cam1", (pessoa(100),), sequence=1, at=0.0))
    resultado = unidade.update(frame("cam2", (pessoa(500),), sequence=1, at=0.0))

    assert resultado.camera_id == "cam2"
    assert len(resultado.tracks) == 1
    assert resultado.tracks[0].camera_id == "cam2"


def test_pessoas_em_cameras_diferentes_nao_se_associam():
    """Duas pessoas na mesma posição em câmeras diferentes são duas pessoas. Se o pool
    compartilhasse estado, a segunda seria lida como continuação da primeira."""
    unidade = pool()
    for i in range(4):
        unidade.update(frame("cam1", (pessoa(100 + 30 * i),), sequence=i + 1, at=i * PASSO_S))
        unidade.update(frame("cam2", (pessoa(100 + 30 * i),), sequence=i + 1, at=i * PASSO_S))

    por_camera = {c.camera_id: c.criados for c in unidade.stats().cameras}
    assert por_camera == {"cam1": 1, "cam2": 1}


def test_register_faz_a_camera_aparecer_antes_do_primeiro_frame():
    unidade = pool()
    unidade.register("cam1")
    assert [c.camera_id for c in unidade.stats().cameras] == ["cam1"]
    assert unidade.stats().cameras[0].criados == 0


def test_camera_sem_tracks_devolve_vazio():
    assert pool().tracks("nunca-vista") == ()


# --- reconexão ------------------------------------------------------------------


def test_sequence_recuando_zera_os_tracks_da_camera():
    """`sequence` andando para trás significa que o ffmpeg daquela câmera reiniciou
    (§3.1). Deixar os tracks atravessarem seria pior que perdê-los: a câmera pode ter
    sido reposicionada, e um track com a posição da cena antiga associaria a primeira
    pessoa que aparecesse no lugar errado."""
    unidade = pool()
    caminha(unidade, "cam1", 5)
    assert len(unidade.tracks("cam1")) == 1

    depois = unidade.update(frame("cam1", (pessoa(400),), sequence=1, at=10.0))

    assert depois.tracks[0].track_id == 1, "o tracker recomeçou do zero"
    assert unidade.stats().cameras[0].resets == 1


def test_reconexao_nao_afeta_as_outras_cameras():
    unidade = pool()
    caminha(unidade, "cam1", 4)
    caminha(unidade, "cam2", 4)

    unidade.update(frame("cam1", (pessoa(400),), sequence=1, at=10.0))

    resets = {c.camera_id: c.resets for c in unidade.stats().cameras}
    assert resets == {"cam1": 1, "cam2": 0}


def test_sequence_avancando_com_buraco_nao_reseta():
    """Buraco na sequência é frame descartado pela fila do §3.2, não reconexão. Resetar
    aí jogaria fora todo track sempre que o box ficasse ocupado."""
    unidade = pool()
    unidade.update(frame("cam1", (pessoa(100),), sequence=1, at=0.0))
    unidade.update(frame("cam1", (pessoa(130),), sequence=2, at=PASSO_S))
    unidade.update(frame("cam1", (pessoa(190),), sequence=9, at=3 * PASSO_S))

    assert unidade.stats().cameras[0].resets == 0
    assert unidade.tracks("cam1")[0].track_id == 1


# --- telemetria -----------------------------------------------------------------


def test_stats_separam_o_ciclo_de_vida_por_camera():
    unidade = pool(min_hits=2)
    caminha(unidade, "cam1", 5)

    (camera,) = unidade.stats().cameras
    assert camera.ativos == 1
    assert camera.provisorios == 0
    assert camera.perdidos == 0
    assert camera.criados == 1


def test_track_perdido_aparece_separado_do_ativo():
    """O §3.4 precisa distinguir "a pessoa está ocluída" de "a pessoa não está lá", e o
    heartbeat também."""
    unidade = pool(max_perdido_s=5.0)
    caminha(unidade, "cam1", 4)
    unidade.update(frame("cam1", (), sequence=5, at=4 * PASSO_S))

    (camera,) = unidade.stats().cameras
    assert (camera.ativos, camera.perdidos) == (0, 1)


def test_criados_por_minuto_mede_a_fragmentacao():
    """**A métrica que mais importa deste módulo.** Numa loja de bairro, tracks criados
    por minuto deveria se parecer com o número de pessoas que entraram no quadro. Muito
    acima disso é o tracker quebrando uma pessoa em pedaços — o que antecede o falso
    positivo do R-2, porque um track que nasce já fora do caixa não tem histórico."""
    relogio = Relogio()
    unidade = TrackerPool(TrackingOptions(min_hits=1, max_perdido_s=0.0), clock=relogio)

    # Seis pessoas nascendo e sumindo, uma a cada 10 s: 6 por minuto.
    for i in range(6):
        instante = i * 10.0
        nasce = frame("cam1", (pessoa(100 + 200 * (i % 2)),), sequence=2 * i + 1, at=instante)
        unidade.update(nasce)
        unidade.update(frame("cam1", (), sequence=2 * i + 2, at=instante + 1.0))

    relogio.agora = 50.0
    assert unidade.stats().cameras[0].criados_por_minuto == pytest.approx(6.0, rel=0.05)


def test_criados_por_minuto_cai_quando_a_loja_esvazia():
    """Sem contar o tempo desde o último nascimento, uma câmera com movimento há uma hora
    continuaria reportando a taxa daquele momento — e a métrica mentiria justamente no
    período tranquilo, que é quando um pico chamaria atenção."""
    relogio = Relogio()
    unidade = TrackerPool(TrackingOptions(min_hits=1, max_perdido_s=0.0), clock=relogio)
    for i in range(4):
        unidade.update(frame("cam1", (pessoa(100),), sequence=2 * i + 1, at=i * 2.0))
        unidade.update(frame("cam1", (), sequence=2 * i + 2, at=i * 2.0 + 1.0))

    relogio.agora = 6.0
    movimentada = unidade.stats().cameras[0].criados_por_minuto

    relogio.agora = 3600.0
    assert unidade.stats().cameras[0].criados_por_minuto < movimentada / 100


def test_uma_criacao_so_nao_inventa_taxa():
    unidade = pool()
    caminha(unidade, "cam1", 3)
    assert unidade.stats().cameras[0].criados_por_minuto == 0.0


def test_vida_media_conta_os_tracks_encerrados():
    unidade = pool(min_hits=1, max_perdido_s=0.0)
    unidade.update(frame("cam1", (pessoa(100),), sequence=1, at=0.0))
    unidade.update(frame("cam1", (pessoa(130),), sequence=2, at=1.0))
    unidade.update(frame("cam1", (), sequence=3, at=2.0))

    assert unidade.stats().cameras[0].vida_media_s == pytest.approx(1.0)


def test_associacoes_baixa_medem_o_ganho_do_bytetrack():
    """Perto de zero significa que a segunda passada não está servindo para nada neste
    ambiente — e aí o piso do detector pode subir de volta."""
    unidade = pool(high_threshold=0.5)
    caminha(unidade, "cam1", 4)
    unidade.update(frame("cam1", (pessoa(220, score=0.25),), sequence=5, at=4 * PASSO_S))

    (camera,) = unidade.stats().cameras
    assert camera.associacoes_baixa == 1
    assert camera.associacoes_alta >= 3


def test_tracking_ms_e_medido():
    unidade = TrackerPool(TrackingOptions(), clock=Relogio(passo=0.002))
    resultado = unidade.update(frame("cam1", (pessoa(100),), sequence=1, at=0.0))
    assert resultado.tracking_ms == pytest.approx(2.0)


def test_stats_agregadas_somam_as_cameras():
    unidade = pool()
    caminha(unidade, "cam1", 3)
    caminha(unidade, "cam2", 3)

    stats = unidade.stats()
    assert stats.ativos == 2
    assert stats.criados == 2
