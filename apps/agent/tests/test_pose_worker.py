"""A thread da pose e a fronteira com detecção e tracking.

O que este arquivo guarda não é o RTMPose — é a promessa de que a sessão não
roda na thread que já inferiu e rastreou, de que um snapshot aceito não muda
quando o chamador reutiliza o buffer, e de que parar não deixa thread nem
callback para trás. GPU, peso e RTSP ficam de fora: a inferência é um dublê.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence

import numpy as np
import pytest

from lince_agent.config import PixelFormat
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.onnx import NUM_JUNTAS, Junta, PosePessoa
from lince_agent.pose.worker import CaixaPose, PoseWorker, PoseWorkerStats, ResultadoPose

LARGURA, ALTURA = 8, 6
SENHA = "s3nha-super-secreta"
TOKEN = "tok-secreto"
URL_COM_SEGREDO = f"rtsp://operador:{SENHA}@cam.loja:554/substream?token={TOKEN}"


def quadro(
    sequence: int,
    received_at: float,
    pixel: tuple[int, int, int] = (1, 2, 3),
) -> Frame:
    imagem = np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)
    imagem[0, 0] = pixel
    return Frame(
        data=imagem.tobytes(),
        width=LARGURA,
        height=ALTURA,
        pixel_format=PixelFormat.BGR24,
        sequence=sequence,
        received_at=received_at,
    )


def pose_da_caixa(xyxy: tuple[float, ...]) -> PosePessoa:
    return PosePessoa(
        tuple(
            Junta(indice=indice, x=float(xyxy[0]), y=float(xyxy[1]), confianca=1.0)
            for indice in range(NUM_JUNTAS)
        )
    )


class PoseFalsa:
    """Devolve uma pose por caixa e guarda a imagem que viu, sem sessão."""

    def __init__(self, *, falha: BaseException | None = None, quantas: int | None = None) -> None:
        self.imagens: list[object] = []
        self.caixas: list[list[tuple[float, float, float, float]]] = []
        self.fechamentos = 0
        self._falha = falha
        self._quantas = quantas

    def estimar(self, imagem: object, caixas: Sequence[Sequence[float]]) -> tuple[PosePessoa, ...]:
        self.imagens.append(imagem)
        copiadas = [tuple(float(valor) for valor in caixa) for caixa in caixas]
        self.caixas.append(copiadas)
        if self._falha is not None:
            erro = self._falha
            self._falha = None
            raise erro
        if self._quantas is not None:
            return tuple(pose_da_caixa((0.0, 0.0, 0.0, 0.0)) for _ in range(self._quantas))
        return tuple(pose_da_caixa(caixa) for caixa in copiadas)

    def close(self) -> None:
        self.fechamentos += 1


class PoseTravada:
    """Fica presa dentro de `estimar` até alguém soltar.

    É a GPU saturada sem `sleep`: a thread entra na inferência e não sai, e o
    teste enche a fila com a certeza de que ninguém a está drenando.
    """

    def __init__(self) -> None:
        self.entrou = threading.Event()
        self.solta = threading.Event()
        self.chamadas = 0
        self.fechamentos = 0

    def estimar(self, imagem: object, caixas: Sequence[Sequence[float]]) -> tuple[PosePessoa, ...]:
        self.chamadas += 1
        self.entrou.set()
        assert self.solta.wait(timeout=5)
        return tuple(pose_da_caixa(tuple(float(valor) for valor in caixa)) for caixa in caixas)

    def close(self) -> None:
        self.fechamentos += 1
        self.solta.set()


def worker(
    pose: PoseFalsa | PoseTravada | None = None,
    *,
    queue_size: int = 4,
    on_result=None,
) -> PoseWorker:
    return PoseWorker(pose or PoseFalsa(), queue_size=queue_size, on_result=on_result)


def assert_particao(stats: PoseWorkerStats) -> None:
    """Um snapshot aceito está num termo só. Os dois lados têm que ser o mesmo número."""
    assert stats.queue_depth == stats.na_fila
    assert stats.aceitos == (
        stats.processados + stats.descartados + stats.erros + stats.em_curso + stats.na_fila
    )


def envia(
    unidade: PoseWorker,
    camera_id: str,
    sequence: int,
    received_at: float,
    pessoas: list[tuple[int, tuple[float, float, float, float]]] | None = None,
) -> None:
    if pessoas is None:
        pessoas = [(sequence, (1.0, 2.0, 3.0, 4.0))]
    unidade.submit(
        camera_id,
        quadro(sequence, received_at),
        pessoas,
        received_at=received_at,
    )


# --- a fronteira com quem chama -------------------------------------------------


def test_submit_nao_bloqueia_com_a_inferencia_travada():
    """A thread de tracking é quem chama `submit`. Se ela esperar a sessão, o
    detector para junto — e o pipe do ffmpeg enche atrás dos dois."""
    travada = PoseTravada()
    unidade = worker(travada, queue_size=2)
    unidade.start()
    try:
        envia(unidade, "cam1", 1, 1.0)
        assert travada.entrou.wait(timeout=5)

        for sequencia in range(2, 10):
            envia(unidade, "cam1", sequencia, float(sequencia))

        assert unidade.stats().queue_depth == 2
        assert unidade.stats().descartados == 6
        assert unidade.stats().aceitos == 9
    finally:
        travada.solta.set()
        unidade.stop(timeout=5)


def test_descarta_o_snapshot_mais_antigo():
    """Com a pose atrasada, o frame velho não vale o recorte: o instante do
    snapshot é o que o gesto vai usar, e trabalhar o antigo só aumenta a
    latência."""
    pose = PoseFalsa()
    unidade = worker(pose, queue_size=2)
    for sequencia in (1, 2, 3):
        envia(unidade, "cam1", sequencia, float(sequencia))

    assert unidade.tick() and unidade.tick()
    assert unidade.tick() is False
    assert [imagem.sequence for imagem in pose.imagens] == [2, 3]  # type: ignore[attr-defined]
    assert unidade.stats().descartados == 1
    assert unidade.stats().processados == 2


def test_descarte_e_cobrado_da_camera_que_o_perdeu():
    """A fila é uma só. O descarte tem que dizer qual câmera perdeu o
    snapshot, senão a saturada some no agregado."""
    unidade = worker(queue_size=1)
    envia(unidade, "cam1", 1, 1.0)
    envia(unidade, "cam2", 1, 1.0)

    por_camera = {camera.camera_id: camera.descartados for camera in unidade.stats().cameras}
    assert por_camera == {"cam1": 1, "cam2": 0}


def test_fila_vazia_nao_da_trabalho():
    assert worker().tick() is False


def test_imagem_invalida_nao_entra_na_fila():
    unidade = worker()
    with pytest.raises(ValueError, match="BGR"):
        unidade.submit("cam", np.zeros((4, 4), dtype=np.uint8), [], received_at=1.0)
    assert unidade.tick() is False
    assert unidade.stats().aceitos == 0


# --- associação -----------------------------------------------------------------


def test_ordem_e_associacao_por_caixa_camera_e_instante():
    """Várias pessoas e câmeras na mesma fila. Trocar a junta de track, ou
    carimbar o instante da inferência, entrega o gesto na pessoa errada ou
    no relógio da GPU."""
    recebidos: list[ResultadoPose] = []
    unidade = worker(on_result=recebidos.append)
    unidade.submit(
        "cam-a",
        quadro(4, received_at=10.0),
        [(10, (1.0, 2.0, 11.0, 22.0)), (11, (30.0, 40.0, 50.0, 60.0))],
        received_at=3.5,
        sequence=4,
    )
    unidade.submit(
        "cam-b",
        quadro(9, received_at=99.0),
        [(7, (8.0, 9.0, 10.0, 11.0))],
        received_at=4.25,
    )
    assert unidade.tick() and unidade.tick()

    primeiro, segundo = recebidos
    assert primeiro.camera_id == "cam-a"
    assert primeiro.received_at == 3.5
    assert primeiro.sequence == 4
    assert [(pessoa.track_id, pessoa.x1, pessoa.y1) for pessoa in primeiro.pessoas] == [
        (10, 1.0, 2.0),
        (11, 30.0, 40.0),
    ]
    assert primeiro.pessoas[0].pose.juntas[0].x == 1.0
    assert primeiro.pessoas[1].pose.juntas[0].x == 30.0
    assert segundo.camera_id == "cam-b"
    assert segundo.received_at == 4.25
    assert segundo.sequence == 9
    assert segundo.pessoas[0].track_id == 7
    assert segundo.pessoas[0].pose.juntas[0].x == 8.0


def test_lista_vazia_entrega_resultado_e_a_seguinte_permanece():
    """Frame sem caixa ainda é um instante. Engoli-lo, ou inferir pessoa
    nenhuma como se fosse uma, desloca o snapshot de quem veio depois."""
    pose = PoseFalsa()
    recebidos: list[ResultadoPose] = []
    unidade = worker(pose, on_result=recebidos.append)
    unidade.submit("cam", quadro(1, 1.0), [], received_at=1.0)
    envia(unidade, "cam", 2, 2.0, [(5, (4.0, 5.0, 6.0, 7.0))])

    assert unidade.tick() and unidade.tick()
    assert recebidos[0].pessoas == ()
    assert recebidos[0].received_at == 1.0
    assert recebidos[1].pessoas[0].track_id == 5
    assert pose.caixas == [[], [(4.0, 5.0, 6.0, 7.0)]]
    assert unidade.stats().processados == 2


def test_um_frame_para_todas_as_pessoas_e_o_ndarray_e_copiado():
    """O box não tem RAM para um frame por pessoa. O ndarray, ao contrário
    do `Frame`, é reescrito pelo chamador — sem uma cópia, o recorte sai do
    frame seguinte."""
    pose = PoseFalsa()
    unidade = worker(pose)
    imagem = np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)
    imagem[0, 0] = (4, 5, 6)
    caixa = np.array([10.0, 20.0, 30.0, 40.0])
    outra = [1.0, 2.0, 3.0, 4.0]
    unidade.submit("cam", imagem, [(1, caixa), (2, outra)], received_at=8.0, sequence=3)
    imagem[0, 0] = (0, 0, 0)
    caixa[:] = -1
    outra[0] = 99.0

    assert unidade.tick()
    assert len(pose.caixas) == 1
    assert pose.caixas[0] == [(10.0, 20.0, 30.0, 40.0), (1.0, 2.0, 3.0, 4.0)]
    vista = pose.imagens[0]
    assert isinstance(vista, np.ndarray)
    assert vista is not imagem
    assert tuple(int(valor) for valor in vista[0, 0]) == (4, 5, 6)


def test_frame_de_bytes_nao_e_copiado_e_a_caixa_e():
    """O `bytes` do pipe não muda. Copiá-lo de novo seria outro frame inteiro
    na fila, e as duas pessoas têm que ver o mesmo objeto."""
    pose = PoseFalsa()
    unidade = worker(pose)
    frame = quadro(6, 2.0, pixel=(9, 8, 7))
    caixa = [1.0, 2.0, 3.0, 4.0]
    unidade.submit(
        "cam",
        frame,
        [(3, caixa), CaixaPose(4, 5.0, 6.0, 7.0, 8.0)],
        received_at=2.5,
    )
    caixa[0] = 0.0

    assert unidade.tick()
    assert pose.imagens[0] is frame
    assert pose.caixas[0][0] == (1.0, 2.0, 3.0, 4.0)
    assert pose.caixas[0][1] == (5.0, 6.0, 7.0, 8.0)


def test_bytearray_no_frame_e_copiado_antes_de_voltar():
    """Quem pôs um buffer mutável no `Frame` ainda pode escrever nele depois
    do `submit`. O snapshot já aceito não pode acompanhar."""
    pose = PoseFalsa()
    unidade = worker(pose)
    bruto = bytearray(np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8).tobytes())
    bruto[0:3] = bytes((4, 5, 6))
    frame = Frame(
        data=bruto,  # type: ignore[arg-type]
        width=LARGURA,
        height=ALTURA,
        pixel_format=PixelFormat.BGR24,
        sequence=1,
        received_at=1.0,
    )
    unidade.submit("cam", frame, [(1, (1.0, 1.0, 2.0, 2.0))], received_at=1.0)
    bruto[0:3] = bytes((0, 0, 0))

    assert unidade.tick()
    guardado = pose.imagens[0]
    assert isinstance(guardado, Frame)
    assert guardado.data is not bruto
    assert guardado.data[0:3] == bytes((4, 5, 6))


# --- falha não apaga a fila -----------------------------------------------------


def test_excecao_na_inferencia_nao_para_o_proximo_nem_vaza_segredo(caplog):
    """A thread é uma só. Morrer num frame, ou gravar a URL da câmera no log,
    ou para a loja ou deixa a senha do RTSP no disco."""
    pose = PoseFalsa(falha=RuntimeError(f"sessão caiu em {URL_COM_SEGREDO}"))
    recebidos: list[ResultadoPose] = []
    unidade = worker(pose, on_result=recebidos.append)
    envia(unidade, "cam1", 1, 1.0)
    envia(unidade, "cam2", 2, 2.0)

    with caplog.at_level(logging.ERROR, logger="lince_agent.pose.worker"):
        assert unidade.tick() and unidade.tick()

    assert recebidos[0].camera_id == "cam2"
    assert unidade.stats().erros == 1
    assert unidade.stats().processados == 1
    assert {camera.camera_id: camera.erros for camera in unidade.stats().cameras} == {
        "cam1": 1,
        "cam2": 0,
    }
    assert SENHA not in caplog.text
    assert TOKEN not in caplog.text
    assert "operador" not in caplog.text
    assert "cam.loja:554/substream" in caplog.text


def test_consumidor_que_falha_nao_derruba_o_proximo_nem_vaza_segredo(caplog):
    """O callback é o adaptador. A exceção dele não pode engolir o snapshot
    seguinte nem repetir um segredo que a mensagem trouxe."""
    recebidos: list[ResultadoPose] = []

    def on_result(resultado: ResultadoPose) -> None:
        if resultado.sequence == 1:
            raise RuntimeError(f"adaptador recusou {resultado.camera_id} via {URL_COM_SEGREDO}")
        recebidos.append(resultado)

    unidade = worker(on_result=on_result)
    envia(unidade, "cam1", 1, 1.0)
    envia(unidade, "cam2", 2, 2.0)
    with caplog.at_level(logging.ERROR, logger="lince_agent.pose.worker"):
        assert unidade.tick() and unidade.tick()

    stats = unidade.stats()
    assert [resultado.camera_id for resultado in recebidos] == ["cam2"]
    # O primeiro snapshot foi inferido e o callback rejeitou. Está em `erros`,
    # não em `processados`: os dois juntos contariam o mesmo frame duas vezes.
    assert stats.aceitos == 2
    assert stats.erros == 1
    assert stats.processados == 1
    assert (
        stats.aceitos
        == stats.processados + stats.descartados + stats.erros + stats.em_curso + stats.na_fila
    )
    assert SENHA not in caplog.text
    assert TOKEN not in caplog.text
    assert "operador" not in caplog.text


def test_resultado_com_tamanho_errado_nao_e_entregue():
    """Uma pose a mais ou a menos cola a junta no track vizinho. Sem entrega
    o contador sobe; entrega trocada não deixa rastro."""
    recebidos: list[ResultadoPose] = []
    unidade = worker(PoseFalsa(quantas=2), on_result=recebidos.append)
    envia(unidade, "cam", 1, 1.0, [(1, (1.0, 2.0, 3.0, 4.0))])
    envia(unidade, "cam", 2, 2.0, [(2, (5.0, 6.0, 7.0, 8.0))])

    assert unidade.tick() and unidade.tick()
    assert recebidos == []
    assert unidade.stats().erros == 2
    assert unidade.stats().processados == 0


# --- ciclo de vida --------------------------------------------------------------


def test_start_repetido_nao_abre_outra_thread():
    """Duas threads na mesma sessão ONNX são inferência concorrente. A
    segunda subida tem que ser o mesmo worker, não um erro nem uma thread
    nova."""
    unidade = worker()
    unidade.start()
    try:
        unidade.start()
        vivas = [
            thread
            for thread in threading.enumerate()
            if thread.name == "pose" and thread.is_alive()
        ]
        assert len(vivas) == 1
    finally:
        unidade.stop(timeout=5)


def test_stop_e_idempotente_fecha_a_sessao_e_nao_deixa_thread():
    pose = PoseFalsa()
    unidade = worker(pose)
    unidade.start()
    unidade.stop(timeout=5)
    unidade.stop(timeout=5)
    assert pose.fechamentos == 1
    assert not any(thread.name == "pose" and thread.is_alive() for thread in threading.enumerate())
    with pytest.raises(RuntimeError, match="já foi encerrado"):
        unidade.start()


def test_stop_sem_start_tambem_fecha_e_pode_repetir():
    """O runtime pode desistir antes de subir a thread. A sessão não pode
    ficar aberta por isso, e o segundo `stop` não pode fechá-la de novo."""
    pose = PoseFalsa()
    unidade = worker(pose)
    unidade.stop()
    unidade.stop()
    assert pose.fechamentos == 1


def test_stop_descarta_o_que_ainda_nao_inferiu_sem_callback():
    """O que está só na fila não é evento. Inferir na parada alonga o
    encerramento pelo tamanho da fila; entregar depois que `stop` voltou
    chama o adaptador que já se desligou."""
    pose = PoseFalsa()
    recebidos: list[ResultadoPose] = []
    unidade = worker(pose, on_result=recebidos.append, queue_size=4)
    envia(unidade, "cam", 1, 1.0)
    envia(unidade, "cam", 2, 2.0)
    unidade.stop()
    assert recebidos == []
    assert pose.imagens == []
    assert unidade.stats().descartados == 2
    assert unidade.stats().processados == 0
    assert unidade.tick() is False


def test_stop_no_callback_nao_trava_nem_entrega_o_seguinte():
    """`stop` chamado de dentro do callback não pode dar join na própria
    thread, e o snapshot que ainda estava na fila não pode sair depois."""
    travada = PoseTravada()
    recebidos: list[ResultadoPose] = []
    terminou = threading.Event()
    caixa: dict[str, PoseWorker] = {}

    def on_result(resultado: ResultadoPose) -> None:
        recebidos.append(resultado)
        caixa["worker"].stop(timeout=5)
        terminou.set()

    unidade = worker(travada, on_result=on_result, queue_size=4)
    caixa["worker"] = unidade
    unidade.start()
    try:
        envia(unidade, "cam", 1, 1.0)
        assert travada.entrou.wait(timeout=5)
        envia(unidade, "cam", 2, 2.0)
        envia(unidade, "cam", 3, 3.0)
        travada.solta.set()
        assert terminou.wait(timeout=5)
        unidade.stop(timeout=5)
        assert [resultado.sequence for resultado in recebidos] == [1]
        assert travada.chamadas == 1
        assert unidade.stats().processados == 1
        assert unidade.stats().descartados == 2
        assert unidade.stats().aceitos == 3
        assert not any(
            thread.name == "pose" and thread.is_alive() for thread in threading.enumerate()
        )
    finally:
        travada.solta.set()
        unidade.stop(timeout=5)


class _PosePresa:
    """Trava em `estimar` e não solta no `close`.

    Se `close` destravasse, um `stop` que fechou cedo esconderia a inferência
    ainda em curso — que é a corrida que este dublê existe para deixar visível.
    """

    def __init__(self) -> None:
        self.entrou = threading.Event()
        self.solta = threading.Event()
        self.chamadas = 0
        self.fechamentos = 0
        self.lock = threading.Lock()
        self.max_dentro = 0
        self._dentro = 0

    def estimar(self, imagem: object, caixas: Sequence[Sequence[float]]) -> tuple[PosePessoa, ...]:
        with self.lock:
            self._dentro += 1
            self.max_dentro = max(self.max_dentro, self._dentro)
        self.chamadas += 1
        self.entrou.set()
        assert self.solta.wait(timeout=5)
        with self.lock:
            self._dentro -= 1
        return tuple(pose_da_caixa(tuple(float(valor) for valor in caixa)) for caixa in caixas)

    def close(self) -> None:
        self.fechamentos += 1


def test_tick_em_curso_recusa_a_thread():
    """`tick` lia a thread e soltava o lock antes de `estimar`. `start` nesse
    intervalo subia a thread, e as duas chamavam a sessão juntas."""
    pose = _PosePresa()
    unidade = PoseWorker(pose, queue_size=4)
    envia(unidade, "cam", 1, 1.0)
    envia(unidade, "cam", 2, 2.0)
    encontro = threading.Barrier(2)
    tick = threading.Thread(target=_tick_ate_o_encontro, args=(unidade, pose, encontro))
    tick.start()
    try:
        encontro.wait(timeout=5)
        with pytest.raises(RuntimeError, match="tick em curso"):
            unidade.start()
        assert pose.max_dentro == 1
    finally:
        pose.solta.set()
        tick.join(timeout=5)
        unidade.stop(timeout=5)


def _tick_ate_o_encontro(
    unidade: PoseWorker, pose: _PosePresa, encontro: threading.Barrier
) -> None:
    def estimar(imagem: object, caixas: Sequence[Sequence[float]]) -> tuple[PosePessoa, ...]:
        pose.chamadas += 1
        with pose.lock:
            pose._dentro += 1
            pose.max_dentro = max(pose.max_dentro, pose._dentro)
        encontro.wait(timeout=5)
        assert pose.solta.wait(timeout=5)
        with pose.lock:
            pose._dentro -= 1
        return tuple(pose_da_caixa(tuple(float(valor) for valor in caixa)) for caixa in caixas)

    pose.estimar = estimar  # type: ignore[method-assign]
    unidade.tick()


def test_stop_durante_o_tick_nao_fecha_nem_entrega_depois():
    """`stop` juntava a thread de trabalho e, sem ela, fechava a sessão enquanto
    o `tick` ainda estava em `estimar`. O callback saía depois do retorno."""
    pose = _PosePresa()
    recebidos: list[ResultadoPose] = []
    entregue = threading.Event()

    def on_result(resultado: ResultadoPose) -> None:
        recebidos.append(resultado)
        entregue.set()

    unidade = PoseWorker(pose, on_result=on_result)
    envia(unidade, "cam", 1, 1.0)
    tick = threading.Thread(target=unidade.tick)
    tick.start()
    try:
        assert pose.entrou.wait(timeout=5)
        with pytest.raises(TimeoutError, match="sessão aberta"):
            unidade.stop(timeout=0)
        assert pose.fechamentos == 0
        assert tick.is_alive()
        assert recebidos == []
        pose.solta.set()
        assert entregue.wait(timeout=5)
        assert [resultado.sequence for resultado in recebidos] == [1]
        assert pose.fechamentos == 0
        unidade.stop(timeout=5)
        assert pose.fechamentos == 1
        envia(unidade, "cam", 2, 2.0)
        assert [resultado.sequence for resultado in recebidos] == [1]
        assert pose.chamadas == 1
    finally:
        pose.solta.set()
        tick.join(timeout=5)
        unidade.stop(timeout=5)


def test_stop_que_expira_nao_se_confunde_com_encerramento():
    """Timeout esgotado devolvia `None`, igual ao `stop` que fechou. A sessão
    tem que continuar com a inferência que não voltou, e o chamador tem que
    poder distinguir os dois desfechos."""
    pose = _PosePresa()
    recebidos: list[ResultadoPose] = []
    entregue = threading.Event()

    def on_result(resultado: ResultadoPose) -> None:
        recebidos.append(resultado)
        entregue.set()

    unidade = PoseWorker(pose, on_result=on_result)
    unidade.start()
    try:
        envia(unidade, "cam", 1, 1.0)
        assert pose.entrou.wait(timeout=5)
        with pytest.raises(TimeoutError, match="sessão aberta"):
            unidade.stop(timeout=0)
        assert pose.fechamentos == 0
        assert any(thread.name == "pose" and thread.is_alive() for thread in threading.enumerate())
        pose.solta.set()
        assert entregue.wait(timeout=5)
        assert pose.fechamentos == 0
        unidade.stop(timeout=5)
        assert pose.fechamentos == 1
        assert [resultado.sequence for resultado in recebidos] == [1]
        assert not any(
            thread.name == "pose" and thread.is_alive() for thread in threading.enumerate()
        )
        envia(unidade, "cam", 2, 2.0)
        assert pose.chamadas == 1
    finally:
        pose.solta.set()
        unidade.stop(timeout=5)


def test_stop_imediato_sem_inferencia_fecha():
    """Timeout zero com ninguém em `estimar` é encerramento de verdade: a
    sessão fecha e não há exceção para confundir com a pose ainda ocupada."""
    pose = PoseFalsa()
    unidade = PoseWorker(pose)
    unidade.stop(timeout=0)
    assert pose.fechamentos == 1
    unidade.stop(timeout=0)
    assert pose.fechamentos == 1


def test_submit_concorrente_com_stop_volta_e_nao_infere_depois():
    """`submit` não pode esperar a inferência nem a parada. O que entra
    enquanto `stop` já começou não vira callback depois que `stop` volta."""
    pose = _PosePresa()
    recebidos: list[ResultadoPose] = []
    unidade = PoseWorker(pose, on_result=recebidos.append, queue_size=8)
    unidade.start()
    encontro = threading.Barrier(2)
    submeteu = threading.Event()
    parou = threading.Event()

    def parar() -> None:
        encontro.wait(timeout=5)
        unidade.stop(timeout=5)
        parou.set()

    def submeter() -> None:
        encontro.wait(timeout=5)
        for sequencia in range(10, 18):
            envia(unidade, "cam", sequencia, float(sequencia))
        submeteu.set()

    try:
        envia(unidade, "cam", 1, 1.0)
        assert pose.entrou.wait(timeout=5)
        threading.Thread(target=parar).start()
        threading.Thread(target=submeter).start()
        assert submeteu.wait(timeout=5)
        # O flag sobe antes do join: é a janela em que o submit tem de voltar
        # e o que ainda está só na fila não pode começar a inferir.
        assert unidade._encerrar.wait(timeout=5)
        assert not parou.is_set()
        pose.solta.set()
        assert parou.wait(timeout=5)
        assert [resultado.sequence for resultado in recebidos] == [1]
        assert pose.chamadas == 1
        assert_particao(unidade.stats())
        envia(unidade, "cam", 99, 99.0)
        assert [resultado.sequence for resultado in recebidos] == [1]
        assert pose.chamadas == 1
        assert unidade.stats().aceitos == unidade.stats().processados + unidade.stats().descartados
    finally:
        pose.solta.set()
        unidade.stop(timeout=5)


def test_processados_so_sobe_depois_que_o_callback_volta():
    """Antes, `processados` subia antes do callback. Quem lesse a conta ali via
    entrega que ainda podia falhar — e, se falhasse, o mesmo snapshot também
    ia para `erros`."""
    visto: dict[str, PoseWorkerStats] = {}

    def on_result(resultado: ResultadoPose) -> None:
        visto["durante"] = unidade.stats()
        assert resultado.sequence == 1

    unidade = PoseWorker(PoseFalsa(), on_result=on_result)
    envia(unidade, "cam", 1, 1.0)
    assert unidade.tick()
    durante = visto["durante"]
    assert durante.aceitos == 1
    assert durante.processados == 0
    assert durante.em_curso == 1
    assert durante.erros == 0
    assert_particao(durante)
    depois = unidade.stats()
    assert depois.processados == 1
    assert depois.em_curso == 0
    assert_particao(depois)


def test_conta_fecha_em_curso_no_descarte_e_na_fila_recusada():
    """No meio da inferência a conta já fecha: o que está em `estimar` não
    falta em `aceitos`, o drop não fica invisível e o `submit` com a fila
    fechada não entra."""
    pose = _PosePresa()
    unidade = PoseWorker(pose, queue_size=1)
    unidade.start()
    try:
        envia(unidade, "cam", 1, 1.0)
        assert pose.entrou.wait(timeout=5)
        em_curso = unidade.stats()
        assert em_curso.aceitos == 1
        assert em_curso.em_curso == 1
        assert em_curso.processados == 0
        assert em_curso.na_fila == 0
        assert_particao(em_curso)
        envia(unidade, "cam", 2, 2.0)
        envia(unidade, "cam", 3, 3.0)
        cheia = unidade.stats()
        assert cheia.aceitos == 3
        assert cheia.descartados == 1
        assert cheia.em_curso == 1
        assert cheia.na_fila == 1
        assert_particao(cheia)
        with pytest.raises(TimeoutError, match="sessão aberta"):
            unidade.stop(timeout=0)
        assert pose.fechamentos == 0
        aceitos = unidade.stats().aceitos
        envia(unidade, "cam", 4, 4.0)
        recusada = unidade.stats()
        assert recusada.aceitos == aceitos
        assert recusada.em_curso == 1
        assert_particao(recusada)
    finally:
        pose.solta.set()
        unidade.stop(timeout=5)


def test_received_at_do_frame_e_track_id_integral():
    """O instante do frame não precisa ser repetido. `np.int64` é o id que o
    tracker entrega; bool não é track."""
    recebidos: list[ResultadoPose] = []
    unidade = PoseWorker(PoseFalsa(), on_result=recebidos.append)
    frame = quadro(4, 3.5)
    unidade.submit("cam", frame, [(np.int64(8), (1.0, 2.0, 3.0, 4.0))])
    assert unidade.tick()
    assert recebidos[0].received_at == 3.5
    assert recebidos[0].pessoas[0].track_id == 8
    with pytest.raises(ValueError, match="não bool"):
        unidade.submit("cam", frame, [(True, (1.0, 2.0, 3.0, 4.0))])
    with pytest.raises(ValueError, match="não finita"):
        unidade.submit("cam", frame, [(1, (1.0, float("nan"), 3.0, 4.0))])
    with pytest.raises(ValueError, match="received_at"):
        unidade.submit("cam", np.zeros((2, 2, 3), dtype=np.uint8), [])


def test_submit_depois_do_stop_nao_chama_de_volta():
    recebidos: list[ResultadoPose] = []
    pronto = threading.Event()

    def on_result(resultado: ResultadoPose) -> None:
        recebidos.append(resultado)
        pronto.set()

    unidade = worker(on_result=on_result)
    unidade.start()
    try:
        envia(unidade, "cam", 1, 1.0)
        assert pronto.wait(timeout=5)
        unidade.stop(timeout=5)
        envia(unidade, "cam", 2, 2.0)
        assert [resultado.sequence for resultado in recebidos] == [1]
        assert unidade.stats().aceitos == 1
        assert not any(
            thread.name == "pose" and thread.is_alive() for thread in threading.enumerate()
        )
    finally:
        unidade.stop(timeout=5)
