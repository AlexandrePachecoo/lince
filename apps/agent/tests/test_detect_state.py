"""Os tipos que atravessam o estágio 2 e viram telemetria do §5.3."""

from __future__ import annotations

import pytest

from lince_agent.detect.state import CameraDetectionStats, Detection, DetectorStats


def caixa(x1: float = 100.0, y1: float = 50.0, x2: float = 140.0, y2: float = 250.0) -> Detection:
    return Detection(class_id=0, score=0.9, x1=x1, y1=y1, x2=x2, y2=y2)


def test_base_central_e_o_ponto_dos_pes():
    """O §3.4 elege a base central do bounding box como candidato natural para avaliar
    a travessia da linha de saída — a pessoa cruza a linha com os pés, não com o
    centro do corpo. Fica na caixa, e não no motor de regras, para que trocar o
    candidato depois do benchmark seja mudança de uma linha só."""
    assert caixa().base_central == (120.0, 250.0)


def test_largura_e_altura_saem_da_caixa():
    assert (caixa().width, caixa().height) == (40.0, 200.0)


def test_deteccao_e_imutavel():
    """A mesma detecção vai para o tracking e, depois, para as regras. Deixá-la
    mutável permitiria a um estágio corrigir a caixa do outro sem ninguém saber."""
    with pytest.raises(AttributeError):
        caixa().score = 0.1


def por_camera(camera_id: str, **kwargs) -> CameraDetectionStats:
    return CameraDetectionStats(camera_id=camera_id, **kwargs)


def test_totais_sao_derivados_das_cameras():
    """O agregado nunca é fonte: um box saturado costuma estar saturado por causa de
    **uma** câmera, e guardar só a soma apagaria justamente essa informação (R-4)."""
    stats = DetectorStats(
        cameras=(
            por_camera("saida", frames_in=90, frames_inferred=88, dropped=2, detections=41),
            por_camera("caixa", frames_in=90, frames_inferred=60, dropped=30, detections=12),
        )
    )
    assert stats.frames_in == 180
    assert stats.frames_inferred == 148
    assert stats.dropped == 32
    assert stats.detections == 53


def test_sem_cameras_os_totais_sao_zero():
    """É o estado do agente recém-subido, antes do primeiro frame — e o heartbeat sai
    nesse estado."""
    stats = DetectorStats()
    assert (stats.frames_in, stats.frames_inferred, stats.dropped, stats.detections) == (0, 0, 0, 0)
    assert stats.enabled is False
    assert stats.info is None
