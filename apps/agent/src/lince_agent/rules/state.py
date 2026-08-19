"""Estado observável do estágio 4.

Duas máquinas de estado convivem sobre a mesma pessoa e não podem ser confundidas: o
`TrackStatus` do §3.3 diz se o *tracker* ainda sabe onde ela está; o `EstadoRegra`
daqui diz o que ela já fez dentro da loja. Os nomes foram escolhidos diferentes de
propósito, e o docstring do `TrackStatus` avisa disso desde antes deste módulo existir.

O que sai daqui alimenta o gatilho (§3.5) e a telemetria do §5.3. A telemetria não é
enfeite: os contadores de descarte são o instrumento com que se calibra uma loja, e
`descartados_por_vida_curta` é o mais perto que dá para chegar de medir o R-2 em
produção, sem *ground truth*.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from lince_agent.rules.geometry import Ponto


class EstadoRegra(StrEnum):
    """Onde uma pessoa está na máquina de estados do §3.4."""

    NOVO = "novo"
    """Track visto, ainda jovem demais para valer. É aqui que mora o filtro de tempo
    mínimo de vida — e é aqui que fica o track que nasceu de uma troca de ID (R-2)."""

    EM_LOJA = "em_loja"
    """Circulando fora da zona do caixa."""

    NO_CAIXA = "no_caixa"
    """Dentro da zona do caixa, acumulando tempo."""

    AVALIANDO = "avaliando"
    """Cruzou a linha de saída; a decisão acontece neste mesmo frame. O estado existe
    para o instante da travessia ter nome no log — quando um evento sai errado, saber
    que a travessia foi vista é o que separa "a regra decidiu mal" de "a regra nunca
    viu a pessoa passar"."""

    EVENTO = "evento"
    DESCARTADO = "descartado"
    EXPIRADO = "expirado"
    """Track sumiu antes de decidir qualquer coisa. É o desfecho da esmagadora maioria
    das pessoas que entram numa loja, e não é anomalia nenhuma."""


class MotivoDescarte(StrEnum):
    """Por que uma travessia de saída não virou evento.

    Existe como enum, e não como texto livre em log, porque estes são os números que
    calibram uma loja: se quase tudo sai como `vida_curta`, o problema é o tracker
    (R-2) e mexer no N do caixa não vai adiantar nada.
    """

    PAGOU = "pagou"
    """Ficou no caixa o tempo mínimo. O desfecho esperado da maior parte das
    travessias — se este contador não domina os outros, a zona do caixa está errada."""

    VIDA_CURTA = "vida_curta"
    """O track era jovem demais na hora da travessia. É o filtro do §3.3/§3.4 fazendo
    seu trabalho, e é **o contador do R-2**: um track que nasce já perto da saída, sem
    histórico, é o que uma troca de ID produz. Crescendo, o tracking está fragmentando
    e não adianta mexer nos limiares da regra."""


@dataclass(frozen=True, slots=True)
class RuleTrigger:
    """A decisão de que aquilo é uma possível ocorrência.

    Não carrega imagem, caixa nem `track_id` para a nuvem: o `track_id` é local e
    efêmero por exigência do §3.3, e sobe apenas o que o `event.v1.json` define. Ele
    está aqui para o log da borda, que é onde se depura um evento que saiu errado.
    """

    camera_id: str
    track_id: int
    at: float
    """Monotônico da borda, herdado do frame — é o `t0` do corte do clipe (§3.5). O
    instante da inferência não serve: embutiria a latência da GPU no pré-roll."""

    rule_id: str
    rule_version: int
    tempo_no_caixa_s: float
    """Quanto a pessoa acumulou na zona do caixa até a travessia. Sobe no log e é o
    dado que calibra o N por layout de loja (§10.5)."""

    idade_s: float


@dataclass(slots=True)
class EstadoTrack:
    """O que o motor lembra de uma pessoa entre frames.

    Mutável, ao contrário de quase tudo no agente: é estado de máquina de estados, e
    congelá-lo obrigaria a recriar o objeto a cada frame de cada pessoa de cada câmera.
    Vive dentro de um `RuleEngine`, que é de uma thread só (ADR-007).
    """

    track_id: int
    estado: EstadoRegra = EstadoRegra.NOVO
    tempo_no_caixa_s: float = 0.0
    ponto: Ponto | None = None
    """Última posição **observada**, não prevista. Ver `RuleEngine.update`."""

    visto_em: float | None = None
    dentro_do_caixa: bool = False
    """Se o último ponto observado estava na zona do caixa. Guardado porque o tempo só
    é creditado entre duas amostras que estavam **ambas** dentro."""

    @property
    def decidido(self) -> bool:
        """Se este track já teve seu desfecho.

        Um track decidido nunca mais dispara, e é isso que implementa o debounce de
        travessia do §3.4 sem nenhuma janela de tempo: alguém parado na porta com a
        caixa oscilando sobre a linha cruza dezenas de vezes, e só a primeira conta.
        """
        return self.estado in (EstadoRegra.EVENTO, EstadoRegra.DESCARTADO)


@dataclass(frozen=True, slots=True)
class CameraRuleStats:
    """Contadores por câmera. Vão para o heartbeat (§5.3) e para o `--stats`."""

    camera_id: str
    configurada: bool = False
    """Se esta câmera tem zonas desenhadas. Uma câmera elegível para IA e sem zonas
    detecta, rastreia e nunca decide nada — precisa aparecer como tal, senão vira uma
    loja que não alerta e não reclama."""

    acompanhados: int = 0
    """Tracks vivos na máquina de estados agora."""

    no_caixa: int = 0
    travessias_saida: int = 0
    travessias_entrada: int = 0
    """Cruzar para dentro é normal (todo mundo entra). Mas uma câmera em que as
    entradas somem e as saídas explodem é a assinatura de uma linha desenhada ao
    contrário — o erro que o §3.4 lista como falso positivo em massa."""

    eventos: int = 0
    descartados_pagou: int = 0
    descartados_vida_curta: int = 0
    expirados: int = 0
    tempo_caixa_medio_s: float = 0.0
    """Média do tempo no caixa nas últimas travessias de saída, disparando ou não.

    É o dado que resolve a pendência §10.5 — o N por layout de loja — sem exigir
    anotação: se a média de quem sai é 3 s e o N está em 10 s, a regra vai disparar
    para a loja inteira, e isso dá para ver antes de o primeiro alerta sair."""


@dataclass(frozen=True, slots=True)
class RuleStats:
    enabled: bool = False
    cameras: tuple[CameraRuleStats, ...] = field(default_factory=tuple)

    @property
    def eventos(self) -> int:
        return sum(camera.eventos for camera in self.cameras)

    @property
    def travessias_saida(self) -> int:
        return sum(camera.travessias_saida for camera in self.cameras)

    @property
    def descartados_vida_curta(self) -> int:
        return sum(camera.descartados_vida_curta for camera in self.cameras)
