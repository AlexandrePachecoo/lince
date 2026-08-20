"""A máquina de estados por track — o estágio 4 (§3.4).

A regra principal do MVP em uma frase: **pessoa cruza a linha de saída sem ter
permanecido na zona do caixa por mais de N segundos**. Tudo neste módulo é a tentativa
de fazer essa frase sobreviver ao contato com uma loja de verdade.

Três decisões moram aqui, e as três existem por causa do R-1:

**Só posição observada entra na conta.** Um track `PERDIDO` continua sendo previsto
pelo Kalman, e o §3.3 avisa que a caixa extrapolada chega a virar do avesso numa
oclusão de segundos. Decidir travessia sobre extrapolação significaria disparar para
alguém que a câmera não estava vendo — um alerta que o triador abre e não encontra
ninguém. Aqui, um track sem detecção nova neste frame simplesmente não é amostrado: a
oclusão vira um segmento mais comprido entre duas observações reais, e a travessia é
julgada quando a pessoa reaparece.

**Track decidido nunca volta a decidir.** É o debounce de travessia do §3.4, e sai de
graça: quem está parado na porta com a caixa oscilando sobre a linha cruza dezenas de
vezes por minuto, e só a primeira conta.

**O erro é empurrado para o lado seguro.** Onde há dúvida — quanto tempo creditar numa
oclusão dentro do caixa, se o track é novo demais — a escolha é a que produz *menos*
alerta, não mais. O sistema não decide nada sozinho (§1): um evento a menos é um caso
que o gerente não viu; um evento falso a mais é o produto morrendo (R-1).

O motor é de uma thread só, a mesma da detecção e do tracking (ADR-007).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable

from lince_agent.config import RuleOptions
from lince_agent.rules.geometry import Ponto, Travessia
from lince_agent.rules.state import (
    CameraRuleStats,
    EstadoRegra,
    EstadoTrack,
    MotivoDescarte,
    RuleStats,
    RuleTrigger,
)
from lince_agent.track.state import Track, TrackingResult

log = logging.getLogger(__name__)


class RuleEngine:
    """O motor de uma câmera. Não é thread-safe, e não precisa ser."""

    def __init__(self, camera_id: str, options: RuleOptions) -> None:
        self._camera_id = camera_id
        self._options = options
        self._tracks: dict[int, EstadoTrack] = {}
        self._tempos: deque[float] = deque(maxlen=max(options.janela_tempo_caixa, 1))

        self.eventos = 0
        self.travessias_saida = 0
        self.travessias_entrada = 0
        self.expirados = 0
        self.descartes: dict[MotivoDescarte, int] = dict.fromkeys(MotivoDescarte, 0)

    def update(self, resultado: TrackingResult) -> tuple[RuleTrigger, ...]:
        """Avança a máquina de estados com os tracks de um frame."""
        if not self._options.enabled:
            return ()

        self._expira_sumidos(resultado.tracks)
        gatilhos = [
            gatilho
            for track in resultado.tracks
            if (gatilho := self._avalia(track, resultado.received_at)) is not None
        ]
        return tuple(gatilhos)

    def reset(self, motivo: str) -> None:
        """Esquece tudo desta câmera.

        Chamado na reconexão. O `TrackerPool` recria o `ByteTracker` nesse momento, e o
        contador de IDs **recomeça do 1** — sem este reset, o estado do track 1 da
        sessão anterior (possivelmente já decidido, possivelmente com posição da cena
        antiga) seria herdado pela primeira pessoa que aparecesse na sessão nova. Ela
        chegaria à porta já decidida, ou cruzaria uma linha vinda de um ponto onde
        nunca esteve.
        """
        if self._tracks:
            log.info(
                "regras da câmera %s zeradas (%s): %d tracks acompanhados descartados",
                self._camera_id,
                motivo,
                len(self._tracks),
            )
            self.expirados += sum(1 for estado in self._tracks.values() if not estado.decidido)
        self._tracks.clear()

    def reconfigure(self, options: RuleOptions) -> None:
        """Troca zonas, tempos e limiares desta câmera, e **esquece os tracks em curso**.

        Zerar não é conservadorismo, é correção. O estado por track guarda duas coisas
        medidas contra a calibração antiga: o tempo acumulado dentro da zona de caixa e
        o último ponto com lado conhecido da linha de saída. Com zonas novas, o tempo
        foi contado dentro de um polígono que não existe mais, e o lado é o lado de uma
        linha que mudou de lugar — uma pessoa parada no caixa poderia "atravessar" sem
        andar um centímetro, só porque a linha veio até ela.

        O preço é conhecido e pequeno: quem estava em trânsito no instante da troca não
        gera evento, e conta como `expirados`. Alguns segundos de cegueira numa câmera,
        no minuto em que alguém está recalibrando aquela câmera pelo dashboard. O outro
        lado do erro seria um evento medido metade numa calibração e metade na outra,
        que sobe com `versions.config` de uma só e é indefensável na triagem.

        `_tempos` é recriado porque o `maxlen` vem de `janela_tempo_caixa`; as amostras
        antigas são de outra zona e não devem sobreviver à troca.
        """
        self._options = options
        self._tempos = deque(maxlen=max(options.janela_tempo_caixa, 1))
        self.reset("configuração nova")

    # --- por track ----------------------------------------------------------

    def _avalia(self, track: Track, agora: float) -> RuleTrigger | None:
        if not track.vale_para_regra:
            # `PROVISORIO`: uma detecção de um frame só, que o §3.3 chama de objeto
            # estático virando pessoa. Nem estado ele ganha.
            return None
        if track.time_since_update_s > 0:
            # Sem detecção nova neste frame: a posição é previsão do Kalman. Ver o
            # docstring do módulo.
            return None

        estado = self._tracks.get(track.track_id)
        if estado is None:
            estado = EstadoTrack(track.track_id)
            self._tracks[track.track_id] = estado
        if estado.decidido:
            return None

        ponto = track.base_central
        dentro = self._no_caixa(ponto)
        self._credita_tempo(estado, agora, dentro=dentro)

        gatilho = None
        travessia = self._travessia(estado, ponto)
        if travessia is Travessia.ENTRADA:
            self.travessias_entrada += 1
        elif travessia is Travessia.SAIDA:
            self.travessias_saida += 1
            gatilho = self._decide(estado, track, agora)
        else:
            estado.estado = self._estado_em_transito(track, dentro=dentro)

        # A referência da travessia só avança quando o ponto **tem** lado. Um ponto
        # exatamente sobre a linha adia a decisão (ver `LinhaOrientada.travessia`), e
        # adiar não pode virar esquecer: se ele substituísse a referência, a comparação
        # do frame seguinte partiria de um ponto sem lado e devolveria "não cruzou" de
        # novo. A pessoa atravessaria a porta sem o motor jamais ver — sem log, sem
        # contador, sem rastro nenhum.
        if self._tem_lado(ponto):
            estado.ponto = ponto
        estado.visto_em = agora
        estado.dentro_do_caixa = dentro
        return gatilho

    def _decide(self, estado: EstadoTrack, track: Track, agora: float) -> RuleTrigger | None:
        """A travessia aconteceu. Vale evento?"""
        estado.estado = EstadoRegra.AVALIANDO
        self._tempos.append(estado.tempo_no_caixa_s)

        if not self._elegivel(track):
            return self._descarta(estado, MotivoDescarte.VIDA_CURTA, track)
        if estado.tempo_no_caixa_s >= self._options.tempo_caixa_min_s:
            return self._descarta(estado, MotivoDescarte.PAGOU, track)

        estado.estado = EstadoRegra.EVENTO
        self.eventos += 1
        log.info(
            "câmera %s: track %d cruzou a saída com %.1fs no caixa (mínimo %.1fs) "
            "após %.1fs de vida — possível ocorrência",
            self._camera_id,
            track.track_id,
            estado.tempo_no_caixa_s,
            self._options.tempo_caixa_min_s,
            track.age_s,
        )
        return RuleTrigger(
            camera_id=self._camera_id,
            track_id=track.track_id,
            at=agora,
            rule_id=self._options.rule_id,
            rule_version=self._options.rule_version,
            tempo_no_caixa_s=estado.tempo_no_caixa_s,
            idade_s=track.age_s,
        )

    def _descarta(
        self, estado: EstadoTrack, motivo: MotivoDescarte, track: Track
    ) -> RuleTrigger | None:
        estado.estado = EstadoRegra.DESCARTADO
        self.descartes[motivo] += 1
        log.debug(
            "câmera %s: track %d cruzou a saída e foi descartado (%s): %.1fs no caixa, "
            "%.1fs de vida, %d associações",
            self._camera_id,
            track.track_id,
            motivo,
            estado.tempo_no_caixa_s,
            track.age_s,
            track.hits,
        )
        return None

    def _elegivel(self, track: Track) -> bool:
        """O filtro de tempo mínimo de vida (§3.3, §3.4) — a defesa contra o R-2.

        Idade **e** associações. Só idade deixaria passar o track que passou quase toda
        a vida perdido, cuja trajetória é mais extrapolação que observação; só
        associações deixaria passar o track que acumulou hits em rajada, num punhado de
        frames, que é o que uma troca de ID produz num cruzamento.
        """
        return track.age_s >= self._options.vida_min_s and track.hits >= self._options.hits_min

    def _estado_em_transito(self, track: Track, *, dentro: bool) -> EstadoRegra:
        if not self._elegivel(track):
            return EstadoRegra.NOVO
        return EstadoRegra.NO_CAIXA if dentro else EstadoRegra.EM_LOJA

    def _credita_tempo(self, estado: EstadoTrack, agora: float, *, dentro: bool) -> None:
        """Soma o intervalo desde a amostra anterior, se as duas estavam no caixa.

        **As duas**, e não só a de agora: creditar o intervalo inteiro para quem acabou
        de entrar na zona daria a alguém que passou raspando pelo caixa o tempo que
        gastou vindo do outro lado da loja.

        O teto de `intervalo_max_s` trata a oclusão: alguém que some atrás de uma
        gôndola dentro da zona reaparece segundos depois, e creditar tudo seria crédito
        que ninguém observou. Truncar subestima o tempo de caixa, o que na dúvida faz o
        evento **subir** para triagem humana em vez de sumir.
        """
        if estado.visto_em is None or not (dentro and estado.dentro_do_caixa):
            return
        intervalo = agora - estado.visto_em
        if intervalo <= 0:
            return
        estado.tempo_no_caixa_s += min(intervalo, self._options.intervalo_max_s)

    def _travessia(self, estado: EstadoTrack, ponto: Ponto) -> Travessia | None:
        if estado.ponto is None or self._options.linha_saida is None:
            return None
        return self._options.linha_saida.travessia(estado.ponto, ponto)

    def _tem_lado(self, ponto: Ponto) -> bool:
        """Se o ponto está de um lado definido da linha, e não em cima dela."""
        linha = self._options.linha_saida
        return linha is None or linha.lado(ponto) != 0

    def _no_caixa(self, ponto: Ponto) -> bool:
        return any(zona.contem(ponto) for zona in self._options.zonas_caixa)

    def _expira_sumidos(self, tracks: tuple[Track, ...]) -> None:
        """Tira da memória quem o tracker não reporta mais.

        Sem isto o dicionário cresce com toda pessoa que já passou pela loja — e o
        agente roda por semanas sem reiniciar. Quem sumiu sem decidir nada é o desfecho
        normal da esmagadora maioria das pessoas, e conta como `EXPIRADO`.
        """
        vivos = {track.track_id for track in tracks}
        for track_id in [track_id for track_id in self._tracks if track_id not in vivos]:
            if not self._tracks[track_id].decidido:
                self.expirados += 1
            del self._tracks[track_id]

    # --- observação ---------------------------------------------------------

    def stats(self) -> CameraRuleStats:
        return CameraRuleStats(
            camera_id=self._camera_id,
            configurada=self._options.enabled,
            acompanhados=len(self._tracks),
            no_caixa=sum(
                1 for estado in self._tracks.values() if estado.estado is EstadoRegra.NO_CAIXA
            ),
            travessias_saida=self.travessias_saida,
            travessias_entrada=self.travessias_entrada,
            eventos=self.eventos,
            descartados_pagou=self.descartes[MotivoDescarte.PAGOU],
            descartados_vida_curta=self.descartes[MotivoDescarte.VIDA_CURTA],
            expirados=self.expirados,
            tempo_caixa_medio_s=sum(self._tempos) / len(self._tempos) if self._tempos else 0.0,
        )


class RuleEnginePool:
    """Um motor por câmera, com as zonas daquela câmera (§3.4).

    Por câmera e não por loja porque é isso que o ADR-002 comprou: a linha de saída de
    uma câmera é o corredor de outra, e recalibrar uma delas não pode mexer nas demais.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._motores: dict[str, RuleEngine] = {}
        self._sequencias: dict[str, int] = {}

    def register(self, camera_id: str, options: RuleOptions) -> None:
        """Faz a câmera aparecer na telemetria antes do primeiro frame.

        Uma câmera registrada sem zonas aparece com `configurada=False` — é o que
        impede uma loja de ficar muda em silêncio porque alguém esqueceu de desenhar a
        linha numa câmera.
        """
        self._motores[camera_id] = RuleEngine(camera_id, options)

    def reconfigure(self, camera_id: str, options: RuleOptions) -> bool:
        """Recalibra uma câmera com o agente em pé (§5.2). Devolve se a câmera existia.

        Não usa `register`, que recriaria o motor e zeraria os contadores junto — e
        `eventos`, `travessias_*` e `descartes` são a métrica de falso positivo por
        câmera (R-1). Perdê-los a cada recalibração destruiria exatamente a série que
        diz se a recalibração funcionou.
        """
        motor = self._motores.get(camera_id)
        if motor is None:
            return False
        motor.reconfigure(options)
        return True

    def update(self, resultado: TrackingResult) -> tuple[RuleTrigger, ...]:
        motor = self._motores.get(resultado.camera_id)
        if motor is None:
            return ()
        self._verifica_reconexao(motor, resultado)
        self._sequencias[resultado.camera_id] = resultado.sequence
        return motor.update(resultado)

    def _verifica_reconexao(self, motor: RuleEngine, resultado: TrackingResult) -> None:
        """Zera a câmera quando o `sequence` recua, pelo mesmo motivo do `TrackerPool`.

        E por um motivo a mais, específico deste estágio: na reconexão o `ByteTracker` é
        recriado e **os IDs recomeçam do 1**. Herdar o estado antigo faria a primeira
        pessoa da sessão nova nascer com o histórico de outra.
        """
        anterior = self._sequencias.get(resultado.camera_id)
        if anterior is not None and resultado.sequence < anterior:
            motor.reset(f"sequence {anterior} → {resultado.sequence}")

    def stats(self) -> RuleStats:
        return RuleStats(
            enabled=any(motor.stats().configurada for motor in self._motores.values()),
            cameras=tuple(
                motor.stats() for _, motor in sorted(self._motores.items(), key=lambda par: par[0])
            ),
        )
