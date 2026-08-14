"""Saída crua do modelo → caixas suprimidas.

Os números esperados aqui são calculados à mão, não pela própria grade do módulo: um
teste que reusa `_grade` para montar a expectativa concorda com qualquer erro que a
grade tenha.
"""

from __future__ import annotations

import numpy as np
import pytest

from lince_agent.detect.postprocess import (
    CLASSE_PESSOA,
    PostprocessError,
    decode_yolox,
    filtra,
    iou,
    melhor_classe,
    nms,
)

LADO = 640
ANCORAS = 80 * 80 + 40 * 40 + 20 * 20  # 8400: os três níveis da FPN
CLASSES = 80


def saida(ancoras: int = ANCORAS) -> np.ndarray:
    return np.zeros((ancoras, 5 + CLASSES), dtype=np.float32)


# --- decodificação --------------------------------------------------------------


def test_uma_entrada_de_640_produz_as_ancoras_dos_tres_strides():
    caixas, scores = decode_yolox(saida(), input_size=LADO)
    assert caixas.shape == (ANCORAS, 4)
    assert scores.shape == (ANCORAS, CLASSES)


def test_decodifica_caixa_no_lugar_calculado_a_mao():
    """Âncora 81 do nível de stride 8: 80 células por linha, então é a célula (1, 1).
    Com deslocamento zero o centro cai em (8, 8), e `w = exp(log 2) × 8 = 16` dá a
    caixa (0, 0, 16, 16). Errar a soma da grade move a caixa sem levantar nada."""
    bruto = saida()
    bruto[81, 0:2] = 0.0
    bruto[81, 2:4] = np.log(2.0)

    caixas, _ = decode_yolox(bruto, input_size=LADO)
    assert caixas[81].tolist() == pytest.approx([0.0, 0.0, 16.0, 16.0])


def test_decodifica_no_nivel_de_stride_32():
    """Âncora 8000 é a primeira do último nível (6400 + 1600 anteriores), célula
    (0, 0) de uma grade de 20. Se os strides fossem concatenados noutra ordem, esta
    caixa sairia 4x menor e no lugar errado."""
    bruto = saida()
    bruto[8000, 0:2] = 0.5
    bruto[8000, 2:4] = 0.0

    caixas, _ = decode_yolox(bruto, input_size=LADO)
    # centro (0,5 + 0) × 32 = 16; lado exp(0) × 32 = 32.
    assert caixas[8000].tolist() == pytest.approx([0.0, 0.0, 32.0, 32.0])


def test_score_e_objectness_vezes_a_classe():
    """Objectness sozinha aceita a caixa que contém *algo*; probabilidade de classe
    sozinha aceita a caixa vazia cuja melhor aposta é "pessoa". O produto é o que
    exige as duas coisas."""
    bruto = saida()
    bruto[10, 4] = 0.8
    bruto[10, 5 + CLASSE_PESSOA] = 0.5

    _, scores = decode_yolox(bruto, input_size=LADO)
    assert scores[10, CLASSE_PESSOA] == pytest.approx(0.4)


def test_aceita_com_e_sem_dimensao_de_lote():
    """O exportador do YOLOX entrega (1, âncoras, 85). Quem chama não deve precisar
    saber disso."""
    com_lote = decode_yolox(saida()[np.newaxis, ...], input_size=LADO)[0]
    sem_lote = decode_yolox(saida(), input_size=LADO)[0]
    assert np.array_equal(com_lote, sem_lote)


def test_a_grade_memoizada_nao_aceita_escrita():
    """A grade é construída uma vez e compartilhada por todo frame de toda câmera.
    Quem escrevesse nela envenenaria a decodificação de todas as inferências
    seguintes, sem nenhum sinal."""
    from lince_agent.detect.postprocess import _grade

    centros, strides = _grade(LADO)
    with pytest.raises(ValueError, match="read-only"):
        centros[0, 0] = 99.0
    with pytest.raises(ValueError, match="read-only"):
        strides[0, 0] = 99.0


def test_nao_escreve_na_saida_do_modelo():
    """O ONNX Runtime pode reaproveitar o buffer de saída entre inferências. Decodificar
    no lugar corromperia o frame seguinte — e só o seguinte, que é o pior tipo de bug
    para reproduzir."""
    bruto = saida()
    bruto[5, 0:4] = 0.25
    copia = bruto.copy()
    decode_yolox(bruto, input_size=LADO)
    assert np.array_equal(bruto, copia)


def test_numero_de_ancoras_divergente_e_recusado():
    """Modelo de 416 rodando com `input_size=640` decodificaria caixas plausíveis e
    completamente erradas. A mensagem tem que dizer que os dois estão divergentes."""
    with pytest.raises(PostprocessError, match="divergentes"):
        decode_yolox(saida(ancoras=3549), input_size=LADO)


def test_entrada_nao_divisivel_pelo_stride_e_recusada():
    with pytest.raises(PostprocessError, match="múltiplo"):
        decode_yolox(saida(), input_size=100)


@pytest.mark.parametrize("forma", [(8400,), (8400, 4), (2, 8400, 85)])
def test_forma_inesperada_e_recusada(forma):
    with pytest.raises(PostprocessError, match="5 \\+ classes"):
        decode_yolox(np.zeros(forma, dtype=np.float32), input_size=LADO)


# --- redução e filtros ----------------------------------------------------------


def test_melhor_classe_escolhe_o_argmax():
    scores = np.array([[0.1, 0.7, 0.2], [0.9, 0.0, 0.05]], dtype=np.float32)
    class_ids, confiancas = melhor_classe(scores)
    assert class_ids.tolist() == [1, 0]
    assert confiancas.tolist() == pytest.approx([0.7, 0.9])


def test_melhor_classe_de_frame_vazio_nao_estoura():
    """Frame sem ninguém é o caso comum numa loja de bairro, não a exceção."""
    class_ids, confiancas = melhor_classe(np.zeros((0, CLASSES), dtype=np.float32))
    assert class_ids.size == 0 and confiancas.size == 0


def test_filtra_corta_abaixo_do_limiar():
    caixas = np.array([[0, 0, 10, 10], [0, 0, 20, 20]], dtype=np.float32)
    class_ids = np.array([0, 0])
    confiancas = np.array([0.9, 0.1], dtype=np.float32)

    caixas, class_ids, confiancas = filtra(
        caixas, class_ids, confiancas, score_threshold=0.5, classes=None
    )
    assert confiancas.tolist() == pytest.approx([0.9])


def test_filtra_descarta_o_que_nao_e_classe_de_interesse():
    """O MVP só precisa de pessoas. Deixar as outras 79 classes atravessarem é
    inferência paga e descartada três estágios depois."""
    caixas = np.zeros((3, 4), dtype=np.float32)
    class_ids = np.array([CLASSE_PESSOA, 24, CLASSE_PESSOA])
    confiancas = np.array([0.9, 0.9, 0.8], dtype=np.float32)

    _, class_ids, _ = filtra(
        caixas, class_ids, confiancas, score_threshold=0.5, classes=(CLASSE_PESSOA,)
    )
    assert class_ids.tolist() == [CLASSE_PESSOA, CLASSE_PESSOA]


# --- interseção -----------------------------------------------------------------


def test_iou_de_caixas_identicas_e_um():
    caixa = np.array([0.0, 0.0, 10.0, 10.0])
    assert iou(caixa, caixa[np.newaxis, :]).tolist() == pytest.approx([1.0])


def test_iou_de_caixas_disjuntas_e_zero():
    caixa = np.array([0.0, 0.0, 10.0, 10.0])
    longe = np.array([[100.0, 100.0, 110.0, 110.0]])
    assert iou(caixa, longe).tolist() == pytest.approx([0.0])


def test_iou_de_sobreposicao_parcial():
    """Duas caixas 10x10 deslocadas de 5 em X: interseção 50, união 150."""
    caixa = np.array([0.0, 0.0, 10.0, 10.0])
    outra = np.array([[5.0, 0.0, 15.0, 10.0]])
    assert iou(caixa, outra).tolist() == pytest.approx([50 / 150])


def test_caixa_degenerada_nao_produz_nan():
    """União zero dividiria por zero e daria `nan`. E `nan <= limiar` é falso, então a
    caixa sobreviveria à supressão — em silêncio, e sempre."""
    degenerada = np.array([5.0, 5.0, 5.0, 5.0])
    assert iou(degenerada, degenerada[np.newaxis, :]).tolist() == [0.0]


# --- supressão ------------------------------------------------------------------


def test_nms_suprime_a_caixa_sobreposta_de_menor_confianca():
    """Sem isto, um cliente sozinho no corredor vira seis tracks no §3.3 e seis
    eventos no §3.4 — a rajada que o R-1 diz que mata o produto."""
    caixas = np.array(
        [[0.0, 0.0, 10.0, 10.0], [1.0, 1.0, 11.0, 11.0]],
        dtype=np.float32,
    )
    confiancas = np.array([0.9, 0.8], dtype=np.float32)
    class_ids = np.array([CLASSE_PESSOA, CLASSE_PESSOA])

    assert nms(caixas, confiancas, class_ids, iou_threshold=0.45).tolist() == [0]


def test_nms_preserva_pessoas_distantes():
    caixas = np.array(
        [[0.0, 0.0, 10.0, 10.0], [200.0, 200.0, 210.0, 210.0]],
        dtype=np.float32,
    )
    confiancas = np.array([0.9, 0.8], dtype=np.float32)
    class_ids = np.array([CLASSE_PESSOA, CLASSE_PESSOA])

    assert sorted(nms(caixas, confiancas, class_ids, iou_threshold=0.45).tolist()) == [0, 1]


def test_nms_nao_suprime_entre_classes_diferentes():
    """Pessoa carregando mochila ocupa o mesmo retângulo. Supressão global apagaria
    uma das duas, e qual delas dependeria de um empate de confiança."""
    caixas = np.array([[0.0, 0.0, 10.0, 10.0]] * 2, dtype=np.float32)
    confiancas = np.array([0.9, 0.85], dtype=np.float32)
    class_ids = np.array([CLASSE_PESSOA, 24])

    assert sorted(nms(caixas, confiancas, class_ids, iou_threshold=0.45).tolist()) == [0, 1]


def test_nms_devolve_em_ordem_decrescente_de_confianca():
    """É a ordem que o §3.3 quer: o tracker associa primeiro o que o modelo tem mais
    certeza."""
    caixas = np.array(
        [[0.0, 0.0, 10.0, 10.0], [100.0, 0.0, 110.0, 10.0], [200.0, 0.0, 210.0, 10.0]],
        dtype=np.float32,
    )
    confiancas = np.array([0.3, 0.9, 0.6], dtype=np.float32)
    class_ids = np.zeros(3, dtype=np.int64)

    assert nms(caixas, confiancas, class_ids, iou_threshold=0.45).tolist() == [1, 2, 0]


def test_nms_sem_caixas_devolve_vazio():
    vazio = nms(
        np.zeros((0, 4), dtype=np.float32),
        np.zeros(0, dtype=np.float32),
        np.zeros(0, dtype=np.int64),
        iou_threshold=0.45,
    )
    assert vazio.shape == (0,)


def test_nms_com_limiar_alto_preserva_tudo():
    """O limiar é configuração por câmera no fim das contas (§3.4). Um limiar de 1.0
    tem que virar passagem direta, não supressão de tudo."""
    caixas = np.array([[0.0, 0.0, 10.0, 10.0], [0.0, 0.0, 10.0, 10.0]], dtype=np.float32)
    confiancas = np.array([0.9, 0.8], dtype=np.float32)
    class_ids = np.zeros(2, dtype=np.int64)

    assert sorted(nms(caixas, confiancas, class_ids, iou_threshold=1.0).tolist()) == [0, 1]
