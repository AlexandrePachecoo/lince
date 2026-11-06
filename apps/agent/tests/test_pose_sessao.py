"""Sessão ONNX do RTMPose-s, sem o peso oficial.

O arquivo é um grafo mínimo montado com `onnx.helper`: a forma é a do
RTMPose-s 256×192 e o SimCC tem um pico num bin conhecido. Sem isso, o
caminho frame → recorte → sessão → decode só existiria no benchmark, que
precisa do checkpoint e não roda na suíte.

O que quebra em silêncio: abrir um WholeBody porque o nome passou, aceitar
384×288, ou tratar a segunda saída como x. A junta continua plausível e o
pulso sai no lugar errado.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import onnx
import pytest
from onnx import TensorProto, helper, numpy_helper

from lince_agent.config import PixelFormat
from lince_agent.detect.onnx import abre_sessao
from lince_agent.detect.onnx import model_version as versao_do_detector
from lince_agent.detect.state import Detection
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.onnx import (
    BINS_X,
    BINS_Y,
    NUM_JUNTAS,
    PROVIDERS,
    ModelError,
    OnnxPose,
)
from lince_agent.pose.preprocess import INPUT_ALTURA, INPUT_LARGURA

ALTURA, LARGURA = 120, 160
CAIXA_A = (20.0, 10.0, 80.0, 100.0)
CAIXA_B = (40.0, 30.0, 100.0, 110.0)
CENTRO_A = (50.0, 55.0)
CENTRO_B = (70.0, 70.0)


def imagem() -> np.ndarray:
    return np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)


def grava_modelo(
    caminho: Path,
    *,
    entrada: list | None = None,
    saidas: list[tuple[str, list]] | None = None,
    n_entradas: int = 1,
    tipo_entrada: int = TensorProto.FLOAT,
) -> Path:
    """ONNX com o contrato do piloto, ou a violação que o teste quer recusar.

    O pico do pulso esquerdo fica no bin central (192, 256) quando a forma é
    a oficial. O valor não depende do pixel: o que o teste vê é o decode
    daquele pico, não um peso treinado.
    """
    entrada = ["batch", 3, INPUT_ALTURA, INPUT_LARGURA] if entrada is None else entrada
    if saidas is None:
        # y primeiro de propósito. Quem assume a ordem das saídas troca o eixo.
        lote = entrada[0]
        saidas = [
            ("simcc_y", [lote, NUM_JUNTAS, BINS_Y]),
            ("simcc_x", [lote, NUM_JUNTAS, BINS_X]),
        ]

    entradas_vi = [
        helper.make_tensor_value_info("input", tipo_entrada, entrada),
    ]
    fonte = "input"
    nos = []
    if n_entradas == 2:
        entradas_vi.append(helper.make_tensor_value_info("input_b", tipo_entrada, entrada))
        nos.append(helper.make_node("Add", ["input", "input_b"], ["usado"]))
        fonte = "usado"

    inits = [
        numpy_helper.from_array(np.array(0, dtype=np.int64), "idx0"),
        numpy_helper.from_array(np.array([1, 1], dtype=np.int64), "uns"),
        numpy_helper.from_array(np.array([0], dtype=np.int64), "eixo0"),
    ]
    nos.extend(
        [
            helper.make_node("Shape", [fonte], ["shape_in"]),
            helper.make_node("Gather", ["shape_in", "idx0"], ["n_escalar"], axis=0),
            helper.make_node("Unsqueeze", ["n_escalar", "eixo0"], ["n_vec"]),
            helper.make_node("Concat", ["n_vec", "uns"], ["repeats"], axis=0),
        ]
    )
    saidas_vi = []
    for nome, forma in saidas:
        base = np.zeros((1, int(forma[1]), int(forma[2])), dtype=np.float32)
        if int(forma[1]) > 9 and int(forma[2]) == BINS_X:
            base[0, 9, 192] = 0.8
        if int(forma[1]) > 9 and int(forma[2]) == BINS_Y:
            base[0, 9, 256] = 0.3
        inits.append(numpy_helper.from_array(base, f"base_{nome}"))
        nos.append(helper.make_node("Tile", [f"base_{nome}", "repeats"], [nome]))
        saidas_vi.append(helper.make_tensor_value_info(nome, TensorProto.FLOAT, forma))

    grafo = helper.make_graph(nos, "rtmpose_sintetico", entradas_vi, saidas_vi, inits)
    modelo = helper.make_model(grafo, opset_imports=[helper.make_opsetid("", 13)])
    modelo.ir_version = 8
    onnx.checker.check_model(modelo)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(modelo, caminho)
    return caminho


class _Conta:
    def __init__(self, real: object) -> None:
        self._real = real
        self.lotes: list[int] = []

    def run(self, nomes, feeds):
        tensor = next(iter(feeds.values()))
        self.lotes.append(int(tensor.shape[0]))
        return self._real.run(nomes, feeds)

    def get_inputs(self):
        return self._real.get_inputs()

    def get_outputs(self):
        return self._real.get_outputs()

    def get_providers(self):
        return self._real.get_providers()


def abre(caminho: Path, **kwargs) -> tuple[OnnxPose, _Conta]:
    caixa: dict[str, _Conta] = {}

    def factory(model_path: Path, providers: tuple[str, ...]) -> _Conta:
        caixa["conta"] = _Conta(abre_sessao(model_path, providers))
        return caixa["conta"]

    pose = OnnxPose(
        caminho,
        providers=("CPUExecutionProvider",),
        session_factory=factory,
        **kwargs,
    )
    return pose, caixa["conta"]


def _pulso(pessoa, centro: tuple[float, float]) -> None:
    pulso = pessoa.juntas[9]
    assert pulso.nome == "pulso_esquerdo"
    assert (pulso.x, pulso.y) == pytest.approx(centro)
    assert pulso.confianca == pytest.approx(0.3)
    # (96, 128) é o centro do recorte, não o da caixa. Esquecer o undo_crop
    # entrega exatamente isso, com a mesma confiança.
    assert (pulso.x, pulso.y) != pytest.approx((96.0, 128.0))
    assert pessoa.confiancas[9] == pytest.approx(0.3)
    assert pessoa.juntas[0].confianca == 0.0


# --- o caminho completo ---------------------------------------------------------


def test_pico_conhecido_cai_no_centro_da_caixa(tmp_path: Path):
    caminho = grava_modelo(tmp_path / "rtmpose-s.onnx")
    pose, conta = abre(caminho)
    pessoas = pose.estimar(imagem(), [CAIXA_A, CAIXA_B])

    assert pose.lote_dinamico is True
    assert conta.lotes == [2]
    assert len(pessoas) == 2
    _pulso(pessoas[0], CENTRO_A)
    _pulso(pessoas[1], CENTRO_B)


def test_lote_fixo_faz_uma_chamada_por_caixa(tmp_path: Path):
    """O export estático declara lote 1. Empilhar as caixas estoura a sessão
    ou, se ela cortar, some com a segunda pessoa. O loop devolve as duas."""
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
        saidas=[
            ("simcc_x", [1, NUM_JUNTAS, BINS_X]),
            ("simcc_y", [1, NUM_JUNTAS, BINS_Y]),
        ],
    )
    pose, conta = abre(caminho)
    pessoas = pose.estimar(imagem(), [CAIXA_A, CAIXA_B])

    assert pose.lote_dinamico is False
    assert conta.lotes == [1, 1]
    _pulso(pessoas[0], CENTRO_A)
    _pulso(pessoas[1], CENTRO_B)


def test_lote_dinamico_e_o_fixo_concordam_na_junta(tmp_path: Path):
    dinamico = grava_modelo(tmp_path / "dinamico.onnx")
    estatico = grava_modelo(
        tmp_path / "estatico.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
        saidas=[
            ("simcc_y", [1, NUM_JUNTAS, BINS_Y]),
            ("simcc_x", [1, NUM_JUNTAS, BINS_X]),
        ],
    )
    pose_d, _ = abre(dinamico)
    pose_e, _ = abre(estatico)
    a = pose_d.estimar(imagem(), [CAIXA_A])[0].juntas
    b = pose_e.estimar(imagem(), [CAIXA_A])[0].juntas
    assert [(j.x, j.y, j.confianca) for j in a] == pytest.approx(
        [(j.x, j.y, j.confianca) for j in b]
    )


def test_frame_e_deteccao_usam_o_mesmo_espaco(tmp_path: Path):
    """A caixa do detector e o frame do ffmpeg são o que a pose vai receber
    quando o estágio ligar. Um caminho que só aceita ndarray deixa essa
    costura para descobrir na loja."""
    caminho = grava_modelo(tmp_path / "rtmpose-s.onnx", entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA])
    pose, _ = abre(caminho)
    img = imagem()
    frame = Frame(
        data=np.ascontiguousarray(img).tobytes(),
        width=LARGURA,
        height=ALTURA,
        pixel_format=PixelFormat.BGR24,
        sequence=1,
        received_at=0.0,
    )
    deteccao = Detection(class_id=0, score=0.9, x1=20.0, y1=10.0, x2=80.0, y2=100.0)
    do_array = pose.estimar(img, [CAIXA_A])[0]
    do_frame = pose.estimar(frame, [deteccao])[0]
    assert (do_frame.juntas[9].x, do_frame.juntas[9].y) == pytest.approx(
        (do_array.juntas[9].x, do_array.juntas[9].y)
    )


def test_lista_ou_tupla_de_quatro_numeros_e_uma_caixa(tmp_path: Path):
    """Uma caixa solta não é quatro caixas. Tratar cada número como caixa
    estoura no recorte, ou pior, produz um tensor sem pessoa."""
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
    )
    pose, conta = abre(caminho)
    da_lista = pose.estimar(imagem(), [20.0, 10.0, 80.0, 100.0])
    da_tupla = pose.estimar(imagem(), (20, 10, 80, 100))
    assert conta.lotes == [1, 1]
    assert len(da_lista) == len(da_tupla) == 1
    _pulso(da_lista[0], CENTRO_A)
    _pulso(da_tupla[0], CENTRO_A)


def test_inferir_recusa_canal_ou_tamanho_errado(tmp_path: Path):
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
    )
    pose, conta = abre(caminho)
    with pytest.raises(ModelError, match=r"recebi \(1, 3, 64, 48\)"):
        pose.inferir(np.zeros((1, 3, 64, 48), np.float32))
    with pytest.raises(ModelError, match=r"recebi \(1, 1, 256, 192\)"):
        pose.inferir(np.zeros((1, 1, INPUT_ALTURA, INPUT_LARGURA), np.float32))
    assert conta.lotes == []


def test_sem_caixa_nao_chama_a_sessao(tmp_path: Path):
    caminho = grava_modelo(tmp_path / "rtmpose-s.onnx")
    pose, conta = abre(caminho)
    assert pose.estimar(imagem(), []) == ()
    assert conta.lotes == []


def test_versao_e_o_provider_inicializado_nao_o_anunciado(tmp_path: Path, caplog):
    """`get_available_providers` é o que o pacote compilou. No onnxruntime-gpu
    o CUDA entra nessa lista mesmo sem `libcuda.so.1`, e a sessão inicializa
    em CPU. `info().provider` tem que ser o que `get_providers` devolveu, e a
    queda para o próximo da lista tem que sair no aviso. Usar o anunciado
    como se fosse o obtido esconde o box que caiu para CPU (§10.10)."""
    import onnxruntime as ort

    caminho = grava_modelo(tmp_path / "rtmpose-s.onnx")
    with caplog.at_level("WARNING"):
        pose = OnnxPose(caminho, providers=PROVIDERS)
    sessao = abre_sessao(caminho, PROVIDERS)
    anunciados = ort.get_available_providers()
    inicializado = sessao.get_providers()[0]
    info = pose.info()

    assert info.provider == inicializado
    assert info.provider in PROVIDERS
    assert info.provider in anunciados
    assert info.model_version == versao_do_detector(caminho)
    assert info.input_width == INPUT_LARGURA
    assert info.input_height == INPUT_ALTURA
    assert info.lote_dinamico is True

    quedas = [
        registro.getMessage()
        for registro in caplog.records
        if "indisponível" in registro.getMessage()
    ]
    if info.provider != PROVIDERS[0]:
        assert len(quedas) == 1
        assert PROVIDERS[0] in quedas[0]
        assert info.provider in quedas[0]
    else:
        assert quedas == []


def test_sessao_fechada_recusa_a_proxima_caixa(tmp_path: Path):
    caminho = grava_modelo(tmp_path / "rtmpose-s.onnx")
    pose, _ = abre(caminho)
    pose.close()
    with pytest.raises(ModelError, match="fechada"):
        pose.estimar(imagem(), [CAIXA_A])


# --- a forma, na subida ---------------------------------------------------------


def test_entrada_de_outro_tamanho_e_recusada(tmp_path: Path):
    """384×288 é outro RTMPose. A sessão aceita o tensor, o decode projeta, e
    o pulso sai na escala errada sem uma exceção no frame."""
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, 384, 288],
        saidas=[
            ("simcc_x", [1, NUM_JUNTAS, 576]),
            ("simcc_y", [1, NUM_JUNTAS, 768]),
        ],
    )
    with pytest.raises(ModelError, match="384"):
        abre(caminho)


def test_entrada_quadrada_do_detector_e_recusada(tmp_path: Path):
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, 640, 640],
    )
    with pytest.raises(ModelError, match="640"):
        abre(caminho)


def test_eixo_espacial_dinamico_e_recusado(tmp_path: Path):
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=["batch", 3, "altura", INPUT_LARGURA],
    )
    with pytest.raises(ModelError, match="dinâmico"):
        abre(caminho)


def test_lote_fixo_diferente_de_um_e_recusado(tmp_path: Path):
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[4, 3, INPUT_ALTURA, INPUT_LARGURA],
        saidas=[
            ("simcc_x", [4, NUM_JUNTAS, BINS_X]),
            ("simcc_y", [4, NUM_JUNTAS, BINS_Y]),
        ],
    )
    with pytest.raises(ModelError, match="lote 4"):
        abre(caminho)


def test_wholebody_pela_contagem_de_juntas_e_recusado(tmp_path: Path):
    """Renomear o arquivo passa na trava do nome. 133 canais decodificados
    como 17 não estouram: o pulso sai no índice errado."""
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
        saidas=[
            ("simcc_x", [1, 133, BINS_X]),
            ("simcc_y", [1, 133, BINS_Y]),
        ],
    )
    with pytest.raises(ModelError, match="133"):
        abre(caminho)


def test_split_ratio_esquecido_e_recusado(tmp_path: Path):
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
        saidas=[
            ("simcc_x", [1, NUM_JUNTAS, INPUT_LARGURA]),
            ("simcc_y", [1, NUM_JUNTAS, INPUT_ALTURA]),
        ],
    )
    with pytest.raises(ModelError, match="2.0"):
        abre(caminho)


def test_nome_do_eixo_que_contradiz_os_bins_e_recusado(tmp_path: Path):
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
        saidas=[
            ("simcc_x", [1, NUM_JUNTAS, BINS_Y]),
            ("simcc_y", [1, NUM_JUNTAS, BINS_X]),
        ],
    )
    with pytest.raises(ModelError, match="nome diz"):
        abre(caminho)


def test_saida_unica_e_recusada(tmp_path: Path):
    caminho = grava_modelo(
        tmp_path / "rtmpose-s.onnx",
        entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA],
        saidas=[("simcc_x", [1, NUM_JUNTAS, BINS_X])],
    )
    with pytest.raises(ModelError, match="duas saídas"):
        abre(caminho)


def test_duas_entradas_sao_recusadas(tmp_path: Path):
    caminho = grava_modelo(tmp_path / "rtmpose-s.onnx", n_entradas=2)
    with pytest.raises(ModelError, match="uma entrada"):
        abre(caminho)


def test_entrada_float16_e_recusada(tmp_path: Path):
    caminho = grava_modelo(tmp_path / "rtmpose-s.onnx", tipo_entrada=TensorProto.FLOAT16)
    with pytest.raises(ModelError, match="float16"):
        abre(caminho)


# --- o nome, antes da sessão ----------------------------------------------------


@pytest.mark.parametrize(
    "nome",
    ["yolov8n-pose.onnx", "rtmpose-m_coco-wholebody.onnx", "rtmpose-s_halpe26.onnx"],
)
def test_nome_fora_do_piloto_nao_abre_sessao(tmp_path: Path, nome: str):
    abriu: list[object] = []

    def factory(*args):
        abriu.append(args)
        raise AssertionError("a sessão não pode abrir")

    with pytest.raises(ModelError):
        OnnxPose(tmp_path / nome, session_factory=factory)
    assert abriu == []


def test_arquivo_valido_com_nome_wholebody_nao_abre(tmp_path: Path):
    caminho = grava_modelo(tmp_path / "rtmpose-s_coco-wholebody.onnx")
    abriu: list[object] = []

    def factory(*args):
        abriu.append(args)
        raise AssertionError("a sessão não pode abrir")

    with pytest.raises(ModelError, match="WholeBody"):
        OnnxPose(caminho, session_factory=factory)
    assert abriu == []


def test_diretorio_halpe_nao_bloqueia_o_arquivo_certo(tmp_path: Path):
    pasta = tmp_path / "halpe-comparativo"
    caminho = grava_modelo(pasta / "rtmpose-s.onnx", entrada=[1, 3, INPUT_ALTURA, INPUT_LARGURA])
    pose, _ = abre(caminho)
    assert pose.info().model_version.startswith("rtmpose-s@")


def test_modelo_ausente_e_recusado(tmp_path: Path):
    with pytest.raises(ModelError, match="não encontrado"):
        OnnxPose(tmp_path / "rtmpose-s.onnx", providers=("CPUExecutionProvider",))


def test_providers_vazios_sao_recusados(tmp_path: Path):
    caminho = tmp_path / "rtmpose-s.onnx"
    caminho.write_bytes(b"nao chega a abrir")
    with pytest.raises(ModelError, match="providers"):
        OnnxPose(caminho, providers=())


class _No:
    def __init__(self, name: str, shape: list, tipo: str = "tensor(float)") -> None:
        self.name = name
        self.shape = shape
        self.type = tipo


class _SessaoCpu:
    def get_inputs(self):
        return [_No("input", [1, 3, INPUT_ALTURA, INPUT_LARGURA])]

    def get_outputs(self):
        return [
            _No("simcc_x", [1, NUM_JUNTAS, BINS_X]),
            _No("simcc_y", [1, NUM_JUNTAS, BINS_Y]),
        ]

    def get_providers(self):
        return ["CPUExecutionProvider"]


def test_avisa_quando_o_cuda_pedido_cai_para_cpu(tmp_path: Path, caplog):
    """Pedido não é obtido. Sem este aviso, o benchmark na GPU do fundador
    pode estar medindo CPU com o rótulo de CUDA."""
    caminho = tmp_path / "rtmpose-s.onnx"
    caminho.write_bytes(b"a sessao e duble; o hash nao liga para o conteudo")
    with caplog.at_level("WARNING"):
        pose = OnnxPose(
            caminho,
            providers=("CUDAExecutionProvider", "CPUExecutionProvider"),
            session_factory=lambda *_args: _SessaoCpu(),
        )
    assert pose.info().provider == "CPUExecutionProvider"
    assert "CUDAExecutionProvider" in caplog.text
