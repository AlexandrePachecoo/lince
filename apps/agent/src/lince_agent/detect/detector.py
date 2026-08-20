"""O contrato do estágio 2.

Existe para que o `DetectorWorker`, o `AgentRuntime` e — depois — o tracking do §3.3
não saibam que o modelo é ONNX. Trocar de runtime, de família de modelo ou rodar sem
modelo nenhum tem que ser troca de implementação, não de fiação.
"""

from __future__ import annotations

from typing import Protocol

from lince_agent.config import DetectionOptions
from lince_agent.detect.state import Detection, DetectorInfo
from lince_agent.ffmpeg.rawframe import Frame


class Detector(Protocol):
    """Recebe um frame, devolve caixas. Sem estado entre frames — o que é histórico
    pertence ao tracking (§3.3), e misturar os dois foi o que o ADR-002 separou."""

    def detect(self, frame: Frame) -> tuple[Detection, ...]:
        """Roda a inferência. Pode levantar: o worker conta o erro e segue com o
        próximo frame, porque falha numa câmera não pode parar as outras."""
        ...

    def info(self) -> DetectorInfo | None:
        """Identidade do modelo carregado, ou `None` quando não há modelo."""
        ...

    def reconfigure(self, options: DetectionOptions) -> None:
        """Troca os limiares de pós-processamento com o agente em pé (§5.2).

        Só o que é lido a cada `detect` — `score_threshold`, `iou_threshold`, `classes`.
        `model_path`, `providers` e `input_size` já viraram sessão do ONNX Runtime na
        subida e são estruturais; quem separa os dois casos é o `config_diff`, e um
        detector nunca recebe aqui uma mudança que não saiba aplicar.
        """
        ...

    def close(self) -> None: ...


class NullDetector:
    """Não detecta nada, e isso é uma resposta legítima.

    **Código de produção, não dublê de teste.** O R-3 prevê câmeras que a loja já tem e
    que não servem para IA — ângulo, altura, contraluz na porta. Elas continuam
    ingerindo, alimentando o buffer circular e respondendo a gatilho manual; só não
    gastam inferência. É também o que o agente usa quando não há modelo no disco, o que
    mantém o §3.9 verdadeiro: o mesmo artefato sobe numa máquina sem GPU.
    """

    def detect(self, frame: Frame) -> tuple[Detection, ...]:
        return ()

    def info(self) -> DetectorInfo | None:
        return None

    def reconfigure(self, options: DetectionOptions) -> None:
        """Não há limiar para trocar em quem não detecta nada."""
        return None

    def close(self) -> None:
        return None
