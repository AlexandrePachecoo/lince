"""Portão de movimento: decide, antes do detector, se há o que olhar (§3.2, ADR-012).

O mini market de condomínio passa a maior parte do dia vazio. Rodar o detector em todo
frame amostrado de três câmeras é pagar inferência para confirmar, três vezes por
segundo, que a loja continua vazia — e num mini PC sem placa essa conta é a diferença
entre caber e não caber. O portão compara o frame com o anterior da mesma câmera, em
escala reduzida, e só deixa passar quando algo mudou.

**O custo é desprezível de propósito.** Um canal só (o verde, no BGR; o luma, no
planar), um pixel a cada oito em cada eixo: num 640x480 são 4 800 amostras, contra os
milhões de operações de qualquer detector. Nada de OpenCV, desfoque ou modelo de fundo:
cada sofisticação aqui é custo pago em todo frame, inclusive no da loja vazia, que é
justamente o que o portão existe para baratear.

**O erro perigoso é o portão fechado com alguém na loja**, porque ele não deixa rastro:
um frame que não foi inferido não produz caixa, track, evento nem contador de regra. Por
isso há três aberturas além do movimento, e cada uma cobre um jeito de errar:

- **track ativo** — a pessoa parada na frente da geladeira, escolhendo, quase não mexe.
  Enquanto o tracking da câmera tem qualquer track (confirmado ou perdido), o portão
  fica aberto. É também o que mantém o tracking coerente: um track só morre por
  `update`, e `update` só acontece com o portão aberto — fechar com track vivo o
  congelaria na memória.
- **retenção** — por alguns segundos depois do último movimento, mesmo sem track. Cobre
  a pessoa que entrou e ainda não foi confirmada pelo tracking.
- **vigia** — com tudo fechado, ainda uma inferência a cada poucos segundos. Cobre quem
  já estava imóvel no quadro antes de o portão começar a olhar (reinício do agente com
  alguém na loja) e qualquer coisa que a diferença de frames não enxergue.

Na dúvida, abre: frame que não sabe amostrar passa para o detector.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from lince_agent.config import DetectionOptions
from lince_agent.ffmpeg.rawframe import Frame, PixelFormat

PASSO = 8
"""Um pixel a cada `PASSO` em cada eixo. Uma pessoa a 640x480 ocupa dezenas de pixels de
largura: mesmo longe da câmera, cobre várias amostras."""

LIMIAR_NIVEL = 25
"""Diferença de nível (0..255) para uma amostra contar como mudada. Abaixo disso é
ruído de sensor e de compressão — que numa câmera barata, à noite, chega perto de 10."""


class Abertura(StrEnum):
    """Por que o portão deixou o frame passar — ou que não deixou."""

    MOVIMENTO = "movimento"
    TRACK = "track"
    RETENCAO = "retencao"
    VIGIA = "vigia"
    FECHADO = "fechado"

    @property
    def infere(self) -> bool:
        return self is not Abertura.FECHADO


@dataclass(slots=True)
class _EstadoCamera:
    referencia: np.ndarray | None = None
    ultimo_movimento: float | None = None
    ultima_abertura: float | None = None


class PortaoMovimento:
    """Um estado por câmera. Roda na thread de detecção, e só nela: sem lock.

    O relógio é o `received_at` do frame, não um relógio próprio. É o timestamp da borda
    que o pipeline inteiro usa (§3.1), e mantém o portão determinístico no teste.
    """

    def __init__(self) -> None:
        self._cameras: dict[str, _EstadoCamera] = {}

    def avalia(
        self, camera_id: str, frame: Frame, *, tem_track: bool, options: DetectionOptions
    ) -> Abertura:
        estado = self._cameras.setdefault(camera_id, _EstadoCamera())
        agora = frame.received_at

        amostra = amostra_cinza(frame)
        referencia, estado.referencia = estado.referencia, amostra

        if referencia is None or referencia.shape != amostra.shape:
            # Primeiro frame da câmera, ou a resolução mudou: não há com o que comparar.
            # Abre como vigia — não como movimento, para não esticar a retenção.
            abertura = Abertura.VIGIA
        elif fracao_mudada(referencia, amostra) >= options.movimento_min:
            estado.ultimo_movimento = agora
            abertura = Abertura.MOVIMENTO
        elif tem_track:
            abertura = Abertura.TRACK
        elif (
            estado.ultimo_movimento is not None
            and agora - estado.ultimo_movimento < options.movimento_retencao_s
        ):
            abertura = Abertura.RETENCAO
        elif estado.ultima_abertura is None or agora - estado.ultima_abertura >= options.vigia_s:
            abertura = Abertura.VIGIA
        else:
            abertura = Abertura.FECHADO

        if abertura.infere:
            estado.ultima_abertura = agora
        return abertura


def amostra_cinza(frame: Frame) -> np.ndarray:
    """Uma grade reduzida de níveis de cinza, em `int16` para a subtração não estourar.

    Do BGR, o canal verde: é o que mais pesa na luminância e dispensa a conversão. Do
    NV12/YUV420P, o plano Y, que é a luminância pronta — as primeiras `height` linhas.
    """
    matriz = frame.as_array()
    if frame.pixel_format is PixelFormat.BGR24:
        grade = matriz[::PASSO, ::PASSO, 1]
    else:
        grade = matriz[: frame.height : PASSO, ::PASSO]
    return grade.astype(np.int16)


def fracao_mudada(anterior: np.ndarray, atual: np.ndarray) -> float:
    """Fração das amostras que mudaram mais que `LIMIAR_NIVEL`.

    Fração, e não soma das diferenças: uma lâmpada que oscila muda **todas** as amostras
    um pouco, e uma pessoa muda **algumas** muito. Contar amostras acima de um limiar
    separa as duas; somar diferenças as confunde.
    """
    if atual.size == 0:
        return 0.0
    return float(np.count_nonzero(np.abs(atual - anterior) > LIMIAR_NIVEL)) / atual.size
