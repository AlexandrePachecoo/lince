"""A thread da pose, fora da detecção e do tracking.

O frame e as caixas nascem na thread que já rodou YOLOX e ByteTrack. Essa thread
não pode esperar o RTMPose: a sessão é uma só para todas as câmeras, e uma
inferência lenta segura o mesmo turno que o `DetectorWorker` usa para não
encher o pipe do ffmpeg. `submit` só copia o que é mutável, empurra e volta.
A fila é a `DropOldestQueue` do estágio 1 — quando enche, o snapshot mais
antigo sai, que é o mesmo *drop oldest* do §3.2.

**Uma thread, uma sessão.** `OnnxPose` não é seguro para duas chamadas ao
mesmo tempo. A segunda `start` não abre outra thread.

O que esta fila aceita é um snapshot: o frame, as caixas já com `track_id`,
o `camera_id` e o `received_at` da borda. O resultado devolve cada junta na
caixa que a originou, com o mesmo instante. Várias pessoas dividem o frame;
câmeras diferentes não dividem snapshot. Quem escolhe *quais* tracks entram
— só confirmado, só atualizado neste frame — é o adaptador no runtime, que
ainda não existe. Este módulo não olha `TrackStatus` e não liga gesto,
overlay, outbox nem config.

**Propriedade.** O `Frame` do ffmpeg carrega `bytes`, que não dá para
alterar; o snapshot fica com esse objeto e não aloca outro por pessoa. Um
`ndarray` é buffer reutilizado pelo chamador, então vira uma cópia só, e
todas as caixas daquele snapshot leem essa cópia. Os quatro números da caixa
são copiados em `submit`: escrever no array do chamador depois não move o
recorte. A profundidade da fila é o teto de frames retidos.

**Parada.** `stop` é idempotente e terminal. A inferência já iniciada
termina e o callback dela ainda pode rodar antes de `stop` voltar. O que
estava só na fila é descartado sem callback — não é evento persistido, e
drenar `queue_size` inferências tornaria o encerramento tão longo quanto a
GPU. Depois que `stop` volta, a thread não está viva e o callback não é
chamado, nem por um `submit` atrasado. A sessão é fechada ali; para rodar de
novo, constrói-se outro worker.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from lince_agent.ffmpeg.queues import DropOldestQueue
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.onnx import PosePessoa
from lince_agent.redact import redige_texto

log = logging.getLogger(__name__)

_ESPERA_S = 0.25
"""Quanto a thread espera por um snapshot antes de rever se foi mandada parar.
Só afeta a latência do `stop` se o `close` da fila não acordar ninguém."""

_FILA_PADRAO = 2
"""Teto de snapshots à espera. A pose guarda o frame inteiro, não o recorte, e
o box já retém a fila da detecção. Dois é folga de um instante sem virar
arquivo de vídeo; o adaptador passa outro número quando souber o orçamento."""


class Pose(Protocol):
    """O que o worker chama. `OnnxPose` cumpre isto; o teste também, sem peso."""

    def estimar(
        self,
        imagem: np.ndarray | Frame,
        caixas: Sequence[object],
    ) -> tuple[PosePessoa, ...]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CaixaPose:
    """Uma pessoa no snapshot, no espaço do frame da câmera.

    Os números são valor, não vista. É o que impede o tracker de mover a caixa
    no frame seguinte e mudar um recorte que já foi aceito.
    """

    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def xyxy(self) -> tuple[float, float, float, float]:
        return (self.x1, self.y1, self.x2, self.y2)


@dataclass(frozen=True, slots=True)
class PessoaEstimada:
    """A pose de uma caixa, ainda amarrada ao track e aos números que a geraram.

    A ordem no `ResultadoPose` é a ordem do `submit`. `track_id` sozinho não
    bastaria: o teste de associação confere a caixa, e dois resultados com o
    mesmo id não podem trocar o pulso.
    """

    track_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    pose: PosePessoa


@dataclass(frozen=True, slots=True)
class ResultadoPose:
    """A saída de um snapshot. Um por câmera e por instante, nunca misturado."""

    camera_id: str
    received_at: float
    """Instante da borda que veio no `submit`, não o da inferência. Embutir a
    espera da fila faria o gesto medir a carga da GPU."""

    sequence: int | None
    """`Frame.sequence` quando a imagem é um frame; `None` para ndarray sem
    sequência explícita. Buraco aqui é snapshot descartado, não pessoa a menos."""

    pessoas: tuple[PessoaEstimada, ...]


@dataclass(frozen=True, slots=True)
class CameraPoseStats:
    camera_id: str
    aceitos: int = 0
    """Snapshots que entraram na fila. O que a saturação descartou também passou
    por aqui — é a mesma conta de `frames_in` do detector."""

    processados: int = 0
    descartados: int = 0
    """*Drop oldest* e o que `stop` abandonou sem inferência."""

    erros: int = 0


@dataclass(frozen=True, slots=True)
class PoseWorkerStats:
    queue_depth: int
    queue_size: int
    cameras: tuple[CameraPoseStats, ...] = ()

    @property
    def aceitos(self) -> int:
        return sum(camera.aceitos for camera in self.cameras)

    @property
    def processados(self) -> int:
        return sum(camera.processados for camera in self.cameras)

    @property
    def descartados(self) -> int:
        return sum(camera.descartados for camera in self.cameras)

    @property
    def erros(self) -> int:
        return sum(camera.erros for camera in self.cameras)


@dataclass(frozen=True, slots=True)
class _Pedido:
    camera_id: str
    received_at: float
    sequence: int | None
    imagem: Frame | np.ndarray
    caixas: tuple[CaixaPose, ...]


@dataclass(slots=True)
class _Contador:
    aceitos: int = 0
    processados: int = 0
    descartados: int = 0
    erros: int = 0


class PoseWorker:
    """Pose em pé: uma fila, uma thread e uma sessão."""

    def __init__(
        self,
        pose: Pose,
        *,
        queue_size: int = _FILA_PADRAO,
        on_result: Callable[[ResultadoPose], None] | None = None,
    ) -> None:
        self._pose = pose
        self._on_result = on_result
        self._queue_size = queue_size
        self._fila: DropOldestQueue[_Pedido] = DropOldestQueue(queue_size)
        self._lock = threading.Lock()
        self._vida = threading.Lock()
        self._encerrar = threading.Event()
        self._cameras: dict[str, _Contador] = {}
        self._thread: threading.Thread | None = None
        self._pose_fechada = False

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        """Sobe a thread. A segunda chamada, com ela viva, não faz nada.

        Idempotente de propósito: a sessão ONNX não aguenta duas threads, e o
        adaptador ainda vai cruzar a subida com o runtime. Errar para uma
        segunda thread seria inferência concorrente na mesma sessão.
        """
        with self._vida:
            if self._pose_fechada or self._encerrar.is_set():
                raise RuntimeError("o worker de pose já foi encerrado")
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="pose", daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 15.0) -> None:
        """Para a thread e fecha a sessão. Pode ser chamado mais de uma vez.

        Não drena a fila. A inferência em curso acaba — e o callback dela pode
        rodar enquanto este método espera — e o resto é contado como
        descartado. Callback depois que isto retorna, ou thread ainda viva
        sem ser o caso do timeout, é o encerramento que este método existe
        para não deixar acontecer.
        """
        self._encerrar.set()
        self._fila.close()
        with self._vida:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
            if thread.is_alive():
                # A sessão continua de pé: fechá-la por baixo de um `run` que
                # não voltou é uso da sessão depois do close. Outro `stop`
                # tenta o join de novo.
                log.warning("thread de pose não terminou em %.1fs", timeout)
                return
        self._descarta_restante()
        self._fechar_pose()

    # --- entrada ------------------------------------------------------------

    def submit(
        self,
        camera_id: str,
        imagem: Frame | np.ndarray,
        pessoas: Sequence[CaixaPose | tuple[int, Sequence[float]]],
        *,
        received_at: float,
        sequence: int | None = None,
    ) -> None:
        """Enfileira um snapshot. **Nunca bloqueia** esperando a inferência.

        `pessoas` vazio é snapshot válido: o frame existiu e não havia caixa.
        Não é filtro de track — isso fica no adaptador. Levanta `ValueError`
        se a imagem ou a caixa nem chegam a ser um snapshot; isso é erro de
        quem chama, não falha de inferência, e não entra na fila.

        Depois do retorno, mudar o array do chamador não muda o que a thread
        vai ler. `submit` após `stop` é ignorado.
        """
        if self._encerrar.is_set() or self._fila.closed:
            return
        pedido = _Pedido(
            camera_id=camera_id,
            received_at=received_at,
            sequence=_sequencia(imagem, sequence),
            imagem=_possuir_imagem(imagem),
            caixas=tuple(_como_caixa(pessoa) for pessoa in pessoas),
        )
        descartado = self._fila.put(pedido)
        if descartado is pedido:
            # A fila fechou entre a leitura e o `put`. O snapshot não entrou.
            return
        with self._lock:
            self._cameras.setdefault(camera_id, _Contador()).aceitos += 1
            if descartado is not None:
                self._cameras.setdefault(descartado.camera_id, _Contador()).descartados += 1

    def tick(self, timeout: float = 0.0) -> bool:
        """Processa no máximo um snapshot, na thread de quem chama.

        É o `DetectorWorker.tick`: saturação, erro e associação se testam sem
        `sleep` e sem corrida. Não convive com a thread — os dois consumiriam
        a mesma fila.
        """
        with self._vida:
            thread = self._thread
        if thread is not None and thread.is_alive():
            raise RuntimeError("tick disputa a fila com a thread de pose")
        if self._encerrar.is_set():
            return False
        pedido = self._fila.get(timeout=timeout)
        if pedido is None:
            return False
        self._processa(pedido)
        return True

    # --- processamento ------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._encerrar.is_set():
                pedido = self._fila.get(timeout=_ESPERA_S)
                if pedido is None:
                    if self._fila.closed or self._encerrar.is_set():
                        return
                    continue
                if self._encerrar.is_set():
                    self._conta_descarte(pedido.camera_id)
                    return
                try:
                    self._processa(pedido)
                except Exception as erro:  # noqa: BLE001 - a thread é a pose da loja inteira
                    # `_processa` já segura inferência e callback. O que chega
                    # aqui é defeito deste laço; morrer apagaria a pose de
                    # todas as câmeras até o processo reiniciar.
                    log.error("falha inesperada na pose: %s", _erro_seguro(erro))
                    self._conta_erro(pedido.camera_id)
        finally:
            with self._vida:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _processa(self, pedido: _Pedido) -> None:
        try:
            poses = tuple(
                self._pose.estimar(pedido.imagem, [caixa.xyxy for caixa in pedido.caixas])
            )
        except Exception as erro:  # noqa: BLE001 - falha numa câmera não pode parar as outras
            log.error(
                "inferência de pose falhou na câmera %s: %s",
                redige_texto(pedido.camera_id),
                _erro_seguro(erro),
            )
            self._conta_erro(pedido.camera_id)
            return

        if len(poses) != len(pedido.caixas):
            # Entregar mesmo assim colaria a junta da pessoa seguinte no track
            # anterior. Sem resultado é visível no contador; resultado trocado
            # vira gesto no pulso errado, sem erro nenhum.
            log.error(
                "pose devolveu %d resultado(s) para %d caixa(s) na câmera %s "
                "(tracks %s); nada foi entregue",
                len(poses),
                len(pedido.caixas),
                redige_texto(pedido.camera_id),
                ",".join(str(caixa.track_id) for caixa in pedido.caixas),
            )
            self._conta_erro(pedido.camera_id)
            return

        self._conta_processado(pedido.camera_id)
        if self._on_result is None:
            return
        resultado = ResultadoPose(
            camera_id=pedido.camera_id,
            received_at=pedido.received_at,
            sequence=pedido.sequence,
            pessoas=tuple(
                PessoaEstimada(
                    track_id=caixa.track_id,
                    x1=caixa.x1,
                    y1=caixa.y1,
                    x2=caixa.x2,
                    y2=caixa.y2,
                    pose=pose,
                )
                for caixa, pose in zip(pedido.caixas, poses, strict=True)
            ),
        )
        try:
            self._on_result(resultado)
        except Exception as erro:  # noqa: BLE001 - o consumidor é o adaptador, que ainda não existe
            log.error(
                "consumidor da pose falhou na câmera %s: %s",
                redige_texto(pedido.camera_id),
                _erro_seguro(erro),
            )
            self._conta_erro(pedido.camera_id)

    def _descarta_restante(self) -> None:
        while True:
            pedido = self._fila.get(timeout=0)
            if pedido is None:
                return
            self._conta_descarte(pedido.camera_id)

    def _fechar_pose(self) -> None:
        with self._vida:
            if self._pose_fechada:
                return
            self._pose_fechada = True
        self._pose.close()

    def _conta_processado(self, camera_id: str) -> None:
        with self._lock:
            self._cameras.setdefault(camera_id, _Contador()).processados += 1

    def _conta_descarte(self, camera_id: str) -> None:
        with self._lock:
            self._cameras.setdefault(camera_id, _Contador()).descartados += 1

    def _conta_erro(self, camera_id: str) -> None:
        with self._lock:
            self._cameras.setdefault(camera_id, _Contador()).erros += 1

    # --- observação ---------------------------------------------------------

    def stats(self) -> PoseWorkerStats:
        with self._lock:
            cameras = tuple(
                CameraPoseStats(
                    camera_id=camera_id,
                    aceitos=contador.aceitos,
                    processados=contador.processados,
                    descartados=contador.descartados,
                    erros=contador.erros,
                )
                for camera_id, contador in sorted(self._cameras.items())
            )
        return PoseWorkerStats(
            queue_depth=len(self._fila),
            queue_size=self._queue_size,
            cameras=cameras,
        )


def _sequencia(imagem: Frame | np.ndarray, sequence: int | None) -> int | None:
    if sequence is not None:
        return sequence
    if isinstance(imagem, Frame):
        return imagem.sequence
    return None


def _possuir_imagem(imagem: Frame | np.ndarray) -> Frame | np.ndarray:
    """Uma imagem própria por snapshot, compartilhada por todas as pessoas.

    `bytes` não muda sob o chamador, então o `Frame` segue sem cópia — copiar
    de novo seria outro frame inteiro na RAM do box, e a fila já limita
    quantos existem. `ndarray` é o buffer que o chamador reescreve no próximo
    frame; sem a cópia, a pose recortaria a pessoa do frame seguinte.
    """
    if isinstance(imagem, Frame):
        # `bytes` não aceita atribuição. Subclasse ou `bytearray` no campo
        # ainda seria mutável, e aí o snapshot precisa da cópia.
        if isinstance(imagem.data, bytes):
            return imagem
        return Frame(
            data=bytes(imagem.data),
            width=imagem.width,
            height=imagem.height,
            pixel_format=imagem.pixel_format,
            sequence=imagem.sequence,
            received_at=imagem.received_at,
        )
    array = np.asarray(imagem)
    if array.dtype != np.uint8 or array.ndim != 3 or array.shape[2] != 3:
        raise ValueError(
            "a pose espera BGR uint8 (altura, largura, 3) ou um Frame; "
            f"recebi {array.dtype} shape={tuple(int(v) for v in array.shape)}"
        )
    return np.array(array, dtype=np.uint8, copy=True)


def _como_caixa(pessoa: object) -> CaixaPose:
    if isinstance(pessoa, CaixaPose):
        return pessoa
    if isinstance(pessoa, tuple) and len(pessoa) == 2:
        track_id, bruto = pessoa
        numeros = _quatro_numeros(bruto)
        if isinstance(track_id, int) and not isinstance(track_id, bool) and numeros is not None:
            return CaixaPose(track_id, *numeros)
    raise ValueError(
        "cada pessoa da pose é (track_id, (x1, y1, x2, y2)) ou CaixaPose; "
        f"recebi {type(pessoa).__name__}"
    )


def _quatro_numeros(bruto: object) -> tuple[float, float, float, float] | None:
    """A caixa vinda do chamador, inclusive um `ndarray` que ele ainda vai reescrever.

    `ndarray` não é `Sequence`: tratar só lista deixaria passar a caixa do
    tracker, que é array, e o `submit` recusaria o caminho real.
    """
    if isinstance(bruto, np.ndarray):
        if bruto.shape != (4,):
            return None
        return (float(bruto[0]), float(bruto[1]), float(bruto[2]), float(bruto[3]))
    if isinstance(bruto, (str, bytes)) or not isinstance(bruto, Sequence) or len(bruto) != 4:
        return None
    return (float(bruto[0]), float(bruto[1]), float(bruto[2]), float(bruto[3]))


def _erro_seguro(erro: BaseException) -> str:
    """Tipo e mensagem, com URL e segredo mascarados. Sem traceback.

    `log.exception` imprimiria o `str()` cru. A mensagem da sessão ou do
    callback pode carregar a URL da câmera, e a senha do RTSP mora nela.
    """
    return redige_texto(f"{type(erro).__name__}: {erro}")
