"""Dublês e fábricas do estágio 2.

O modelo real é caro e não está numa máquina limpa — é a exceção que o CLAUDE.md
reserva para mock. Todo o resto do estágio (fila, thread, contagem, descarte) roda
contra estes dublês, que fazem exatamente o que o teste mandar e nada mais.
"""

from __future__ import annotations

import threading

from lince_agent.config import PixelFormat
from lince_agent.detect.state import Detection, DetectorInfo
from lince_agent.ffmpeg.rawframe import Frame

LARGURA, ALTURA = 640, 480


def frame(sequence: int = 1, *, received_at: float | None = None) -> Frame:
    return Frame(
        data=bytes(PixelFormat.BGR24.frame_bytes(LARGURA, ALTURA)),
        width=LARGURA,
        height=ALTURA,
        pixel_format=PixelFormat.BGR24,
        sequence=sequence,
        received_at=float(sequence) if received_at is None else received_at,
    )


def pessoa(score: float = 0.9, x1: float = 10.0) -> Detection:
    return Detection(class_id=0, score=score, x1=x1, y1=20.0, x2=x1 + 40.0, y2=200.0)


INFO = DetectorInfo(
    model_version="dublê@000000000000",
    input_size=640,
    provider="CPUExecutionProvider",
    classes=(0,),
)


class DetectorFalso:
    """Devolve sempre as mesmas detecções e registra os frames que viu."""

    def __init__(self, deteccoes: tuple[Detection, ...] = (), *, info: DetectorInfo | None = INFO):
        self._deteccoes = deteccoes
        self._info = info
        self.vistos: list[Frame] = []
        self.fechado = False
        self.chamou = threading.Event()
        """Disparado a cada inferência, para o teste esperar sem `sleep` quando o
        worker está rodando na própria thread."""
        self.reconfiguracoes: list[object] = []

    def detect(self, frame: Frame) -> tuple[Detection, ...]:
        self.vistos.append(frame)
        self.chamou.set()
        return self._deteccoes

    def info(self) -> DetectorInfo | None:
        return self._info

    def reconfigure(self, options) -> None:
        """Guarda o que a troca a quente do §5.2 entregou.

        Faz parte do Protocol `Detector`, e não é enfeite: o `AgentRuntime` chama isto
        em toda configuração nova. Um dublê sem o método faria a aplicação estourar num
        `AttributeError` que só apareceria com o poll ligado, em produção.
        """
        self.reconfiguracoes.append(options)

    def close(self) -> None:
        self.fechado = True


class DetectorTravado:
    """Fica preso dentro de `detect` até alguém soltar.

    É como se simula GPU saturada sem `sleep`: a thread do worker entra na inferência e
    não sai, e o teste pode encher a fila com a certeza de que ninguém a está drenando.
    """

    def __init__(self) -> None:
        self.entrou = threading.Event()
        self.solta = threading.Event()
        self.chamadas = 0

    def detect(self, frame: Frame) -> tuple[Detection, ...]:
        self.chamadas += 1
        self.entrou.set()
        self.solta.wait(timeout=5.0)
        return ()

    def info(self) -> DetectorInfo | None:
        return INFO

    def reconfigure(self, options) -> None:
        return None

    def close(self) -> None:
        self.solta.set()


class DetectorQueFalha:
    """Levanta nas primeiras `falhas` chamadas e funciona depois."""

    def __init__(self, falhas: int = 1) -> None:
        self.restantes = falhas
        self.chamadas = 0

    def detect(self, frame: Frame) -> tuple[Detection, ...]:
        self.chamadas += 1
        if self.restantes > 0:
            self.restantes -= 1
            raise RuntimeError("a placa sumiu no meio da inferência")
        return (pessoa(),)

    def info(self) -> DetectorInfo | None:
        return INFO

    def reconfigure(self, options) -> None:
        return None

    def close(self) -> None:
        return None


class Relogio:
    """Relógio falso. `passo` é quanto cada leitura avança sozinha, o que permite
    simular uma inferência que demora sem esperar de verdade."""

    def __init__(self, inicio: float = 0.0, passo: float = 0.0) -> None:
        self.agora = inicio
        self.passo = passo

    def __call__(self) -> float:
        valor = self.agora
        self.agora += self.passo
        return valor

    def avanca(self, segundos: float) -> None:
        self.agora += segundos
