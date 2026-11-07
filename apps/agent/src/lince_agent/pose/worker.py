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

**Contas.** Cada snapshot aceito está em exatamente um lugar:
`processados` (entrega feita, ou sem consumidor), `descartados` (fila cheia
ou parada), `erros` (inferência, associação ou callback), `em_curso` ou
`na_fila`. Somar `processados` e `erros` duas vezes contaria o callback
falho nos dois lados — por isso `processados` só sobe depois da entrega.
Erro de callback não mata o laço: o snapshot vai para `erros` e o próximo
segue. `aceitos` entra no mesmo lock do `put`, antes de o item poder ser
retirado; `submit` não segura o lock da sessão.

**Parada.** `stop` que retorna fechou a sessão: a thread morreu, nenhum
`tick` segue em inferência, e não há callback novo. O que estava só na fila
é descartado sem inferir. Se o prazo estoura com alguém ainda dentro de
`estimar` — a thread ou um `tick` — `stop` levanta `TimeoutError` e não
fecha a sessão. Isso não é parada concluída: a inferência em curso pode
ainda chamar o callback, e outro `stop`, quando ela puder voltar, é que
fecha. Para rodar de novo, constrói-se outro worker.
"""

from __future__ import annotations

import logging
import math
import numbers
import threading
import time
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
    """Entraram na fila. O que a saturação descartou também passou por aqui."""

    processados: int = 0
    """Entrega concluída, ou inferência sem consumidor. Callback que levantou
    não entra: esse snapshot está em `erros`, e somar os dois contaria duas
    vezes."""

    descartados: int = 0
    """*Drop oldest* e o que a parada tirou da fila sem inferir."""

    erros: int = 0
    """Inferência, associação ou callback. Terminal para o snapshot, não para
    o laço: a câmera seguinte continua."""

    em_curso: int = 0
    """Retirados da fila e ainda não entregues, descartados ou marcados como erro."""

    na_fila: int = 0


@dataclass(frozen=True, slots=True)
class PoseWorkerStats:
    """Fotografia em que a conta fecha.

    `aceitos == processados + descartados + erros + em_curso + na_fila`.

    Um snapshot não está em dois termos. `queue_depth` é a soma de `na_fila`,
    lida no mesmo lock que move o snapshot de um termo para o outro — por isso
    `processados` não aparece antes de `aceitos`, e um `submit` recusado (fila
    já fechada) não entra na conta.
    """

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

    @property
    def em_curso(self) -> int:
        return sum(camera.em_curso for camera in self.cameras)

    @property
    def na_fila(self) -> int:
        return sum(camera.na_fila for camera in self.cameras)


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
    em_curso: int = 0
    na_fila: int = 0


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
        """Contas e a retirada da fila. Não é o lock da sessão: `submit` pode
        segurá-lo no `put`, que não bloqueia, e nunca durante `estimar`."""

        self._vida = threading.Condition()
        self._encerrar = threading.Event()
        self._cameras: dict[str, _Contador] = {}
        self._thread: threading.Thread | None = None
        self._subindo = False
        self._reservas: dict[int, int] = {}
        """Profundidade de `tick` por thread. A thread de trabalho não entra:
        `stop` a espera no `join`. Serve para `start` não abrir outra inferência
        e para `stop` não fechar a sessão com um `tick` ainda dentro."""

        self._pose_fechada = False

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        """Sobe a thread. Com ela viva, a segunda chamada não faz nada.

        Recusa se um `tick` reservou a sessão: os dois chamariam `estimar` ao
        mesmo tempo. A reserva e esta decisão usam o mesmo lock — a checagem
        não se solta antes da inferência.
        """
        with self._vida:
            if self._pose_fechada or self._encerrar.is_set():
                raise RuntimeError("o worker de pose já foi encerrado")
            while self._subindo:
                self._vida.wait()
            if self._thread is not None and self._thread.is_alive():
                return
            if self._reservas:
                raise RuntimeError("tick em curso; a sessão não admite outra inferência")
            thread = threading.Thread(target=self._run, name="pose", daemon=True)
            self._thread = thread
            self._subindo = True
        falhou = False
        try:
            thread.start()
        except Exception:  # noqa: BLE001 - limpa `_subindo` e relança; senão o stop espera para sempre
            falhou = True
            raise
        finally:
            with self._vida:
                if falhou and self._thread is thread:
                    self._thread = None
                self._subindo = False
                self._vida.notify_all()

    def stop(self, timeout: float = 15.0) -> None:
        """Para e fecha a sessão, ou avisa que não deu.

        Retorno normal: thread morta, nenhum `tick` em inferência, fila
        restante descartada, sessão fechada, sem callback novo. Pode repetir.

        `TimeoutError`: ainda há inferência em curso. A sessão **continua
        aberta** — fechá-la por baixo de um `estimar` que não voltou é uso
        depois do close. O callback dessa inferência pode sair depois da
        exceção. Outro `stop`, quando ela puder terminar, conclui a parada.
        """
        if timeout < 0:
            raise ValueError(f"timeout deve ser >= 0, recebi {timeout}")
        prazo = time.monotonic() + timeout
        self._encerrar.set()
        self._fila.close()
        thread = self._thread_depois_de_subir(prazo, timeout)
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(max(0.0, prazo - time.monotonic()))
            if thread.is_alive():
                self._estourou(timeout)
        self._esperar_ticks(prazo, timeout)
        self._descarta_restante()
        self._fechar_pose()

    # --- entrada ------------------------------------------------------------

    def submit(
        self,
        camera_id: str,
        imagem: Frame | np.ndarray,
        pessoas: Sequence[CaixaPose | tuple[int, Sequence[float]]],
        *,
        received_at: float | None = None,
        sequence: int | None = None,
    ) -> None:
        """Enfileira um snapshot. **Nunca bloqueia** esperando a inferência.

        `pessoas` vazio é snapshot válido: o frame existiu e não havia caixa.
        Não é filtro de track — isso fica no adaptador. Levanta `ValueError`
        se a imagem ou a caixa nem chegam a ser um snapshot; isso é erro de
        quem chama, não falha de inferência, e não entra na fila.

        `received_at` omitido num `Frame` usa o instante do próprio frame. Num
        ndarray continua obrigatório: não há outro relógio que não seja o da
        inferência.

        Depois do retorno, mudar o array do chamador não muda o que a thread
        vai ler. `submit` com a fila já fechada não entra na conta. O lock da
        sessão não é tomado aqui — só o das contas, e só em volta do `put`,
        que não bloqueia.
        """
        instante = _instante(imagem, received_at)
        if self._encerrar.is_set():
            return
        pedido = _Pedido(
            camera_id=camera_id,
            received_at=instante,
            sequence=_sequencia(imagem, sequence),
            imagem=_possuir_imagem(imagem),
            caixas=tuple(_como_caixa(pessoa) for pessoa in pessoas),
        )
        with self._lock:
            if self._encerrar.is_set():
                return
            # Aceito antes de o item ficar visível, e o `put` ainda sob este
            # lock: a thread só retira com o mesmo lock, então `processados`
            # não é publicado antes de `aceitos`.
            cont = self._cont(camera_id)
            cont.aceitos += 1
            cont.na_fila += 1
            descartado = self._fila.put(pedido)
            if descartado is pedido:
                cont.aceitos -= 1
                cont.na_fila -= 1
                return
            if descartado is not None:
                vitima = self._cont(descartado.camera_id)
                vitima.na_fila -= 1
                vitima.descartados += 1

    def tick(self, timeout: float = 0.0) -> bool:
        """Processa no máximo um snapshot, na thread de quem chama.

        Reserva a sessão até voltar, no mesmo lock que `start` consulta. Sem
        isso `start` abria a thread entre a checagem e o `estimar`, e `stop`
        fechava a sessão com este método ainda lá dentro.
        """
        if not self._reservar():
            return False
        try:
            pedido = self._retirar()
            if pedido is None and timeout and not self._encerrar.is_set() and not self._fila.closed:
                self._fila.espera(timeout)
                pedido = self._retirar()
            if pedido is None:
                return False
            self._processa(pedido)
            return True
        finally:
            self._liberar()

    # --- processamento ------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._encerrar.is_set():
                pedido = self._retirar()
                if pedido is None:
                    if self._encerrar.is_set() or self._fila.closed:
                        return
                    self._fila.espera(_ESPERA_S)
                    continue
                self._processa(pedido)
        finally:
            with self._vida:
                if self._thread is threading.current_thread():
                    self._thread = None

    def _processa(self, pedido: _Pedido) -> None:
        erro = False
        try:
            try:
                poses = tuple(
                    self._pose.estimar(pedido.imagem, [caixa.xyxy for caixa in pedido.caixas])
                )
            except Exception as exc:  # noqa: BLE001 - falha numa câmera não pode parar as outras
                erro = True
                log.error(
                    "inferência de pose falhou na câmera %s: %s",
                    redige_texto(pedido.camera_id),
                    _erro_seguro(exc),
                )
                return

            if len(poses) != len(pedido.caixas):
                # Entregar mesmo assim colaria a junta da pessoa seguinte no track
                # anterior. Sem resultado o contador vê o erro; resultado trocado
                # vira gesto no pulso errado, sem erro nenhum.
                erro = True
                log.error(
                    "pose devolveu %d resultado(s) para %d caixa(s) na câmera %s "
                    "(tracks %s); nada foi entregue",
                    len(poses),
                    len(pedido.caixas),
                    redige_texto(pedido.camera_id),
                    ",".join(str(caixa.track_id) for caixa in pedido.caixas),
                )
                return

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
            except Exception as exc:  # noqa: BLE001 - o callback é o adaptador, que ainda não existe
                # Não soma em `processados`: o snapshot já vai para `erros`.
                # O laço segue — o erro é deste snapshot, não da pose da loja.
                erro = True
                log.error(
                    "consumidor da pose falhou na câmera %s: %s",
                    redige_texto(pedido.camera_id),
                    _erro_seguro(exc),
                )
        finally:
            self._fechar_curso(pedido.camera_id, erro=erro)

    def _retirar(self) -> _Pedido | None:
        """Tira um snapshot e passa `na_fila` para `em_curso`, sob o mesmo lock.

        Com a parada já pedida, o item vai para `descartados` e o retorno é
        `None`: não se começa inferência nova.
        """
        with self._lock:
            pedido = self._fila.get(timeout=0)
            if pedido is None:
                return None
            cont = self._cont(pedido.camera_id)
            cont.na_fila -= 1
            if self._encerrar.is_set():
                cont.descartados += 1
                return None
            cont.em_curso += 1
            return pedido

    def _descarta_restante(self) -> None:
        with self._lock:
            while True:
                pedido = self._fila.get(timeout=0)
                if pedido is None:
                    return
                cont = self._cont(pedido.camera_id)
                cont.na_fila -= 1
                cont.descartados += 1

    def _fechar_curso(self, camera_id: str, *, erro: bool) -> None:
        with self._lock:
            cont = self._cont(camera_id)
            cont.em_curso -= 1
            if erro:
                cont.erros += 1
            else:
                cont.processados += 1

    def _fechar_pose(self) -> None:
        with self._vida:
            if self._pose_fechada:
                return
            self._pose_fechada = True
        self._pose.close()

    def _cont(self, camera_id: str) -> _Contador:
        return self._cameras.setdefault(camera_id, _Contador())

    def _reservar(self) -> bool:
        with self._vida:
            if self._encerrar.is_set() or self._pose_fechada:
                return False
            thread = self._thread
            if self._subindo or (
                thread is not None
                and thread.is_alive()
                and thread is not threading.current_thread()
            ):
                raise RuntimeError("tick disputa a fila com a thread de pose")
            ident = threading.get_ident()
            self._reservas[ident] = self._reservas.get(ident, 0) + 1
            return True

    def _liberar(self) -> None:
        with self._vida:
            ident = threading.get_ident()
            falta = self._reservas.get(ident, 0) - 1
            if falta <= 0:
                self._reservas.pop(ident, None)
            else:
                self._reservas[ident] = falta
            self._vida.notify_all()

    def _thread_depois_de_subir(self, prazo: float, timeout: float) -> threading.Thread | None:
        with self._vida:
            while self._subindo:
                if not self._espera_vida(prazo):
                    self._estourou(timeout)
            return self._thread

    def _esperar_ticks(self, prazo: float, timeout: float) -> None:
        ident = threading.get_ident()
        with self._vida:
            while any(dono != ident for dono in self._reservas):
                if not self._espera_vida(prazo):
                    self._estourou(timeout)

    def _espera_vida(self, prazo: float) -> bool:
        restante = prazo - time.monotonic()
        if restante <= 0:
            return False
        self._vida.wait(restante)
        return True

    def _estourou(self, timeout: float) -> None:
        raise TimeoutError(
            f"a pose não encerrou em {timeout:.1f}s; a inferência em curso manteve a sessão aberta"
        )

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
                    em_curso=contador.em_curso,
                    na_fila=contador.na_fila,
                )
                for camera_id, contador in sorted(self._cameras.items())
            )
            # Mesmo lock da retirada: a profundidade não anda sem `na_fila`.
            profundidade = len(self._fila)
        return PoseWorkerStats(
            queue_depth=profundidade,
            queue_size=self._queue_size,
            cameras=cameras,
        )


def _instante(imagem: Frame | np.ndarray, received_at: float | None) -> float:
    if received_at is not None:
        return received_at
    if isinstance(imagem, Frame):
        return imagem.received_at
    raise ValueError(
        "received_at é obrigatório quando a imagem não é um Frame: "
        "sem ele o resultado usaria o relógio da inferência"
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
        if not all(math.isfinite(valor) for valor in pessoa.xyxy):
            raise ValueError(
                f"caixa do track {pessoa.track_id} tem coordenada não finita; "
                "a pose não recorta NaN"
            )
        return pessoa
    if isinstance(pessoa, tuple) and len(pessoa) == 2:
        track_id, bruto = pessoa
        numeros = _quatro_numeros(bruto)
        if isinstance(track_id, bool) or not isinstance(track_id, numbers.Integral):
            raise ValueError(f"track_id da pose é um inteiro (não bool); recebi {track_id!r}")
        if numeros is None:
            raise ValueError(
                "cada pessoa da pose é (track_id, (x1, y1, x2, y2)) ou CaixaPose; "
                f"a caixa de {track_id!r} não tem quatro números"
            )
        if not all(math.isfinite(valor) for valor in numeros):
            raise ValueError(
                f"caixa do track {int(track_id)} tem coordenada não finita; a pose não recorta NaN"
            )
        return CaixaPose(int(track_id), *numeros)
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
