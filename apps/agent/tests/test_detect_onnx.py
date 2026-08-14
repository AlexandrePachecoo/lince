"""O detector ONNX: a fiação entre letterbox, sessão, decodificação e supressão.

Quase tudo aqui roda **sem modelo nenhum**, contra uma sessão dublê que devolve um
tensor fabricado à mão. Isso é de propósito: o caminho que leva de "o modelo disse
alguma coisa" até "esta caixa está nestas coordenadas do frame" é o que produz erro
silencioso, e ele não precisa de 35 MB de pesos para ser testado.

O modelo de verdade fica sob o marcador `modelo`, e prova só o que o dublê não pode:
que o arquivo abre, que a forma declarada bate e que a inferência roda.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from deteccoes import frame

from lince_agent.config import DetectionOptions
from lince_agent.detect.onnx import ModelError, OnnxDetector, model_version

LADO = 640
ANCORAS = 80 * 80 + 40 * 40 + 20 * 20
CLASSES = 80


class _Entrada:
    def __init__(self, name: str, shape: list) -> None:
        self.name = name
        self.shape = shape


class SessaoFalsa:
    """Uma sessão do ONNX Runtime, sem o ONNX Runtime."""

    def __init__(
        self,
        saida: np.ndarray | None = None,
        *,
        shape: list | None = None,
        entradas: int = 1,
        provider: str = "CPUExecutionProvider",
    ) -> None:
        self._saida = np.zeros((1, ANCORAS, 5 + CLASSES), np.float32) if saida is None else saida
        self._shape = shape or [1, 3, LADO, LADO]
        self._entradas = entradas
        self._provider = provider
        self.recebidos: list[np.ndarray] = []

    def get_inputs(self) -> list[_Entrada]:
        return [_Entrada(f"images{i or ''}", self._shape) for i in range(self._entradas)]

    def get_providers(self) -> list[str]:
        return [self._provider]

    def run(self, _saidas, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.recebidos.append(next(iter(feeds.values())))
        return [self._saida]


def modelo_falso(tmp_path: Path) -> Path:
    caminho = tmp_path / "yolox_s.onnx"
    caminho.write_bytes(b"nao e um onnx de verdade, e nao precisa ser")
    return caminho


def detector(tmp_path: Path, sessao: SessaoFalsa, **kwargs) -> OnnxDetector:
    opcoes = DetectionOptions(enabled=True, model_path=modelo_falso(tmp_path), **kwargs)
    return OnnxDetector(modelo_falso(tmp_path), opcoes, session_factory=lambda *_args: sessao)


def saida_com_pessoa(ancora: int, *, objectness: float = 0.9, prob: float = 0.8) -> np.ndarray:
    """Saída crua do YOLOX com uma pessoa numa âncora escolhida a dedo."""
    bruto = np.zeros((1, ANCORAS, 5 + CLASSES), dtype=np.float32)
    bruto[0, ancora, 0:2] = 0.0
    bruto[0, ancora, 2:4] = np.log(2.0)
    bruto[0, ancora, 4] = objectness
    bruto[0, ancora, 5] = prob
    return bruto


# --- identidade do modelo -------------------------------------------------------


def test_model_version_junta_nome_e_checksum(tmp_path: Path):
    """O nome sozinho mente: dois boxes com `yolox_s.onnx` podem ter arquivos
    diferentes depois de um rollout parcial (R-7), e é isso que o campo do heartbeat
    existe para revelar."""
    caminho = modelo_falso(tmp_path)
    versao = model_version(caminho)
    assert versao.startswith("yolox_s@")
    assert len(versao.split("@")[1]) == 12


def test_model_version_muda_quando_o_arquivo_muda(tmp_path: Path):
    caminho = modelo_falso(tmp_path)
    antes = model_version(caminho)
    caminho.write_bytes(b"outro conteudo")
    assert model_version(caminho) != antes


def test_modelo_ausente_e_recusado(tmp_path: Path):
    with pytest.raises(ModelError, match="não encontrado"):
        OnnxDetector(tmp_path / "nao-existe.onnx", DetectionOptions())


# --- a subida -------------------------------------------------------------------


def test_info_reporta_o_provider_que_de_fato_pegou(tmp_path: Path):
    """Pedido não é obtido: um box com o driver quebrado aceita `CUDAExecutionProvider`
    na lista e roda em CPU sem dizer nada. É o dado que fecha a pendência §10.10 com
    medição em vez de palpite."""
    unidade = detector(tmp_path, SessaoFalsa(provider="CPUExecutionProvider"))
    info = unidade.info()
    assert info is not None
    assert info.provider == "CPUExecutionProvider"
    assert info.input_size == LADO


def test_avisa_quando_cai_para_o_segundo_provider(tmp_path: Path, caplog):
    """ "O box está lento" e "o box caiu para CPU" são problemas diferentes, e sem este
    aviso na subida ninguém distingue um do outro."""
    with caplog.at_level("WARNING"):
        detector(
            tmp_path,
            SessaoFalsa(provider="CPUExecutionProvider"),
            providers=("CUDAExecutionProvider", "CPUExecutionProvider"),
        )
    assert "CUDAExecutionProvider" in caplog.text


def test_entrada_declarada_divergente_da_configuracao_e_recusada(tmp_path: Path):
    """Um modelo de 416 com `input_size=640` não estoura: a grade de âncoras não bate e
    o decode recusa — mas só no primeiro frame, com as câmeras já no ar."""
    with pytest.raises(ModelError, match="declara entrada de 416"):
        detector(tmp_path, SessaoFalsa(shape=[1, 3, 416, 416]))


def test_entrada_nao_quadrada_e_recusada(tmp_path: Path):
    with pytest.raises(ModelError, match="não quadrada"):
        detector(tmp_path, SessaoFalsa(shape=[1, 3, 640, 480]))


def test_entrada_dinamica_usa_a_configuracao(tmp_path: Path):
    """Exportação com lado dinâmico traz string no lugar do número. Aí a configuração é
    a única fonte, e não há o que conferir."""
    unidade = detector(tmp_path, SessaoFalsa(shape=["batch", 3, "height", "width"]))
    info = unidade.info()
    assert info is not None and info.input_size == LADO


def test_modelo_de_varias_entradas_e_recusado(tmp_path: Path):
    with pytest.raises(ModelError, match="uma entrada só"):
        detector(tmp_path, SessaoFalsa(entradas=2))


# --- a inferência ---------------------------------------------------------------


def test_alimenta_a_sessao_com_o_tensor_letterboxado(tmp_path: Path):
    sessao = SessaoFalsa()
    detector(tmp_path, sessao).detect(frame(1))
    assert sessao.recebidos[0].shape == (1, 3, LADO, LADO)
    assert sessao.recebidos[0].dtype == np.float32


def test_caixa_sai_nas_coordenadas_do_frame_da_camera(tmp_path: Path):
    """Âncora 81 do nível de stride 8 decodifica para (0, 0, 16, 16) no espaço do
    modelo. Com o letterbox do YOLOX — canto superior esquerdo, escala 1.0 — é a mesma
    caixa no frame da câmera. É esta igualdade que o §3.4 vai assumir ao testar a
    travessia contra uma linha desenhada sobre o frame."""
    unidade = detector(tmp_path, SessaoFalsa(saida_com_pessoa(81)))
    (deteccao,) = unidade.detect(frame(1))

    assert deteccao.class_id == 0
    assert deteccao.score == pytest.approx(0.72)
    assert (deteccao.x1, deteccao.y1, deteccao.x2, deteccao.y2) == pytest.approx(
        (0.0, 0.0, 16.0, 16.0)
    )


def test_caixa_na_faixa_cinza_e_recortada_no_frame(tmp_path: Path):
    """As 160 linhas de preenchimento não existem no frame da câmera. Uma detecção ali
    é ruído do modelo, e deixá-la sair com y = 504 entregaria ao §3.4 um ponto fora do
    frame para testar contra um polígono."""
    ancora = 63 * 80 + 10  # célula (10, 63) do nível de stride 8: centro em y = 504
    unidade = detector(tmp_path, SessaoFalsa(saida_com_pessoa(ancora)))
    (deteccao,) = unidade.detect(frame(1))

    assert deteccao.y1 == 480.0 and deteccao.y2 == 480.0


def test_abaixo_do_limiar_nao_vira_deteccao(tmp_path: Path):
    unidade = detector(
        tmp_path, SessaoFalsa(saida_com_pessoa(81, objectness=0.5, prob=0.2)), score_threshold=0.35
    )
    assert unidade.detect(frame(1)) == ()


def test_classe_fora_do_interesse_e_descartada(tmp_path: Path):
    """O MVP só precisa de pessoas."""
    bruto = np.zeros((1, ANCORAS, 5 + CLASSES), dtype=np.float32)
    bruto[0, 81, 2:4] = np.log(2.0)
    bruto[0, 81, 4] = 0.9
    bruto[0, 81, 5 + 24] = 0.9  # 24 é `backpack` no COCO

    unidade = detector(tmp_path, SessaoFalsa(bruto), classes=(0,))
    assert unidade.detect(frame(1)) == ()


def test_frame_sem_ninguem_devolve_vazio(tmp_path: Path):
    """O caso comum numa loja de bairro, não a exceção."""
    assert detector(tmp_path, SessaoFalsa()).detect(frame(1)) == ()


def test_close_solta_a_sessao(tmp_path: Path):
    """A sessão segura arenas de memória e, num box com GPU, VRAM."""
    unidade = detector(tmp_path, SessaoFalsa())
    unidade.close()
    with pytest.raises(AttributeError):
        unidade.detect(frame(1))


# --- com o modelo de verdade ----------------------------------------------------


@pytest.mark.modelo
def test_o_arquivo_abre_e_declara_a_entrada_esperada(model_path: Path):
    unidade = OnnxDetector(model_path, DetectionOptions(enabled=True, model_path=model_path))
    try:
        info = unidade.info()
        assert info is not None
        assert info.input_size == LADO
        assert info.provider.endswith("ExecutionProvider")
        assert info.model_version.startswith(model_path.stem)
    finally:
        unidade.close()


@pytest.mark.modelo
def test_inferencia_de_verdade_roda_num_frame_cru(model_path: Path):
    """Prova o que o dublê não pode: que a forma que o `preprocess` produz é aceita
    pelo grafo e que a saída passa pelo `decode_yolox` sem recusa. Um frame preto não
    deve produzir pessoa nenhuma."""
    unidade = OnnxDetector(model_path, DetectionOptions(enabled=True, model_path=model_path))
    try:
        assert unidade.detect(frame(1)) == ()
    finally:
        unidade.close()
