"""A regra da bolsa: alcançar, e então levar o pulso à lateral do quadril e parar ou sumir ali.

É a linha de base do piloto do gesto, escrita a partir do que se viu no dataset de
furto encenado (MNNIT, 2022): o item vai para a bolsa a tiracolo, que fica na
lateral do quadril, e nesse instante a mão entra na bolsa e o pulso ou para colado
ao corpo ou some. Ela existe para ser **medida** — `tools/avalia_gesto.py` roda as
duas classes do dataset e diz quantos furtos pega e quantos alertas falsos dá — e
para fechar o caminho de ponta a ponta antes de existir classificador treinado.

Por pessoa e por pulso, cada amostra vira um de cinco estados, numa régua que é o
próprio tronco (`corpo.normaliza`):

- `longe` — a mais de `alcance_min` troncos do centro: o braço na prateleira;
- `zona` — dentro da faixa da bolsa (`|x| ≤ zona_x_max`, `zona_y_min ≤ y ≤ zona_y_max`);
- `meio` — nem um nem outro;
- `sumido` — pulso abaixo do limiar de confiança;
- `sem_tronco` — sem ombro ou quadril: não há régua, e a regra não chuta.

**Dispara** quando a sequência mais recente é uma corrida de `zona` seguida ou não de
`sumido`, que **começa** em `zona`, dura pelo menos `permanencia_min_s`, e tem um
`longe` antes dela dentro da janela. Começar em `zona` é o que separa a mão que entra
na bolsa (vídeos 11 e 12) da mão que vai para trás das costas e some (vídeos 10 e 13).
O alcance antes é o que separa a mão na bolsa da mão apoiada na cintura de quem espera.

O risco conhecido, que a avaliação existe para quantificar: quem pega um produto e o
segura junto ao quadril faz o mesmo movimento. A regra não sabe para onde a mão foi,
só onde o pulso parou.

**Medida no dataset (182 vídeos, 8 fps): não separa.** Disparou em 8% dos furtos e
6% dos normais. 150 dos 182 vídeos nunca chegam a `longe`: a prateleira está à
frente da pessoa, o braço estica na profundidade e encurta na imagem, e em 2D o
alcance quase não aparece. A melhor das 108 combinações da varredura dá 18% contra
9%. O pulso também some *mais* nos normais (17% das amostras) que nos furtos (7%).
A regra fica como gatilho provisório para fechar o caminho do evento; afinar estes
números não é o caminho — o que separa as classes, se algo separa, está no
movimento inteiro, que é trabalho do classificador.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

from lince_agent.gesto.corpo import normaliza, referencia_do_tronco
from lince_agent.pose.onnx import JUNTAS_COCO17, PosePessoa
from lince_agent.pose.worker import ResultadoPose

log = logging.getLogger(__name__)

Estado = Literal["longe", "zona", "meio", "sumido", "sem_tronco"]
Lado = Literal["esquerdo", "direito"]

_PULSOS: tuple[tuple[Lado, int], ...] = (
    ("esquerdo", JUNTAS_COCO17.index("pulso_esquerdo")),
    ("direito", JUNTAS_COCO17.index("pulso_direito")),
)

_FOLGA_S = 1e-6
"""Instantes vêm de `n / fps`; 4/8 − 2/8 tem que valer 0,25 e não 0,2499999."""


@dataclass(frozen=True, slots=True)
class ParametrosGesto:
    """Os números da regra. Defaults escolhidos a partir do dataset, para serem medidos.

    Nenhum deles é do plano: são o ponto de partida que a avaliação vai confirmar ou
    mudar. Por isso todos são flags em `tools/avalia_gesto.py`.
    """

    janela_s: float = 2.0
    """O gesto dura de 0,5 a 2 s (seção 2 do plano)."""

    alcance_min: float = 1.2
    """Troncos do centro. Braço esticado para a prateleira passa disso; mão no peito, não."""

    zona_x_max: float = 1.1
    """A bolsa a tiracolo pende ao lado do quadril, um pouco fora da silhueta."""

    zona_y_min: float = 0.2
    zona_y_max: float = 1.4
    """Do meio da barriga ao meio da coxa. O quadril fica em y ≈ +0,5."""

    permanencia_min_s: float = 0.25
    """Separa parar na bolsa de passar pelo quadril com o braço balançando."""

    refratario_s: float = 3.0
    """Um disparo por gesto. Oito frames com a mão parada na bolsa não são oito alertas."""

    limiar_junta: float = 0.3

    def __post_init__(self) -> None:
        if self.janela_s <= 0 or self.permanencia_min_s < 0 or self.refratario_s < 0:
            raise ValueError("janela, permanência e refratário têm que ser positivos")
        if self.zona_y_min >= self.zona_y_max or self.zona_x_max <= 0:
            raise ValueError(
                f"zona da bolsa vazia: y de {self.zona_y_min} a {self.zona_y_max}, "
                f"|x| até {self.zona_x_max}"
            )
        if self.alcance_min <= 0:
            raise ValueError("alcance_min tem que ser positivo")


@dataclass(frozen=True, slots=True)
class DisparoGesto:
    camera_id: str
    track_id: int
    instante: float
    """O `received_at` do frame em que a regra fechou."""

    sequence: int | None
    lado: Lado
    motivo: Literal["permaneceu", "sumiu"]
    """`sumiu`: a mão entrou na região da bolsa e o pulso deixou de ser visto ali."""

    alcance_em: float
    """Instante do último `longe` antes da corrida — onde o clipe deve começar a mostrar."""


@dataclass(frozen=True, slots=True)
class MotorGestoStats:
    observacoes: int = 0
    sem_tronco: int = 0
    """Pessoas-frame sem ombro ou quadril: a regra se absteve."""

    pulso_nao_visto: int = 0
    disparos: int = 0
    reconexoes: int = 0


@dataclass(slots=True)
class _Pulso:
    amostras: deque[tuple[float, Estado]] = field(default_factory=deque)


@dataclass(slots=True)
class _Track:
    visto_em: float
    pulsos: dict[Lado, _Pulso] = field(default_factory=dict)
    ultimo_disparo: float | None = None


def estados_dos_pulsos(pose: PosePessoa, parametros: ParametrosGesto) -> list[tuple[Lado, Estado]]:
    """O estado de cada pulso numa amostra. É o que o motor vê, exposto para quem mede.

    `tools/avalia_gesto.py` usa a mesma função para dizer **onde** a regra parou num
    vídeo sem disparo; uma segunda classificação lá divergiria desta no primeiro
    limiar ajustado, e o diagnóstico passaria a explicar outra regra.
    """
    p = parametros
    ref = referencia_do_tronco(pose, limiar=p.limiar_junta)
    if ref is None:
        return [(lado, "sem_tronco") for lado, _ in _PULSOS]
    estados: list[tuple[Lado, Estado]] = []
    for lado, indice in _PULSOS:
        junta = pose.juntas[indice]
        if not (
            math.isfinite(junta.confianca)
            and math.isfinite(junta.x)
            and math.isfinite(junta.y)
            and junta.confianca >= p.limiar_junta
        ):
            estados.append((lado, "sumido"))
            continue
        x, y = normaliza(junta.x, junta.y, ref)
        if abs(x) <= p.zona_x_max and p.zona_y_min <= y <= p.zona_y_max:
            estados.append((lado, "zona"))
        elif math.hypot(x, y) >= p.alcance_min:
            estados.append((lado, "longe"))
        else:
            estados.append((lado, "meio"))
    return estados


class MotorGesto:
    """A janela de cada pessoa e a regra sobre ela. Uma thread só: a de quem entrega a pose."""

    def __init__(self, parametros: ParametrosGesto | None = None) -> None:
        self.parametros = parametros or ParametrosGesto()
        self._tracks: dict[tuple[str, int], _Track] = {}
        self._ultima_sequencia: dict[str, int] = {}
        self._observacoes = 0
        self._sem_tronco = 0
        self._pulso_nao_visto = 0
        self._disparos = 0
        self._reconexoes = 0

    def observa(self, resultado: ResultadoPose) -> list[DisparoGesto]:
        self._marca_sessao(resultado)
        agora = resultado.received_at
        disparos: list[DisparoGesto] = []
        for pessoa in resultado.pessoas:
            self._observacoes += 1
            chave = (resultado.camera_id, pessoa.track_id)
            track = self._tracks.get(chave)
            if track is None:
                track = self._tracks[chave] = _Track(visto_em=agora)
            track.visto_em = agora
            for lado, estado in self._estados(pessoa.pose):
                pulso = track.pulsos.setdefault(lado, _Pulso())
                pulso.amostras.append((agora, estado))
                while (
                    pulso.amostras
                    and pulso.amostras[0][0] < agora - self.parametros.janela_s - _FOLGA_S
                ):
                    pulso.amostras.popleft()
                if self._em_refratario(track, agora):
                    continue
                disparo = self._avalia(pulso, agora)
                if disparo is None:
                    continue
                motivo, alcance_em = disparo
                track.ultimo_disparo = agora
                self._disparos += 1
                disparos.append(
                    DisparoGesto(
                        camera_id=resultado.camera_id,
                        track_id=pessoa.track_id,
                        instante=agora,
                        sequence=resultado.sequence,
                        lado=lado,
                        motivo=motivo,
                        alcance_em=alcance_em,
                    )
                )
        self._poda(agora)
        return disparos

    def _estados(self, pose: PosePessoa) -> list[tuple[Lado, Estado]]:
        estados = estados_dos_pulsos(pose, self.parametros)
        if estados[0][1] == "sem_tronco":
            self._sem_tronco += 1
        self._pulso_nao_visto += sum(1 for _, estado in estados if estado == "sumido")
        return estados

    def _avalia(
        self, pulso: _Pulso, agora: float
    ) -> tuple[Literal["permaneceu", "sumiu"], float] | None:
        amostras = list(pulso.amostras)
        # A corrida mais recente de `zona`/`sumido`, de trás para a frente.
        i = len(amostras)
        while i > 0 and amostras[i - 1][1] in ("zona", "sumido"):
            i -= 1
        corrida = amostras[i:]
        if not corrida or corrida[0][1] != "zona":
            # Vazia, ou começou sumindo: a mão foi para trás do corpo, não para a bolsa.
            return None
        if agora - corrida[0][0] + _FOLGA_S < self.parametros.permanencia_min_s:
            return None
        alcances = [t for t, estado in amostras[:i] if estado == "longe"]
        if not alcances:
            return None
        motivo: Literal["permaneceu", "sumiu"] = (
            "sumiu" if corrida[-1][1] == "sumido" else "permaneceu"
        )
        return motivo, alcances[-1]

    def _em_refratario(self, track: _Track, agora: float) -> bool:
        return (
            track.ultimo_disparo is not None
            and agora - track.ultimo_disparo < self.parametros.refratario_s
        )

    def _marca_sessao(self, resultado: ResultadoPose) -> None:
        """Na reconexão a sequência recomeça e os IDs de track também (armadilha já
        paga): o estado da câmera inteira vai embora, e só dela."""
        if resultado.sequence is None:
            return
        anterior = self._ultima_sequencia.get(resultado.camera_id)
        self._ultima_sequencia[resultado.camera_id] = resultado.sequence
        if anterior is None or resultado.sequence >= anterior:
            return
        for chave in [c for c in self._tracks if c[0] == resultado.camera_id]:
            del self._tracks[chave]
        self._reconexoes += 1
        log.info(
            "gesto: câmera %s reconectou; janelas da sessão anterior descartadas",
            resultado.camera_id,
        )

    def _poda(self, agora: float) -> None:
        limite = agora - 2 * self.parametros.janela_s
        for chave in [c for c, t in self._tracks.items() if t.visto_em < limite]:
            del self._tracks[chave]

    def tracks_em_memoria(self) -> int:
        return len(self._tracks)

    def stats(self) -> MotorGestoStats:
        return MotorGestoStats(
            observacoes=self._observacoes,
            sem_tronco=self._sem_tronco,
            pulso_nao_visto=self._pulso_nao_visto,
            disparos=self._disparos,
            reconexoes=self._reconexoes,
        )
