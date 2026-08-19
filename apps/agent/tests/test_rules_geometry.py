"""A geometria das zonas do §3.4, nos casos em que ela erra calada.

Nenhum erro daqui levanta exceção em produção: a zona simplesmente passa a conter
gente que não está nela, ou a linha passa a disparar para quem entra. O sintoma chega
dias depois, como falso positivo em massa numa câmera só (R-1), e a essa altura
ninguém liga uma coisa à outra.

O eixo `y` cresce para baixo em todos os testes, como no frame de verdade.
"""

from __future__ import annotations

import pytest

from lince_agent.rules.geometry import LinhaOrientada, Poligono, Travessia

CAIXA = Poligono(((100.0, 100.0), (300.0, 100.0), (300.0, 250.0), (100.0, 250.0)))
"""Um caixa retangular, para os testes que não são sobre a forma do polígono."""

SAIDA = LinhaOrientada(origem=(0.0, 400.0), destino=(640.0, 400.0))
"""Linha desenhada da esquerda para a direita: a loja fica **embaixo** (y > 400) e a
rua em cima. É a convenção documentada em `LinhaOrientada`."""


# --- polígono -------------------------------------------------------------------


def test_ponto_dentro_e_fora_do_caixa():
    assert CAIXA.contem((200.0, 180.0))
    assert not CAIXA.contem((50.0, 180.0))
    assert not CAIXA.contem((200.0, 300.0))


def test_ponto_na_altura_de_um_vertice_nao_conta_duas_vezes():
    """A comparação assimétrica de `y` do lançamento de raio.

    Contar o vértice nas duas arestas que nele se encontram cruzaria a fronteira duas
    vezes e devolveria "fora" para um ponto que está dentro. Como o ponto avaliado são
    os pés de alguém andando pela loja, cair exatamente na altura de um vértice não é
    coincidência rara — é o que acontece durante todo um trecho do percurso quando o
    caixa é retangular e a pessoa anda paralelo a ele.
    """
    assert CAIXA.contem((200.0, 100.0)), "a altura da aresta de cima ainda é dentro"
    assert not CAIXA.contem((50.0, 100.0)), "à esquerda do caixa, na mesma altura, é fora"
    assert not CAIXA.contem((400.0, 100.0)), "à direita também"


def test_poligono_concavo_nao_engole_o_vao():
    """Zona em L — o corredor que contorna o caixa não pode contar como caixa.

    Um algoritmo que assumisse convexidade (fecho convexo, ou "dentro de todas as
    arestas") diria que o vão do L está dentro. Na loja, o vão do L é justamente por
    onde passa quem **não** parou no caixa: a pessoa que devia gerar evento acumularia
    tempo de caixa e sairia descartada.
    """
    ele = Poligono(
        (
            (0.0, 0.0),
            (200.0, 0.0),
            (200.0, 200.0),
            (100.0, 200.0),
            (100.0, 100.0),
            (0.0, 100.0),
        )
    )
    assert ele.contem((50.0, 50.0)), "no braço de cima"
    assert ele.contem((150.0, 150.0)), "no braço da direita"
    assert not ele.contem((50.0, 150.0)), "o vão do L está fora"


def test_poligono_com_menos_de_tres_vertices_e_recusado():
    with pytest.raises(ValueError, match="3 vértices"):
        Poligono(((0.0, 0.0), (10.0, 10.0)))


# --- linha orientada ------------------------------------------------------------


def test_a_convencao_da_linha_e_a_documentada():
    """Linha desenhada da esquerda para a direita ⇒ dentro é embaixo.

    Este teste existe para uma frase do docstring não virar mentira depois de uma
    refatoração. O sinal do produto vetorial em coordenadas de imagem é o oposto do
    que a memória do plano cartesiano sugere, e inverter a convenção sem perceber
    produz uma câmera que dispara para quem **entra** na loja.
    """
    assert SAIDA.dentro((320.0, 500.0)), "y maior é dentro da loja"
    assert not SAIDA.dentro((320.0, 300.0)), "y menor é a rua"


def test_linha_degenerada_e_recusada():
    with pytest.raises(ValueError, match="degenerada"):
        LinhaOrientada(origem=(10.0, 10.0), destino=(10.0, 10.0))


# --- travessia ------------------------------------------------------------------


def test_sair_da_loja_e_saida_e_entrar_e_entrada():
    assert SAIDA.travessia((320.0, 450.0), (320.0, 350.0)) is Travessia.SAIDA
    assert SAIDA.travessia((320.0, 350.0), (320.0, 450.0)) is Travessia.ENTRADA


def test_andar_sem_cruzar_nao_e_travessia():
    assert SAIDA.travessia((320.0, 500.0), (320.0, 450.0)) is None


def test_parado_nao_e_travessia():
    """Pessoa em pé entrega o mesmo ponto em dois frames seguidos. O percurso vira um
    segmento de comprimento zero, e construir uma linha orientada com ele levantaria
    `ValueError` dentro do laço do motor de regras — derrubando a thread de detecção,
    que é uma só para o agente (ADR-007)."""
    assert SAIDA.travessia((320.0, 500.0), (320.0, 500.0)) is None


def test_quem_anda_no_fundo_da_loja_nao_cruza_o_prolongamento_da_linha():
    """**O teste que justifica o segundo par de orientações.**

    A linha de saída cobre a porta, não a largura do mundo. Alguém no fundo da loja,
    a 2000 px à direita da porta, atravessa a *reta infinita* que passa pela linha
    toda vez que anda de uma gôndola para outra. Um teste de travessia que só olhasse
    "os dois pontos estão em lados opostos" transformaria cada uma dessas idas e
    vindas num evento — a câmera inteira viraria ruído.
    """
    assert SAIDA.travessia((2000.0, 450.0), (2000.0, 350.0)) is None


def test_ponto_exatamente_sobre_a_linha_adia_a_decisao():
    """Sobre a linha não há lado, e inventar um é decidir no ponto em que o jitter da
    caixa oscila. Esperar o próximo frame custa 333 ms a 3 fps; decidir errado custa
    um alerta falso."""
    assert SAIDA.travessia((320.0, 450.0), (320.0, 400.0)) is None
    assert SAIDA.travessia((320.0, 400.0), (320.0, 350.0)) is None


def test_linha_diagonal_tambem_respeita_o_sentido():
    """A porta raramente é paralela ao eixo do frame.

    Com a linha na diagonal, "dentro" deixa de ser "embaixo" e passa a depender só do
    sentido do desenho. Aqui a linha vai do canto superior esquerdo para o inferior
    direito, e a conta dá `lado = 300·(y − x)`: dentro é a metade **abaixo e à
    esquerda** dela. Quem confere a convenção olhando o frame precisa fazer essa conta,
    e é por isso que ela está escrita aqui.
    """
    diagonal = LinhaOrientada(origem=(100.0, 100.0), destino=(400.0, 400.0))
    dentro, fora = (200.0, 300.0), (300.0, 200.0)
    assert diagonal.dentro(dentro) and not diagonal.dentro(fora)
    assert diagonal.travessia(dentro, fora) is Travessia.SAIDA
    assert diagonal.travessia(fora, dentro) is Travessia.ENTRADA
