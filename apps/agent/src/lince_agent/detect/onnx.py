"""O único módulo que fala com o modelo.

ONNX Runtime, e não um framework de treino, por três motivos que valem além da
preferência:

**O modelo vira um arquivo só.** É exatamente o que o `GET /v1/models/current` do §5.2
distribui — "versão de modelo alvo + URL assinada de download + checksum" — e o que o
ADR-006 precisa para fazer rollback. Um checkpoint acoplado a um framework transforma
"trocar o modelo" em "trocar a imagem".

**O fallback de GPU vira configuração.** O §3.2 deixa em aberto o que fazer quando a
placa cai, e o §10.10 registra a pendência. Com uma lista de providers, a resposta é
dado: o runtime tenta CUDA, não consegue, usa CPU, e o heartbeat reporta qual pegou.
Sem nenhum `if gpu` — que é o que o §3.9 exige.

**A imagem do box fica enxuta.** O agente inteiro depende hoje de `numpy` e `redis`.
Um framework de treino traria dois gigabytes de coisa que só serve para treinar, num
desktop de mercado de bairro.

Uma armadilha de licença, que é metade do motivo da escolha: a licença do *runtime*
não é a licença do *modelo*. O ONNX Runtime é MIT, mas pesos exportados da linhagem
Ultralytics são AGPL-3.0, e adotá-los desfaria o ponto inteiro. O default do agente é
YOLOX (Apache-2.0).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lince_agent.config import DetectionOptions
from lince_agent.detect.postprocess import decode_yolox, filtra, melhor_classe, nms
from lince_agent.detect.preprocess import (
    YOLOX,
    PreprocessSpec,
    frame_to_bgr,
    letterbox,
    undo_letterbox,
)
from lince_agent.detect.state import Detection, DetectorInfo
from lince_agent.ffmpeg.rawframe import Frame

log = logging.getLogger(__name__)

_CHECKSUM_PREFIXO = 12
"""Caracteres do sha256 no `model_version`. Doze hexadecimais são 48 bits — suficiente
para identificar o arquivo em log e heartbeat sem poluir a linha."""


class ModelError(RuntimeError):
    """O modelo não pôde ser carregado ou não é o que a configuração diz."""


def model_version(model_path: Path) -> str:
    """Identidade do arquivo: nome mais o começo do sha256.

    O nome sozinho mente — dois boxes com `yolox_s.onnx` podem ter arquivos diferentes
    depois de um rollout parcial (R-7), e é justamente essa a situação que o campo
    `model_version` do heartbeat existe para revelar. O checksum é o mesmo que o §5.2
    entrega junto do download.
    """
    digest = hashlib.sha256()
    with model_path.open("rb") as arquivo:
        for bloco in iter(lambda: arquivo.read(1 << 20), b""):
            digest.update(bloco)
    return f"{model_path.stem}@{digest.hexdigest()[:_CHECKSUM_PREFIXO]}"


class OnnxDetector:
    """Detector do §3.2 sobre uma sessão do ONNX Runtime."""

    def __init__(
        self,
        model_path: Path,
        options: DetectionOptions,
        *,
        spec: PreprocessSpec = YOLOX,
        session_factory: Callable[[Path, tuple[str, ...]], Any] | None = None,
    ) -> None:
        if not model_path.is_file():
            raise ModelError(f"modelo não encontrado em {model_path}")

        self._options = options
        self._spec = spec
        self._session = (session_factory or _abre_sessao)(model_path, options.providers)

        entradas = self._session.get_inputs()
        if len(entradas) != 1:
            raise ModelError(
                f"esperava um modelo de uma entrada só, {model_path.name} tem {len(entradas)}"
            )
        self._input_name = entradas[0].name
        self._input_size = _resolve_input_size(entradas[0].shape, options.input_size, model_path)

        provider = self._session.get_providers()[0]
        self._info = DetectorInfo(
            model_version=model_version(model_path),
            input_size=self._input_size,
            provider=provider,
            classes=options.classes,
        )
        # O provider pedido não é o provider obtido: um box com o driver quebrado aceita
        # `CUDAExecutionProvider` na lista e roda em CPU sem dizer nada. Registrar na
        # subida é o que transforma "o box está lento" em "o box caiu para CPU" (§10.10).
        if provider != options.providers[0]:
            log.warning(
                "provider %s indisponível; a inferência vai rodar em %s",
                options.providers[0],
                provider,
            )
        log.info(
            "modelo %s carregado em %s (entrada %d)",
            self._info.model_version,
            provider,
            self._input_size,
        )

    def detect(self, frame: Frame) -> tuple[Detection, ...]:
        tensor, caixa = letterbox(frame_to_bgr(frame), size=self._input_size, spec=self._spec)
        saida = self._session.run(None, {self._input_name: tensor})[0]

        caixas, scores = decode_yolox(saida, input_size=self._input_size)
        class_ids, confiancas = melhor_classe(scores)
        caixas, class_ids, confiancas = filtra(
            caixas,
            class_ids,
            confiancas,
            score_threshold=self._options.score_threshold,
            classes=self._options.classes,
        )

        # A supressão roda no espaço do modelo, antes de desfazer o letterbox: a IoU é
        # invariante a escala e translação uniformes, e suprimir primeiro deixa menos
        # caixa para converter.
        mantidos = nms(caixas, confiancas, class_ids, iou_threshold=self._options.iou_threshold)
        finais = undo_letterbox(caixas[mantidos], caixa)

        return tuple(
            Detection(
                class_id=int(class_ids[indice]),
                score=float(confiancas[indice]),
                x1=float(x1),
                y1=float(y1),
                x2=float(x2),
                y2=float(y2),
            )
            for indice, (x1, y1, x2, y2) in zip(mantidos, finais, strict=True)
        )

    def info(self) -> DetectorInfo | None:
        return self._info

    def reconfigure(self, options: DetectionOptions) -> None:
        """Troca os limiares do pós-processamento a partir do frame seguinte (§5.2).

        Basta substituir o objeto: `detect` lê `score_threshold`, `classes` e
        `iou_threshold` a cada chamada. A sessão do ONNX Runtime e o `PreprocessSpec`
        não são tocados — são eles que o `config_diff` classifica como estrutural, e um
        `input_size` diferente aqui deslocaria toda caixa em silêncio.
        """
        self._options = options

    def close(self) -> None:
        self._session = None


def _abre_sessao(model_path: Path, providers: tuple[str, ...]):
    """Import adiado de propósito.

    `onnxruntime` leva perto de um segundo para importar e reserva arenas de memória na
    subida. Um agente com a detecção desligada (§3.9: máquina sem GPU, box de
    desenvolvimento) não deve pagar isso, e a suíte de testes — que sobe o runtime
    centenas de vezes — muito menos.
    """
    import onnxruntime as ort

    disponiveis = set(ort.get_available_providers())
    pedidos = [provider for provider in providers if provider in disponiveis]
    if not pedidos:
        raise ModelError(
            f"nenhum dos providers {providers} está disponível nesta instalação do "
            f"ONNX Runtime (tem {sorted(disponiveis)})"
        )
    return ort.InferenceSession(str(model_path), providers=pedidos)


def _resolve_input_size(shape: list[Any], configurado: int, model_path: Path) -> int:
    """Confere o `input_size` da configuração contra o que o próprio `.onnx` declara.

    Um modelo de 416 rodando com `input_size=640` não estoura: a grade de âncoras não
    bate, e o `decode_yolox` recusa — mas só no primeiro frame, já com as câmeras no ar.
    Conferir na subida é a diferença entre um erro na inicialização e um agente que
    ingere e nunca detecta.
    """
    # Exportação com lote ou lado dinâmico traz string ou None no lugar do número.
    laterais = [valor for valor in shape[2:] if isinstance(valor, int) and valor > 0]
    if not laterais:
        return configurado

    if len(set(laterais)) != 1:
        raise ModelError(f"{model_path.name} declara entrada não quadrada {shape}")
    declarado = laterais[0]
    if declarado != configurado:
        raise ModelError(
            f"{model_path.name} declara entrada de {declarado} e a configuração diz "
            f"{configurado}. Ajuste DetectionOptions.input_size — decodificar com o "
            f"tamanho errado produz caixas plausíveis e erradas"
        )
    return declarado
