"""A fronteira entre o relógio monotônico da borda e o relógio de parede da nuvem.

Todo o pipeline mede tempo com `time.monotonic`, e isso é deliberado: é o único
relógio que não anda para trás quando o NTP corrige o horário do box, e o §3.1 já
decidiu que o timestamp de referência é o da borda, nunca o da câmera. Mas o evento
que sobe para a nuvem precisa de instante absoluto — o §6 guarda os instantes do
evento e a triagem acontece horas depois.

Converter um no outro exige uma âncora, e a âncora é onde mora o erro sutil: se ela
for relida a cada evento, um passo de NTP entre dois gatilhos faz o segundo evento
sair com instante **anterior** ao do primeiro. O clipe estaria certo e a linha do
tempo da triagem, errada.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

DEFAULT_STEP_THRESHOLD_S = 1.0
"""Acima disto não é deriva de cristal, é passo de relógio (NTP, `date -s`, RTC).
Deriva normal de um PC é da ordem de segundos por dia; um segundo em poucos minutos
só acontece quando alguém mexeu no relógio."""


@dataclass(frozen=True, slots=True)
class Instante:
    """Um ponto amarrado nos dois relógios ao mesmo tempo."""

    wall_s: float
    monotonic_s: float


def to_iso(wall_s: float) -> str:
    """Formata epoch UTC como ISO-8601 com milissegundos e sufixo `Z`.

    Milissegundos porque é a granularidade honesta: a incerteza real do instante do
    gatilho é o intervalo entre frames avaliados (~333 ms a 3 fps) somado ao
    alinhamento de GOP. Emitir microssegundos daria à nuvem uma precisão que o
    pipeline não tem.
    """
    texto = datetime.fromtimestamp(wall_s, tz=UTC).isoformat(timespec="milliseconds")
    # `isoformat` produz "+00:00"; o contrato do §5 usa "Z" e o schema exige.
    return texto.removesuffix("+00:00") + "Z"


class MonotonicAnchor:
    """Converte instantes monotônicos em instantes de parede, de forma estável.

    Lido de duas threads — a do recorder, no gatilho, e a do sender, na verificação
    de deriva —, então o par de âncora anda sob lock. Trocar os dois campos sem lock
    deixaria uma janela em que `wall₀` é novo e `mono₀` é velho, e o instante do
    evento sairia deslocado justamente no momento em que o relógio estava errado.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        step_threshold_s: float = DEFAULT_STEP_THRESHOLD_S,
    ) -> None:
        self._clock = clock
        self._wall_clock = wall_clock
        self._step_threshold_s = step_threshold_s
        self._lock = threading.Lock()
        self._anchor = Instante(wall_s=wall_clock(), monotonic_s=clock())
        self._reanchors = 0

    @property
    def anchor(self) -> Instante:
        with self._lock:
            return self._anchor

    @property
    def reanchors(self) -> int:
        """Quantas vezes o relógio de parede deu um passo. Vai para o heartbeat: um
        box reancorando toda hora tem NTP brigando com o RTC, e todos os instantes
        que ele reportou até agora merecem desconfiança."""
        with self._lock:
            return self._reanchors

    def to_wall(self, monotonic_s: float) -> float:
        with self._lock:
            anchor = self._anchor
        return anchor.wall_s + (monotonic_s - anchor.monotonic_s)

    def to_iso(self, monotonic_s: float) -> str:
        return to_iso(self.to_wall(monotonic_s))

    def now_iso(self) -> str:
        """Instante de parede **agora**, lido direto, sem passar pela âncora.

        É o `reported_at` do payload: a leitura crua é justamente o que permite à
        nuvem comparar com o `occurred_at` derivado e medir a deriva da âncora.
        """
        return to_iso(self._wall_clock())

    def check_drift(self) -> float:
        """Mede a divergência entre a âncora e o relógio de parede, e reancora num passo.

        Devolve a deriva medida, em segundos, positiva quando o relógio de parede
        está adiantado em relação ao que a âncora previa.

        Reancorar só no passo grande é o ponto todo: aplicar continuamente cada
        microcorreção do NTP faria dois eventos consecutivos poderem sair fora de
        ordem. Um passo grande é raro e, quando acontece, manter a âncora velha seria
        pior — todos os eventos seguintes nasceriam com o horário errado.
        """
        with self._lock:
            anchor = self._anchor
            mono = self._clock()
            wall = self._wall_clock()
            drift = wall - (anchor.wall_s + (mono - anchor.monotonic_s))
            if abs(drift) < self._step_threshold_s:
                return drift
            self._anchor = Instante(wall_s=wall, monotonic_s=mono)
            self._reanchors += 1

        log.warning(
            "relógio de parede deu um passo de %.3fs; âncora refeita (%d no total). "
            "Eventos já enfileirados mantêm o instante com que nasceram.",
            drift,
            self._reanchors,
        )
        return drift
