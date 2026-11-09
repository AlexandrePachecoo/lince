"""Quem entra na pose e o que sai dela depois de uma reconexão.

O `PoseWorker` não olha `TrackStatus` e não sabe o que é reconexão — de
propósito. Quem decide é o adaptador, e os dois erros que ele existe para
evitar não dão exceção nenhuma:

- mandar track `PROVISORIO` ou `PERDIDO` para a pose. O provisório é a detecção
  espúria de um frame; o perdido é a caixa que o Kalman extrapolou, e recortar
  ali estima o pulso de uma pessoa que não está naquele lugar;
- entregar, depois de a câmera reconectar, a pose de um snapshot da sessão
  anterior. Os IDs de track recomeçam do 1 na reconexão: a junta do "track 1"
  antigo cairia no "track 1" novo, que é outra pessoa.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from deteccoes import frame as quadro

from lince_agent.pose.adaptador import PoseAdapter, caixas_para_pose
from lince_agent.pose.worker import CaixaPose, PessoaEstimada, ResultadoPose
from lince_agent.track.state import Track, TrackingResult, TrackStatus


def track(
    track_id: int,
    *,
    status: TrackStatus = TrackStatus.CONFIRMADO,
    desde_atualizacao_s: float = 0.0,
    x1: float = 10.0,
) -> Track:
    return Track(
        track_id=track_id,
        camera_id="cam1",
        status=status,
        x1=x1,
        y1=20.0,
        x2=x1 + 40.0,
        y2=200.0,
        score=0.9,
        hits=3,
        age_s=1.0,
        time_since_update_s=desde_atualizacao_s,
        first_seen_at=0.0,
        last_seen_at=1.0,
    )


def rastreio(
    sequence: int, *tracks: Track, camera_id: str = "cam1", received_at: float | None = None
) -> TrackingResult:
    """`received_at` igual ao do frame, como o tracker herda (§3.1)."""
    return TrackingResult(
        camera_id=camera_id,
        sequence=sequence,
        received_at=float(sequence) if received_at is None else received_at,
        tracks=tuple(replace(t, camera_id=camera_id) for t in tracks),
        tracking_ms=0.1,
    )


class WorkerFalso:
    """Registra o `submit` como o `PoseWorker` o receberia."""

    def __init__(self) -> None:
        self.pedidos: list[dict] = []

    def submit(self, camera_id, imagem, pessoas, *, received_at=None, sequence=None) -> None:
        self.pedidos.append(
            {
                "camera_id": camera_id,
                "imagem": imagem,
                "pessoas": tuple(pessoas),
                "received_at": received_at,
                "sequence": sequence,
            }
        )


def resultado(camera_id: str, sequence: int, *track_ids: int) -> ResultadoPose:
    return ResultadoPose(
        camera_id=camera_id,
        received_at=float(sequence),
        sequence=sequence,
        pessoas=tuple(
            PessoaEstimada(track_id=i, x1=0.0, y1=0.0, x2=1.0, y2=1.0, pose=None)  # type: ignore[arg-type]
            for i in track_ids
        ),
    )


# --- quem entra -----------------------------------------------------------------


def test_so_track_confirmado_e_atualizado_neste_frame_vai_para_a_pose():
    entrada = rastreio(
        5,
        track(1),
        track(2, status=TrackStatus.PROVISORIO),
        track(3, status=TrackStatus.PERDIDO, desde_atualizacao_s=0.66),
        track(4, desde_atualizacao_s=0.33),
        track(5, x1=300.0),
    )
    assert caixas_para_pose(entrada) == (
        CaixaPose(track_id=1, x1=10.0, y1=20.0, x2=50.0, y2=200.0),
        CaixaPose(track_id=5, x1=300.0, y1=20.0, x2=340.0, y2=200.0),
    )


def test_confirmado_sem_deteccao_neste_frame_fica_de_fora():
    """O motor de regras só amostra track com detecção nova (`time_since_update_s
    == 0`); a pose segue a mesma regra. Um confirmado que não foi visto neste
    frame tem caixa prevista, não observada."""
    assert caixas_para_pose(rastreio(1, track(1, desde_atualizacao_s=0.33))) == ()


def test_snapshot_leva_o_frame_a_camera_e_o_instante_do_tracking():
    worker = WorkerFalso()
    adaptador = PoseAdapter(worker)
    imagem = quadro(7, received_at=7.0)

    adaptador.submete(rastreio(7, track(1)), imagem)

    (pedido,) = worker.pedidos
    assert pedido["camera_id"] == "cam1"
    assert pedido["imagem"] is imagem
    assert pedido["sequence"] == 7
    assert pedido["received_at"] == 7.0
    assert [caixa.track_id for caixa in pedido["pessoas"]] == [1]


def test_frame_sem_ninguem_elegivel_nao_ocupa_a_fila():
    """A fila guarda o frame inteiro. Um snapshot vazio por frame empurraria para
    fora, na rajada, o snapshot que tinha gente."""
    worker = WorkerFalso()
    adaptador = PoseAdapter(worker)
    adaptador.submete(rastreio(1, track(1, status=TrackStatus.PROVISORIO)), quadro(1))
    adaptador.submete(rastreio(2), quadro(2))
    assert worker.pedidos == []
    assert adaptador.stats().sem_pessoa == 2


def test_frame_de_outro_instante_nao_e_associado_ao_tracking():
    """As caixas são do frame que o tracker viu. Recortar outro frame com elas
    poria o recorte em cima de outra cena — a junta sai plausível e errada."""
    worker = WorkerFalso()
    adaptador = PoseAdapter(worker)
    adaptador.submete(rastreio(8, track(1)), quadro(7))
    assert worker.pedidos == []
    assert adaptador.stats().desencontrados == 1


def test_ndarray_sem_sequencia_e_recusado():
    worker = WorkerFalso()
    adaptador = PoseAdapter(worker)
    adaptador.submete(rastreio(1, track(1)), np.zeros((4, 4, 3), dtype=np.uint8))
    assert worker.pedidos == []
    assert adaptador.stats().desencontrados == 1


# --- o que sai depois de uma reconexão ------------------------------------------


def test_pose_da_sessao_anterior_nao_chega_ao_consumidor():
    """Na reconexão o `Frame.sequence` recomeça e o tracker recria os IDs a partir
    do 1. Um snapshot que já estava na fila sai da inferência depois disso
    falando de um "track 1" que não existe mais."""
    entregues: list[ResultadoPose] = []
    adaptador = PoseAdapter(WorkerFalso(), on_pose=entregues.append)

    adaptador.submete(rastreio(40, track(1)), quadro(40))
    adaptador.submete(rastreio(1, track(1), received_at=50.0), quadro(1, received_at=50.0))

    velho = replace(resultado("cam1", 40, 1), received_at=40.0)
    novo = replace(resultado("cam1", 1, 1), received_at=50.0)
    adaptador.entrega(velho)
    adaptador.entrega(novo)

    assert entregues == [novo]
    assert adaptador.stats().reconexoes == 1
    assert adaptador.stats().descartados_reconexao == 1


def test_reconexao_de_uma_camera_nao_apaga_a_pose_da_outra():
    entregues: list[ResultadoPose] = []
    adaptador = PoseAdapter(WorkerFalso(), on_pose=entregues.append)

    adaptador.submete(rastreio(40, track(1), camera_id="cam1"), quadro(40))
    adaptador.submete(rastreio(30, track(1), camera_id="cam2"), quadro(30))
    adaptador.submete(
        rastreio(1, track(1), camera_id="cam1", received_at=50.0), quadro(1, received_at=50.0)
    )

    da_cam2 = replace(resultado("cam2", 30, 1), received_at=30.0)
    adaptador.entrega(da_cam2)
    assert entregues == [da_cam2]
    assert adaptador.stats().descartados_reconexao == 0


def test_mesma_sequencia_repetida_nao_e_reconexao():
    adaptador = PoseAdapter(WorkerFalso())
    adaptador.submete(rastreio(5, track(1)), quadro(5))
    adaptador.submete(rastreio(5, track(1)), quadro(5))
    assert adaptador.stats().reconexoes == 0


def test_reconexao_vista_mesmo_sem_ninguem_para_estimar():
    """A sessão nova pode começar com o corredor vazio. Se o corte só fosse
    marcado quando houvesse gente, a pose atrasada da sessão antiga passaria."""
    entregues: list[ResultadoPose] = []
    adaptador = PoseAdapter(WorkerFalso(), on_pose=entregues.append)
    adaptador.submete(rastreio(40, track(1)), quadro(40))
    adaptador.submete(rastreio(1, received_at=50.0), quadro(1, received_at=50.0))

    adaptador.entrega(replace(resultado("cam1", 40, 1), received_at=40.0))
    assert entregues == []
    assert adaptador.stats().reconexoes == 1


def test_sem_consumidor_o_resultado_e_so_contado():
    adaptador = PoseAdapter(WorkerFalso())
    adaptador.entrega(resultado("cam1", 1, 1))
    assert adaptador.stats().entregues == 1


def test_consumidor_que_falha_nao_some_com_o_erro():
    """O `PoseWorker` já conta erro de callback. O adaptador não engole: deixa
    subir para o worker contar e registrar."""

    def explode(_):
        raise RuntimeError("consumidor quebrado")

    adaptador = PoseAdapter(WorkerFalso(), on_pose=explode)
    with pytest.raises(RuntimeError, match="consumidor quebrado"):
        adaptador.entrega(resultado("cam1", 1, 1))
