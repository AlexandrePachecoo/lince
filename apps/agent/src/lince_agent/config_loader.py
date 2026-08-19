"""Do documento de configuração (§5.2) para o `AgentConfig`.

Este módulo é a fronteira que o `AgentConfig` promete: nada abaixo dele sabe de onde
a configuração veio. Hoje ela vem de um arquivo no disco do box; amanhã vem do
`GET /v1/agents/config`, e a única coisa que muda é quem entrega o dicionário — o
formato é **o mesmo**, definido em `packages/shared/schemas/config.v1.json`.

Isso não é elegância: é o que impede o erro mais caro do projeto, que é a definição do
contrato existir em dois lugares e divergir. Inventar aqui um dialeto YAML só para o
arquivo local significaria escrever o parser duas vezes, e a segunda — a que roda em
produção contra a nuvem — seria a menos testada.

**O que o documento não carrega**, de propósito: URL do Redis, diretório dos clipes,
caminho do `.onnx`, endereço e credencial da API, binário do ffmpeg. Isso é do box, não
da loja. Repointar o disco de uma loja não pode ser efeito de uma resposta HTTP, e a
credencial não pode viajar dentro do que ela mesma autentica. Tudo isso entra por
argumento nomeado, e é o que a CLI continua controlando por flag.

**Nenhuma regra de validação mora aqui.** Os limites são os `__post_init__` do
`config.py`, que são os mesmos rodando venha o documento de onde vier; este módulo só
converte tipos, recusa campo desconhecido e acrescenta à mensagem o caminho onde o
problema está — `cameras[1].rules.linha_saida.origem` é depurável, `ValueError: ...`
solto no meio de um JSON de três telas não é.

O schema JSON **não** é validado em tempo de execução: `jsonschema` é dependência de
desenvolvimento, e pôr um validador no box para conferir um formato que este loader já
recusa campo a campo seria peso sem ganho. Quem garante que os dois concordam é
`tests/test_contrato_config.py`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any

from lince_agent.config import (
    AgentConfig,
    CameraConfig,
    ClipOptions,
    CloudOptions,
    DecodeOptions,
    DetectionOptions,
    HwAccel,
    OutboxOptions,
    PixelFormat,
    RuleOptions,
    SupervisionOptions,
    TrackingOptions,
)
from lince_agent.rules.geometry import LinhaOrientada, Poligono

SCHEMA_VERSION = 1
"""Major do `config.v1.json`. Sobe junto com o nome do arquivo do schema, e só quando
um campo obrigatório nasce, some ou muda de tipo — campo opcional novo não bumpa."""


class ConfigError(ValueError):
    """Configuração que o agente recusa, com o caminho do campo na mensagem.

    Herda de `ValueError` porque é o que o `AgentConfig` levanta e o que a CLI já
    trata: um agente que sobe com configuração inválida é pior que um que não sobe.
    """


# --------------------------------------------------------------------------- escalares

Conversor = Callable[[Any, str], Any]


def _erro(caminho: str, mensagem: str) -> ConfigError:
    return ConfigError(f"{caminho or 'raiz'}: {mensagem}")


def _tipo(valor: object) -> str:
    """Nome do tipo em JSON, não em Python: quem escreveu o documento não pensa em
    `dict` nem em `NoneType`."""
    return {
        type(None): "null",
        bool: "booleano",
        int: "número",
        float: "número",
        str: "texto",
        dict: "objeto",
        list: "lista",
    }.get(type(valor), type(valor).__name__)


def _texto(valor: Any, caminho: str) -> str:
    if not isinstance(valor, str):
        raise _erro(caminho, f"esperava texto, recebi {_tipo(valor)}")
    return valor


def _booleano(valor: Any, caminho: str) -> bool:
    if not isinstance(valor, bool):
        raise _erro(caminho, f"esperava true ou false, recebi {_tipo(valor)}")
    return valor


def _numero(valor: Any, caminho: str) -> float:
    # `isinstance(True, int)` é verdadeiro em Python. Sem esta guarda, `"sample_fps":
    # true` viraria 1.0 fps sem uma linha de log — e a diferença entre 3 fps e 1 fps só
    # aparece semanas depois, como tracking que perde quem anda rápido.
    if isinstance(valor, bool) or not isinstance(valor, int | float):
        raise _erro(caminho, f"esperava número, recebi {_tipo(valor)}")
    return float(valor)


def _inteiro(valor: Any, caminho: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise _erro(caminho, f"esperava número inteiro, recebi {_tipo(valor)}")
    return valor


def _enumeracao(cls: type[StrEnum]) -> Conversor:
    def converte(valor: Any, caminho: str) -> StrEnum:
        texto = _texto(valor, caminho)
        try:
            return cls(texto)
        except ValueError as erro:
            aceitos = ", ".join(membro.value for membro in cls)
            raise _erro(caminho, f"{texto!r} não é válido; aceito: {aceitos}") from erro

    return converte


def _lista(item: Conversor) -> Conversor:
    def converte(valor: Any, caminho: str) -> tuple:
        if not isinstance(valor, Sequence) or isinstance(valor, str | bytes):
            raise _erro(caminho, f"esperava uma lista, recebi {_tipo(valor)}")
        return tuple(item(elemento, f"{caminho}[{i}]") for i, elemento in enumerate(valor))

    return converte


def _opcional(conversor: Conversor) -> Conversor:
    """`null` explícito é "não tem", e não erro de tipo.

    A nuvem precisa poder apagar uma linha de saída sem omitir a chave: omitir é
    "mantenha o que estava", e a diferença importa quando o documento é um PATCH mental
    na cabeça de quem edita.
    """

    def converte(valor: Any, caminho: str) -> Any:
        return None if valor is None else conversor(valor, caminho)

    return converte


# --------------------------------------------------------------------------- objetos


def _campos(dado: Any, caminho: str, conversores: Mapping[str, Conversor]) -> dict[str, Any]:
    """Converte só as chaves presentes, e recusa qualquer outra.

    Recusar campo desconhecido é a mesma escolha do `additionalProperties: false` do
    contrato, pelo mesmo motivo: um campo com nome errado — `zona_caixa` no lugar de
    `zonas_caixa`, `tempo_caixa` no lugar de `tempo_caixa_min_s` — não pode ser
    descartado em silêncio. Quem digitou acha que calibrou a loja, e a câmera segue com
    o default alertando para todo mundo (R-1).
    """
    if not isinstance(dado, Mapping):
        raise _erro(caminho, f"esperava um objeto, recebi {_tipo(dado)}")

    desconhecidos = sorted(chave for chave in dado if chave not in conversores)
    if desconhecidos:
        aceitos = ", ".join(sorted(conversores))
        raise _erro(
            caminho,
            f"campo(s) que não existem: {', '.join(desconhecidos)}. Aceito aqui: {aceitos}",
        )

    return {
        nome: conversor(dado[nome], f"{caminho}.{nome}" if caminho else nome)
        for nome, conversor in conversores.items()
        if nome in dado
    }


def _monta[T](
    cls: type[T],
    dado: Any,
    caminho: str,
    conversores: Mapping[str, Conversor],
    **extras: Any,
) -> T:
    """Bloco ausente é bloco com os defaults — os do `config.py`, nunca repetidos aqui."""
    campos = _campos({} if dado is None else dado, caminho, conversores)
    try:
        return cls(**campos, **extras)  # type: ignore[call-arg]
    except ValueError as erro:
        # A mensagem do `__post_init__` já diz o que fazer; o que falta a ela é onde.
        raise _erro(caminho, str(erro)) from erro


def _obrigatorios(dado: Mapping[str, Any], caminho: str, nomes: Sequence[str]) -> None:
    faltando = [nome for nome in nomes if nome not in dado]
    if faltando:
        raise _erro(caminho, f"falta(m) o(s) campo(s) obrigatório(s): {', '.join(faltando)}")


# --------------------------------------------------------------------------- geometria


def _ponto(valor: Any, caminho: str) -> tuple[float, float]:
    if not isinstance(valor, Sequence) or isinstance(valor, str | bytes) or len(valor) != 2:
        raise _erro(caminho, f"esperava um ponto [x, y], recebi {valor!r}")
    return (_numero(valor[0], f"{caminho}[0]"), _numero(valor[1], f"{caminho}[1]"))


def _linha_orientada(valor: Any, caminho: str) -> LinhaOrientada:
    if isinstance(valor, Mapping):
        _obrigatorios(valor, caminho, ("origem", "destino"))
    return _monta(LinhaOrientada, valor, caminho, {"origem": _ponto, "destino": _ponto})


def _poligono(valor: Any, caminho: str) -> Poligono:
    if isinstance(valor, Mapping):
        _obrigatorios(valor, caminho, ("vertices",))
    return _monta(Poligono, valor, caminho, {"vertices": _lista(_ponto)})


# --------------------------------------------------------------------------- blocos

_DECODE: dict[str, Conversor] = {
    "sample_fps": _numero,
    "width": _inteiro,
    "height": _inteiro,
    "pixel_format": _enumeracao(PixelFormat),
    "hwaccel": _enumeracao(HwAccel),
    "rtsp_transport": _texto,
    "socket_timeout_s": _numero,
    "probesize_bytes": _inteiro,
    "analyze_duration_s": _numero,
    "low_latency": _booleano,
    "wallclock_timestamps": _booleano,
    "log_level": _texto,
}

_SUPERVISION: dict[str, Conversor] = {
    "backoff_base_s": _numero,
    "backoff_cap_s": _numero,
    "backoff_jitter_s": _numero,
    "offline_after_failures": _inteiro,
    "stall_timeout_s": _numero,
    "stop_grace_s": _numero,
}

_CLIP: dict[str, Conversor] = {
    "window_s": _numero,
    "pre_roll_s": _numero,
    "post_roll_s": _numero,
    "post_roll_grace_s": _numero,
    "max_bytes": _inteiro,
    "max_disk_bytes": _inteiro,
    "remux_timeout_s": _numero,
    "pending_requests": _inteiro,
}

_RULES: dict[str, Conversor] = {
    "enabled": _booleano,
    "rule_id": _texto,
    "rule_version": _inteiro,
    "linha_saida": _opcional(_linha_orientada),
    "zonas_caixa": _lista(_poligono),
    "tempo_caixa_min_s": _numero,
    "vida_min_s": _numero,
    "hits_min": _inteiro,
    "intervalo_max_s": _numero,
    "janela_tempo_caixa": _inteiro,
}

_CAMERA: dict[str, Conversor] = {
    "camera_id": _texto,
    "url": _texto,
    "detect": _booleano,
}

_DETECTION: dict[str, Conversor] = {
    "enabled": _booleano,
    "input_size": _inteiro,
    "score_threshold": _numero,
    "iou_threshold": _numero,
    "classes": _lista(_inteiro),
    "queue_size": _inteiro,
    "fps_window": _inteiro,
}

_TRACKING: dict[str, Conversor] = {
    "enabled": _booleano,
    "high_threshold": _numero,
    "iou_min": _numero,
    "iou_min_baixa": _numero,
    "iou_min_novo": _numero,
    "min_hits": _inteiro,
    "max_perdido_s": _numero,
    "max_tracks": _inteiro,
}

_RAIZ: dict[str, Conversor] = {
    "schema_version": _inteiro,
    "config_version": _texto,
    "tenant_id": _texto,
    "store_id": _texto,
    "cameras": lambda valor, caminho: valor,
    "detection": lambda valor, caminho: valor,
    "tracking": lambda valor, caminho: valor,
}
"""As três últimas chaves são consumidas por `monta_config`, que precisa combiná-las
com o que vem do box; passam cruas por aqui só para o campo desconhecido ser recusado
na raiz junto com os outros."""


def _monta_camera(dado: Any, caminho: str) -> CameraConfig:
    if not isinstance(dado, Mapping):
        raise _erro(caminho, f"esperava um objeto, recebi {_tipo(dado)}")
    _obrigatorios(dado, caminho, ("camera_id", "url"))

    aninhados = {
        "decode": (DecodeOptions, _DECODE),
        "supervision": (SupervisionOptions, _SUPERVISION),
        "clip": (ClipOptions, _CLIP),
        "rules": (RuleOptions, _RULES),
    }
    conversores = dict(_CAMERA) | {nome: (lambda v, c: v) for nome in aninhados}
    campos = _campos(dado, caminho, conversores)

    for nome, (cls, esquema) in aninhados.items():
        campos[nome] = _monta(cls, campos.get(nome), f"{caminho}.{nome}", esquema)

    try:
        return CameraConfig(**campos)
    except ValueError as erro:
        raise _erro(caminho, str(erro)) from erro


def monta_config(
    documento: Any,
    *,
    clips_dir: Path,
    outbox: OutboxOptions | None = None,
    cloud: CloudOptions | None = None,
    model_path: Path | None = None,
    providers: tuple[str, ...] | None = None,
    ffmpeg_bin: str = "ffmpeg",
) -> AgentConfig:
    """Junta o que é da loja (o documento) com o que é do box (os argumentos nomeados).

    `model_path` e `providers` ficam de fora do documento porque são as duas coisas do
    estágio 2 que dependem da máquina: onde o `.onnx` pousou depois do download
    (§5.2) e que provider do ONNX Runtime existe naquele hardware (§10.10). A nuvem
    manda **quais** limiares, não **onde** está o arquivo.
    """
    if not isinstance(documento, Mapping):
        raise _erro("", f"esperava um objeto na raiz, recebi {_tipo(documento)}")
    _obrigatorios(documento, "", ("schema_version", "config_version", "tenant_id", "store_id"))

    cru = _campos(documento, "", _RAIZ)

    versao = cru["schema_version"]
    if versao != SCHEMA_VERSION:
        raise _erro(
            "schema_version",
            f"documento é v{versao} e este agente fala v{SCHEMA_VERSION}. Uma major nova "
            "muda campo obrigatório: aplicar o que dá e ignorar o resto deixaria a loja "
            "rodando com metade da calibração",
        )

    cameras_cruas = cru.get("cameras")
    if not isinstance(cameras_cruas, Sequence) or isinstance(cameras_cruas, str | bytes):
        raise _erro("cameras", f"esperava uma lista de câmeras, recebi {_tipo(cameras_cruas)}")
    cameras = tuple(
        _monta_camera(camera, f"cameras[{i}]") for i, camera in enumerate(cameras_cruas)
    )

    cru_detection = cru.get("detection")
    if (
        model_path is None
        and isinstance(cru_detection, Mapping)
        and cru_detection.get("enabled") is True
    ):
        # Antes de construir `DetectionOptions`, porque a mensagem dela — "detecção
        # habilitada exige model_path" — manda procurar um campo no JSON, onde ele não
        # existe nem deve existir. A metade que falta é a do box, não a do documento.
        raise _erro(
            "detection.enabled",
            "a configuração liga a detecção, mas este box não sabe onde está o modelo. "
            "O caminho do .onnx é local (--model ou LINCE_MODEL_PATH) e não vem no "
            "documento: o que desce da nuvem é o binário com checksum (§5.2)",
        )

    detection = _monta(
        DetectionOptions,
        cru_detection,
        "detection",
        _DETECTION,
        **({"model_path": model_path} if model_path is not None else {}),
        **({"providers": providers} if providers is not None else {}),
    )

    tracking = _monta(TrackingOptions, cru.get("tracking"), "tracking", _TRACKING)

    try:
        return AgentConfig(
            tenant_id=cru["tenant_id"],
            store_id=cru["store_id"],
            config_version=cru["config_version"],
            cameras=cameras,
            clips_dir=clips_dir,
            outbox=outbox or OutboxOptions(),
            cloud=cloud or CloudOptions(),
            detection=detection,
            tracking=tracking,
            ffmpeg_bin=ffmpeg_bin,
        )
    except ValueError as erro:
        raise ConfigError(str(erro)) from erro


def le_documento(caminho: Path) -> dict[str, Any]:
    """O JSON do disco, com o nome do arquivo em qualquer erro.

    Um `JSONDecodeError` cru diz "line 42 column 7" sem dizer de qual dos arquivos —
    e quem está subindo um agente costuma ter dois abertos, o da loja e o de exemplo.
    """
    try:
        texto = caminho.read_text(encoding="utf-8")
    except OSError as erro:
        raise ConfigError(f"não consegui ler {caminho}: {erro}") from erro
    try:
        return json.loads(texto)
    except json.JSONDecodeError as erro:
        raise ConfigError(f"{caminho} não é JSON válido: {erro}") from erro


def carrega_arquivo(caminho: Path, **kwargs: Any) -> AgentConfig:
    """Atalho de `le_documento` + `monta_config`, com o arquivo na mensagem de erro."""
    try:
        return monta_config(le_documento(caminho), **kwargs)
    except ConfigError as erro:
        raise ConfigError(f"{caminho}: {erro}") from erro
