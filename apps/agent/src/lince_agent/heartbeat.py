"""O heartbeat da §5.3: contar à nuvem como esta loja está passando.

O agente já sabia tudo isto de si — `AgentHealth` foi montado campo a campo contra a
tabela da §5.3 muito antes de existir para onde mandá-lo. O que faltava era o
transporte, e é só isso que este módulo é: uma função pura que traduz o `AgentHealth`
no documento do `heartbeat.v1.json`, e um laço fino que o posta a cada 30 s.

Três decisões que separam este módulo do `ConfigPoller`, apesar do formato idêntico:

1. **Heartbeat não se reenvia.** Um envio que falhou carrega telemetria velha; o tick
   seguinte tem um instantâneo novo e melhor. Não existe fila aqui, e nunca deve
   existir: uma fila de heartbeats entregaria à nuvem, depois de uma noite offline,
   duzentas fotografias do passado — e a única que interessa é a de agora.
2. **`classify_response` não serve, nem a de evento nem a de configuração.** As duas
   existem para decidir o que fazer com um item de fila. Sem fila, não há decisão de
   fila: o status aqui só escolhe qual contador sobe e qual log sai.
3. **Falhar é normal e não é grave.** A última linha da §5.4 vale inteira: o pipeline
   não pode nem desacelerar porque a nuvem não atende. Nada aqui levanta para fora do
   laço, e a linha do `--stats` existe para que a ausência de heartbeat apareça no box
   mesmo quando a nuvem não está lá para reclamar dela.

O que a nuvem faz com isso é a outra metade: ausência de heartbeat por um limite
configurado abre alerta técnico de **agente offline**, distinto de câmera offline,
porque a ação do operador é diferente (§5.3, R-6).
"""

from __future__ import annotations

import logging
import random
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lince_agent.backoff import backoff_delay
from lince_agent.config import RetryOptions
from lince_agent.outbox.http import CloudResponse, NetworkError
from lince_agent.outbox.policy import parse_retry_after

if TYPE_CHECKING:  # pragma: no cover - só para o type checker
    from lince_agent.runtime import AgentHealth

log = logging.getLogger(__name__)

INTERVALO_S = 30.0
"""O mesmo ritmo do poll de configuração (§5.3: "candidato: junto ao poll de 30 s").

São dois laços e não um só porque as políticas de falha divergem: o poll repete a
**mesma** pergunta até ser respondida, e o heartbeat descarta a resposta que não saiu.
Juntá-los faria um dos dois herdar o backoff do outro.
"""

SCHEMA_VERSION = 1


def monta_heartbeat(
    saude: AgentHealth,
    *,
    agent_version: str,
    to_iso: Callable[[float], str],
    clock_skew_s: float = 0.0,
    disk_free_bytes: int | None = None,
) -> dict[str, Any]:
    """Traduz o `AgentHealth` no corpo do `heartbeat.v1.json`.

    Pura de propósito — sem relógio, sem rede, sem disco. É aqui que mora tudo que dá
    para errar de verdade (uma câmera que some da lista, um monotônico que sobe como se
    fosse epoch), e função pura é o que permite testar isso contra o schema de
    `packages/shared` sem subir nada.

    `to_iso` é injetado, e é a âncora do runtime (`MonotonicAnchor.to_iso`): os
    instantes do agente são monotônicos, e converter com `time.time()` na hora do envio
    daria um `last_frame_at` deslocado de todo o tempo de subida do processo.
    """
    detector = saude.detector
    por_camera = {camera.camera_id: camera for camera in detector.cameras}

    cameras = []
    for camera in saude.cameras:
        # A lista base é a da **ingestão**, não a da detecção: câmera com `detect: false`
        # (R-3) decodifica, alimenta buffer e pode estar offline — a nuvem precisa vê-la
        # mesmo sem nunca ter havido inferência nela.
        deteccao = por_camera.get(camera.camera_id)
        cameras.append(
            {
                "camera_id": camera.camera_id,
                "status": str(camera.status),
                "decode_fps": camera.sampled_fps,
                "inference_fps": deteccao.inference_fps if deteccao else 0.0,
                # Os dois pontos de descarte somados: o da ingestão (consumidor lento,
                # §3.1) e o da fila do estágio 2 (GPU que não acompanha, R-4). Os dois
                # são frame decodificado que não virou inferência, que é o que o campo
                # mede. O `--stats` continua separando-os, porque lá a pergunta é outra:
                # qual dos dois estágios está apertado.
                "dropped_frames": camera.frames_dropped + (deteccao.dropped if deteccao else 0),
                "last_frame_at": (
                    to_iso(camera.last_frame_at) if camera.last_frame_at is not None else None
                ),
            }
        )

    fila = saude.outbox
    host: dict[str, Any] = {}
    if disk_free_bytes is not None:
        host["disk_free_bytes"] = disk_free_bytes

    return {
        "schema_version": SCHEMA_VERSION,
        "agent_version": agent_version,
        "model_version": detector.info.model_version if detector.info is not None else None,
        # A versão que está **valendo**, do `ConfigHealth` de quem aplicou — nunca a que
        # o poller baixou por último. Um documento estrutural é recebido, cacheado e
        # recusado; reportar a versão baixada faria a nuvem acreditar que a loja já
        # roda uma calibração que ninguém aplicou, e a investigação de um falso positivo
        # partiria de limiares que nunca estiveram em pé (R-1).
        "config_version": saude.config.config_version,
        "queue": {
            "depth": fila.depth,
            "oldest_age_s": fila.oldest_age_s,
            # O payload JSON mais o clipe em disco: é o que a fila ocupa de verdade na
            # loja, e o clipe é a parte pesada por três ordens de grandeza. Reportar só
            # o JSON mostraria alguns KB numa loja com o disco enchendo (§3.6).
            "bytes": fila.payload_bytes + saude.clip_disk_bytes,
        },
        "cameras": cameras,
        "host": host,
        "uptime_s": saude.uptime_s,
        # Respawn de ffmpeg somado sobre as câmeras. Zera com o processo, que é
        # exatamente o que o campo pede: com `uptime_s` baixo ao lado, distingue "o box
        # reiniciou agora" de "uma câmera está em crash loop" (R-6/R-7).
        "restarts": sum(camera.restarts for camera in saude.cameras),
        "clock_skew_s": clock_skew_s,
    }


def espaco_livre(caminho: Path) -> int | None:
    """Bytes livres na partição do diretório de clipes.

    É a única métrica de `host` que esta fatia preenche, e não por preguiça: é a que
    tem consequência hoje. O teto de disco do §3.6 despeja clipe quando a partição
    aperta, e um clipe despejado é um alerta que chega à triagem sem vídeo. As outras
    (cpu, ram, gpu) exigiriam `psutil` no runtime do box, que hoje é `numpy + redis +
    onnxruntime`.

    Devolve `None` em vez de levantar: o caminho pode não existir ainda numa subida, e
    heartbeat nenhum pode falhar por causa de uma métrica opcional.
    """
    try:
        return shutil.disk_usage(caminho).free
    except OSError as erro:
        log.debug("espaço livre indisponível em %s: %s", caminho, erro)
        return None


@dataclass(frozen=True, slots=True)
class HeartbeatStats:
    """O que o transporte observa. Não há "aplicado" aqui — heartbeat só sobe."""

    enviados: int = 0
    falhas: int = 0

    invalidos: int = 0
    """A nuvem recusou o corpo (`400`/`422`). Diferente de zero é divergência de
    contrato entre `monta_heartbeat` e o `heartbeat.v1.json` que a API valida — bug
    nosso, e é o que `test_contrato_heartbeat.py` existe para pegar antes da loja."""

    recusados: int = 0
    """`401`/`403`: credencial. O conserto é humano, e o log é próprio por isso."""

    ultimo_sucesso_s: float | None = None
    """Monotônico do último envio aceito. É a **idade** disto que denuncia um agente que
    a nuvem parou de enxergar — a contagem de falhas não serve, porque ela zera no
    reinício que o crash loop provoca."""

    clock_skew_s: float = 0.0
    skew_medido: bool = False
    """Se `False`, o `clock_skew_s` acima é o zero de "nunca houve resposta com `Date`",
    e não o zero de "os relógios batem". Sem esta distinção, um agente que nunca falou
    com a nuvem reportaria relógio em dia com toda a convicção do mundo."""


class HeartbeatSender:
    """Posta a telemetria do agente a cada 30 s, e nunca atrapalha ninguém.

    Mesmo formato do `ConfigPoller` — `tick()` público e síncrono, `clock` e `jitter`
    injetáveis, laço fino em volta —, e pela mesma razão: é o que torna a suíte
    testável sem `sleep` e sem tempo real (regra 6 do CLAUDE.md).
    """

    def __init__(
        self,
        client: Any,
        *,
        health: Callable[[], AgentHealth],
        agent_version: str,
        to_iso: Callable[[float], str],
        clips_dir: Path | None = None,
        retry: RetryOptions | None = None,
        intervalo_s: float = INTERVALO_S,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._client = client
        self._health = health
        """Chamado a **cada** tick, nunca guardado: o valor do heartbeat é ser recente.
        Um `AgentHealth` capturado na subida descreveria uma loja que não existe mais."""

        self._agent_version = agent_version
        self._to_iso = to_iso
        self._clips_dir = clips_dir
        self._retry = retry or RetryOptions()
        self._intervalo_s = intervalo_s
        self._clock = clock
        self._wall_clock = wall_clock
        self._jitter = jitter

        self._falhas_seguidas = 0
        self._proximo_em = 0.0
        self._skew_s = 0.0
        self._skew_medido = False
        self._lock = threading.Lock()
        self._stats = HeartbeatStats()
        self._acorda = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # --- ciclo de vida ------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("o heartbeat já está rodando")
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="heartbeat", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._acorda.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def stats(self) -> HeartbeatStats:
        with self._lock:
            return self._stats

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - ver docstring do módulo
                # Uma exceção inesperada aqui mataria a telemetria da loja em silêncio,
                # e o sintoma na nuvem seria idêntico ao de um box desligado: alerta de
                # agente offline para uma loja que está detectando normalmente.
                log.exception("falha inesperada no heartbeat; o laço continua")
                self._registra_falha()
            self._acorda.wait(self._espera())
            self._acorda.clear()

    # --- uma passagem -------------------------------------------------------

    def tick(self) -> bool:
        """Monta e posta um heartbeat. Devolve se a nuvem o aceitou.

        O corpo é montado **agora**, dentro do tick: um envio que falhou não deixa nada
        para trás, e o tick seguinte descreve a loja daquele momento, não deste.
        """
        corpo = monta_heartbeat(
            self._health(),
            agent_version=self._agent_version,
            to_iso=self._to_iso,
            clock_skew_s=self._skew_s,
            disk_free_bytes=espaco_livre(self._clips_dir) if self._clips_dir else None,
        )

        enviado_em = self._wall_clock()
        try:
            resposta = self._client.post_heartbeat(corpo)
        except NetworkError as erro:
            log.warning("heartbeat sem resposta: %s", erro)
            self._registra_falha()
            return False

        self._mede_skew(resposta, enviado_em=enviado_em)

        if 200 <= resposta.status < 300:
            self._sucesso()
            return True

        if resposta.status in (400, 422):
            # Não é falha de rede: a nuvem respondeu, e o que o agente mandou ela não
            # aceita. Entrar em backoff por isso esconderia uma divergência de contrato
            # atrás de um "deve ser a internet" — e o conserto é um deploy, não espera.
            log.error(
                "nuvem recusou o corpo do heartbeat (%d): divergência de contrato com "
                "heartbeat.v1.json",
                resposta.status,
            )
            self._registra_invalido()
            return False

        if resposta.status in (401, 403):
            log.warning(
                "nuvem recusou a credencial no heartbeat (%d): o pipeline segue, mas a "
                "loja está invisível até alguém consertar isso",
                resposta.status,
            )
            self._registra_falha(recusa=True, retry_after=resposta.retry_after)
            return False

        log.warning("heartbeat falhou (%d)", resposta.status)
        self._registra_falha(retry_after=resposta.retry_after)
        return False

    def _mede_skew(self, resposta: CloudResponse, *, enviado_em: float) -> None:
        """Deriva do relógio do box contra o da nuvem, pelo cabeçalho `Date`.

        Medida em toda resposta, inclusive nas de erro: um `503` também carrega `Date`,
        e um box com o relógio errado precisa ser denunciado mesmo quando a API está
        ruim. O valor entra no heartbeat **seguinte** — não há como carimbar no corpo
        uma medição que só existe depois de ele ter sido enviado.
        """
        if resposta.date is None:
            return
        try:
            nuvem = parsedate_to_datetime(resposta.date)
        except (TypeError, ValueError):
            log.debug("cabeçalho Date ilegível: %r", resposta.date)
            return
        with self._lock:
            self._skew_s = enviado_em - nuvem.timestamp()
            self._skew_medido = True
            self._stats = replace(self._stats, clock_skew_s=self._skew_s, skew_medido=True)

    # --- contadores e ritmo -------------------------------------------------

    def _espera(self) -> float:
        """Quanto dormir até a próxima passagem.

        Idêntico ao do poller, e pela mesma razão: o backoff **soma** ao intervalo em
        vez de substituí-lo. Com a nuvem fora do ar não há motivo para mandar telemetria
        mais rápido que em regime.
        """
        if self._falhas_seguidas == 0:
            return self._intervalo_s
        espera = backoff_delay(
            self._falhas_seguidas,
            base_s=self._retry.base_s,
            cap_s=self._retry.cap_s,
            jitter_s=self._retry.jitter_s,
            jitter=self._jitter,
        )
        return max(self._intervalo_s, min(espera, self._retry.cap_s), self._proximo_em)

    def _sucesso(self) -> None:
        self._falhas_seguidas = 0
        self._proximo_em = 0.0
        with self._lock:
            self._stats = replace(
                self._stats,
                enviados=self._stats.enviados + 1,
                ultimo_sucesso_s=self._clock(),
            )

    def _registra_falha(self, *, recusa: bool = False, retry_after: str | None = None) -> None:
        self._falhas_seguidas += 1
        espera = parse_retry_after(retry_after, now_s=self._wall_clock())
        # `Retry-After` é piso, nunca atalho — a mesma regra do §5.4 em todo o agente.
        self._proximo_em = espera or 0.0
        with self._lock:
            self._stats = replace(
                self._stats,
                falhas=self._stats.falhas + 1,
                recusados=self._stats.recusados + (1 if recusa else 0),
            )

    def _registra_invalido(self) -> None:
        """Corpo recusado não entra no backoff, pelo mesmo motivo do poller: um erro de
        contrato não se resolve esperando, e alargar o intervalo até o teto atrasaria o
        primeiro heartbeat **válido** depois do deploy que conserta."""
        with self._lock:
            self._stats = replace(self._stats, invalidos=self._stats.invalidos + 1)
