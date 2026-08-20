"""O que muda entre duas configurações, e se dá para aplicar com o agente em pé.

A §5.2 entrega o documento inteiro a cada mudança, mas nem todo campo dele custa o
mesmo para trocar. Recalibrar uma zona é substituir dois polígonos numa máquina de
estados; trocar a resolução de decode é derrubar o `ffmpeg`, o buffer circular e todos
os tracks daquela câmera. Este módulo é quem separa os dois casos, e é puro de
propósito: a decisão precisa ser testável sem subir nada.

**Tudo ou nada por documento.** Se qualquer campo estrutural mudou, o resultado é
`ESTRUTURAL` e *nada* é aplicado — nem a metade que daria. A tentação de aplicar só os
limiares é forte e está errada: o evento sobe com `versions.config`, e é esse campo que
vai explicar um falso positivo três semanas depois (R-1). Um agente rodando "a versão 7
com as câmeras da 6" declara 7 e não é 7, e a investigação parte de uma calibração que
nunca existiu. Melhor um agente honesto pedindo reinício.

O que decide se um campo é quente não é a importância dele, é **onde ele foi lido**:

- Quente é o que é consultado a cada frame, num objeto que dá para substituir:
  `RuleEngine._options`, `ByteTracker._options`, `OnnxDetector._options`.
- Estrutural é o que já virou outra coisa na subida: argumento de linha de comando do
  `ffmpeg`, tamanho de fila, `maxlen` de `deque`, sessão do ONNX Runtime, o próprio
  conjunto de câmeras.

Na dúvida, estrutural. Errar para esse lado custa um reinício de agente, anunciado no
heartbeat; errar para o outro lado é um campo que o dashboard mostra como aplicado e
que o box ignora em silêncio — a loja calibrada na tela e não calibrada no box.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any

from lince_agent.config import (
    AgentConfig,
    CameraConfig,
)

# --------------------------------------------------------------------------- buckets

A_QUENTE: dict[str, frozenset[str]] = {
    # Tudo do §3.4. É literalmente o que o ADR-002 comprou ao separar as regras do
    # modelo: "zonas, tempos e limiares são configuração versionada, por câmera".
    # Se algum destes exigisse reinício, o ADR-002 não teria entregado nada.
    "rules": frozenset(
        {
            "enabled",
            "rule_id",
            "rule_version",
            "linha_saida",
            "zonas_caixa",
            "tempo_caixa_min_s",
            "vida_min_s",
            "hits_min",
            "intervalo_max_s",
            "janela_tempo_caixa",
        }
    ),
    # Lidos a cada `detect()` em `OnnxDetector`, depois da inferência. Trocar o objeto
    # de opções vale a partir do frame seguinte.
    "detection": frozenset({"score_threshold", "iou_threshold", "classes"}),
    # Lidos a cada `update()` em `ByteTracker`: são limiares de associação, não estado.
    # Os tracks vivos continuam válidos, com o Kalman intacto.
    "tracking": frozenset(
        {
            "enabled",
            "high_threshold",
            "iou_min",
            "iou_min_baixa",
            "iou_min_novo",
            "min_hits",
            "max_perdido_s",
            "max_tracks",
        }
    ),
}

ESTRUTURAL: dict[str, frozenset[str]] = {
    "raiz": frozenset({"tenant_id", "store_id"}),
    # `camera_id` e `url` são identidade e destino do processo de ingestão; `detect`
    # decide se a câmera foi sequer registrada no detector, no tracker e nas regras.
    "camera": frozenset({"camera_id", "url", "detect"}),
    # Todo campo de decode já virou argumento na linha de comando de um `ffmpeg` que
    # está rodando. Mudar qualquer um é matar e recriar o processo — e `width`/`height`
    # ainda invalidam as zonas, que `CameraConfig` valida contra a moldura.
    "decode": frozenset(
        {
            "sample_fps",
            "width",
            "height",
            "pixel_format",
            "hwaccel",
            "rtsp_transport",
            "socket_timeout_s",
            "probesize_bytes",
            "analyze_duration_s",
            "low_latency",
            "wallclock_timestamps",
            "log_level",
        }
    ),
    # Parâmetros da máquina de estados do `CameraSupervisor`, que já está em pé com
    # eles. Não são calibração de falso positivo e mudam praticamente nunca.
    "supervision": frozenset(
        {
            "backoff_base_s",
            "backoff_cap_s",
            "backoff_jitter_s",
            "offline_after_failures",
            "stall_timeout_s",
            "stop_grace_s",
        }
    ),
    # `window_s` dimensionou o buffer circular em RAM e `pending_requests` a fila do
    # recorder; os dois já foram alocados. Pré e pós-roll são estruturais por
    # conservadorismo: quem os lê é o recorder no momento do corte, e trocá-los com um
    # clipe em voo produziria um clipe com uma janela que não é nem a antiga nem a nova.
    "clip": frozenset(
        {
            "window_s",
            "pre_roll_s",
            "post_roll_s",
            "post_roll_grace_s",
            "max_bytes",
            "max_disk_bytes",
            "remux_timeout_s",
            "pending_requests",
        }
    ),
    # `enabled`, `model_path`, `providers` e `input_size` decidiram qual objeto detector
    # foi construído e com que sessão do ONNX Runtime; `queue_size` e `fps_window` já
    # dimensionaram fila e janela.
    "detection": frozenset(
        {"enabled", "model_path", "input_size", "providers", "queue_size", "fps_window"}
    ),
}

IGNORADO: dict[str, frozenset[str]] = {
    # É o rótulo da configuração, não um ajuste dela: por definição muda em todo
    # documento novo. Compará-lo faria todo poll parecer uma mudança.
    "raiz": frozenset({"config_version"}),
    # Do box, não da loja — a divisão dura da §5.2. Não vêm no documento e não podem
    # mudar por resposta HTTP, então não há o que comparar.
    "box": frozenset({"clips_dir", "outbox", "cloud", "ffmpeg_bin"}),
    # Comparadas câmera a câmera, por `camera_id`, em `_compara_cameras`.
    "especial": frozenset({"cameras"}),
    # Blocos aninhados: quem compara são as entradas de `A_QUENTE`/`ESTRUTURAL` de cada
    # um deles, não o objeto inteiro.
    "aninhado": frozenset({"decode", "supervision", "clip", "rules", "detection", "tracking"}),
}


class Veredito(StrEnum):
    """O que dá para fazer com a configuração nova."""

    IGUAL = "igual"
    """Nada mudou. Acontece quando a nuvem serve o mesmo documento com `ETag` novo."""

    A_QUENTE = "a_quente"
    """Só limiares, zonas e tempos. Aplicável com o agente em pé."""

    ESTRUTURAL = "estrutural"
    """Alguma coisa que já virou processo, fila ou sessão. Exige reinício."""


@dataclass(frozen=True, slots=True)
class Diff:
    """O que mudou, com o caminho de cada campo — o mesmo tom de erro do
    `config_loader`: `cameras[cam3].rules.tempo_caixa_min_s` é depurável, "a
    configuração mudou" não é."""

    a_quente: tuple[str, ...] = ()
    estruturais: tuple[str, ...] = ()

    @property
    def veredito(self) -> Veredito:
        if self.estruturais:
            return Veredito.ESTRUTURAL
        return Veredito.A_QUENTE if self.a_quente else Veredito.IGUAL


def classifica(atual: AgentConfig, nova: AgentConfig) -> Diff:
    """Compara duas configurações montadas e diz o que mudou de cada tipo."""
    a_quente: list[str] = []
    estruturais: list[str] = []

    _compara(atual, nova, "", ESTRUTURAL["raiz"], estruturais)
    _compara_cameras(atual.cameras, nova.cameras, a_quente, estruturais)
    _compara_bloco(atual.detection, nova.detection, "detection", a_quente, estruturais)
    _compara_bloco(atual.tracking, nova.tracking, "tracking", a_quente, estruturais)

    return Diff(a_quente=tuple(a_quente), estruturais=tuple(estruturais))


# --------------------------------------------------------------------------- interno


def _compara(atual: Any, nova: Any, prefixo: str, campos: frozenset[str], saida: list[str]) -> None:
    for nome in sorted(campos):
        if getattr(atual, nome) != getattr(nova, nome):
            saida.append(f"{prefixo}{nome}")


def _compara_bloco(
    atual: Any,
    nova: Any,
    nome: str,
    a_quente: list[str],
    estruturais: list[str],
    *,
    prefixo: str = "",
) -> None:
    caminho = f"{prefixo}{nome}."
    _compara(atual, nova, caminho, A_QUENTE.get(nome, frozenset()), a_quente)
    _compara(atual, nova, caminho, ESTRUTURAL.get(nome, frozenset()), estruturais)


def _compara_cameras(
    atuais: tuple[CameraConfig, ...],
    novas: tuple[CameraConfig, ...],
    a_quente: list[str],
    estruturais: list[str],
) -> None:
    """Câmera entrando ou saindo é sempre estrutural.

    Comparadas por `camera_id` e não por posição: reordenar a lista no dashboard não
    pode virar "todas as câmeras mudaram". E o `camera_id` é a chave de tudo o que o
    agente guarda por câmera — supervisor, buffer, tracker, motor de regras —, então
    perder essa correspondência aqui seria trocar a calibração de uma câmera pela de
    outra sem que nada estourasse.
    """
    por_id_atual = {camera.camera_id: camera for camera in atuais}
    por_id_nova = {camera.camera_id: camera for camera in novas}

    for camera_id in sorted(set(por_id_atual) ^ set(por_id_nova)):
        verbo = "removida" if camera_id in por_id_atual else "acrescentada"
        estruturais.append(f"cameras[{camera_id}] ({verbo})")

    for camera_id in sorted(set(por_id_atual) & set(por_id_nova)):
        atual, nova = por_id_atual[camera_id], por_id_nova[camera_id]
        prefixo = f"cameras[{camera_id}]."
        _compara(atual, nova, prefixo, ESTRUTURAL["camera"], estruturais)
        for bloco in ("decode", "supervision", "clip", "rules"):
            _compara_bloco(
                getattr(atual, bloco),
                getattr(nova, bloco),
                bloco,
                a_quente,
                estruturais,
                prefixo=prefixo,
            )


def campos_por_classe(cls: type) -> dict[str, str]:
    """Cada campo da dataclass e a classe em que ele caiu, para o teste de cobertura.

    Existe para que o teste possa afirmar o que realmente importa: **nenhum campo sem
    classificação**. Um campo novo que não entra em nenhum dos três conjuntos seria
    ignorado pelo diff — e ser ignorado pelo diff significa "trocado a quente sem
    ninguém reiniciar nada", que é o lado errado do erro.
    """
    todos = {campo.name for campo in fields(cls)}
    classificado: dict[str, str] = {}
    for balde, nome in ((A_QUENTE, "a_quente"), (ESTRUTURAL, "estrutural"), (IGNORADO, "ignorado")):
        for conjunto in balde.values():
            for campo in conjunto & todos:
                classificado.setdefault(campo, nome)
    return classificado
