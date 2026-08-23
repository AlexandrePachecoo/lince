"""Fábricas de `AgentHealth` para os testes de heartbeat (§5.3).

Fora do `conftest.py` de propósito, no mesmo espírito de `eventos.py` e `regras.py`:
são construtores, não fixtures, e um teste que precisa de uma câmera com dois campos
diferentes do padrão fica legível chamando uma função em vez de compondo fixtures.
"""

from __future__ import annotations

from lince_agent.detect.state import CameraDetectionStats, DetectorInfo, DetectorStats
from lince_agent.ingest.state import CameraHealth, CameraStatus
from lince_agent.outbox.state import OutboxStats
from lince_agent.runtime import AgentHealth, ConfigHealth

VERSAO_MODELO = "yolox_s.onnx@0123456789ab"


def camera(
    camera_id: str = "cam1",
    *,
    status: CameraStatus = CameraStatus.OK,
    sampled_fps: float = 3.0,
    frames: int = 900,
    frames_dropped: int = 0,
    restarts: int = 0,
    last_frame_at: float | None = 1000.0,
) -> CameraHealth:
    return CameraHealth(
        camera_id=camera_id,
        status=status,
        consecutive_failures=0,
        restarts=restarts,
        sampled_fps=sampled_fps,
        frames=frames,
        frames_dropped=frames_dropped,
        fragments=30,
        fragments_dropped=0,
        fragment_bytes=1024,
        last_frame_at=last_frame_at,
    )


def deteccao(
    camera_id: str = "cam1",
    *,
    inference_fps: float = 2.9,
    dropped: int = 0,
) -> CameraDetectionStats:
    return CameraDetectionStats(
        camera_id=camera_id,
        frames_in=900,
        frames_inferred=870,
        dropped=dropped,
        detections=1200,
        inference_fps=inference_fps,
    )


def saude(
    *,
    cameras: tuple[CameraHealth, ...] = (),
    deteccoes: tuple[CameraDetectionStats, ...] | None = None,
    com_modelo: bool = True,
    config_version: str | None = "v7",
    outbox: OutboxStats | None = None,
    clip_disk_bytes: int = 0,
    uptime_s: float = 120.0,
) -> AgentHealth:
    if not cameras:
        cameras = (camera(),)
    if deteccoes is None:
        deteccoes = tuple(deteccao(c.camera_id) for c in cameras)
    info = (
        DetectorInfo(
            model_version=VERSAO_MODELO,
            input_size=640,
            provider="CPUExecutionProvider",
            classes=(0,),
        )
        if com_modelo
        else None
    )
    return AgentHealth(
        cameras=cameras,
        detector=DetectorStats(enabled=com_modelo, info=info, cameras=deteccoes),
        outbox=outbox or OutboxStats(),
        config=ConfigHealth(config_version=config_version),
        clip_disk_bytes=clip_disk_bytes,
        uptime_s=uptime_s,
    )
