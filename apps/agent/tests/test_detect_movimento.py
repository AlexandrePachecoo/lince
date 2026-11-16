"""O portão de movimento (ADR-012): inferir só quando há o que ver.

O erro que este arquivo vigia não é o portão aberto demais — esse custa CPU e aparece no
`--stats`. É o portão **fechado com alguém na loja**, que não deixa rastro nenhum: frame
que não foi inferido não vira caixa, nem track, nem evento, nem contador. Cada teste de
abertura abaixo corresponde a um jeito concreto de uma pessoa ficar invisível.
"""

from __future__ import annotations

import numpy as np
import pytest
from deteccoes import ALTURA, LARGURA, frame

from lince_agent.config import DetectionOptions, PixelFormat
from lince_agent.detect.movimento import (
    LIMIAR_NIVEL,
    Abertura,
    PortaoMovimento,
    amostra_cinza,
    fracao_mudada,
)
from lince_agent.ffmpeg.rawframe import Frame

LIGADO = DetectionOptions(portao_movimento=True, movimento_retencao_s=2.0, vigia_s=5.0)


def cena(at: float, *, pessoa_em: int | None = None, nivel: int = 0) -> Frame:
    """Um frame BGR de fundo uniforme, com um retângulo claro do tamanho de uma pessoa
    ao fundo da loja (60x160 px) quando `pessoa_em` diz onde."""
    imagem = np.full((ALTURA, LARGURA, 3), nivel, dtype=np.uint8)
    if pessoa_em is not None:
        imagem[200:360, pessoa_em : pessoa_em + 60] = 200
    return Frame(
        data=imagem.tobytes(),
        width=LARGURA,
        height=ALTURA,
        pixel_format=PixelFormat.BGR24,
        sequence=int(at * 10),
        received_at=at,
    )


def sequencia(portao: PortaoMovimento, quadros, *, tem_track: bool = False, options=LIGADO):
    return [
        portao.avalia("cam1", quadro, tem_track=tem_track, options=options) for quadro in quadros
    ]


# --- o caso que justifica o portão ---------------------------------------------


def test_loja_vazia_fecha_entre_as_vigias():
    """A loja vazia é o caso comum: sem isto, três câmeras pagam inferência três vezes
    por segundo para confirmar que nada mudou."""
    portao = PortaoMovimento()
    instantes = [i / 3 for i in range(0, 31)]  # 10 s a 3 fps
    aberturas = sequencia(portao, [cena(t) for t in instantes])

    inferidos = [t for t, a in zip(instantes, aberturas, strict=True) if a.infere]
    assert inferidos == [0.0, 5.0, 10.0]
    assert aberturas[0] is Abertura.VIGIA


def test_pessoa_entrando_abre_no_mesmo_frame():
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0), cena(0.33)])
    assert sequencia(portao, [cena(0.66, pessoa_em=100)]) == [Abertura.MOVIMENTO]


def test_pessoa_andando_mantem_aberto():
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0)])
    passos = [cena(1 + i / 3, pessoa_em=50 + 40 * i) for i in range(8)]
    assert all(a is Abertura.MOVIMENTO for a in sequencia(portao, passos))


# --- as três aberturas que cobrem quem não se mexe ------------------------------


def test_pessoa_parada_com_track_vivo_mantem_aberto():
    """A pessoa escolhendo na frente da geladeira quase não mexe. Sem esta abertura, o
    portão fecharia com ela na loja e o track dela congelaria na memória."""
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0), cena(0.33, pessoa_em=100)])
    parada = [cena(1 + i / 3, pessoa_em=100) for i in range(30)]

    aberturas = sequencia(portao, parada, tem_track=True)
    assert all(a is Abertura.TRACK for a in aberturas)


def test_retencao_cobre_quem_o_tracking_ainda_nao_confirmou():
    """Entre o primeiro movimento e o track nascer passam frames. Sem retenção, a pessoa
    que entra e para logo na porta só seria vista na vigia seguinte."""
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0), cena(0.33, pessoa_em=100)])

    assert sequencia(portao, [cena(0.66, pessoa_em=100)]) == [Abertura.RETENCAO]
    assert sequencia(portao, [cena(2.0, pessoa_em=100)]) == [Abertura.RETENCAO]
    assert sequencia(portao, [cena(2.4, pessoa_em=100)]) == [Abertura.FECHADO]


def test_vigia_acha_quem_ja_estava_imovel_quando_o_portao_comecou():
    """Agente reiniciado com alguém parado na loja: não há movimento para disparar, nem
    track ainda. A vigia periódica é o que o põe no tracking."""
    portao = PortaoMovimento()
    parado = [cena(i / 3, pessoa_em=100) for i in range(0, 19)]  # 6 s, sem mexer

    aberturas = sequencia(portao, parado)
    assert aberturas[0] is Abertura.VIGIA
    vigias = [i for i, a in enumerate(aberturas) if a is Abertura.VIGIA]
    assert vigias == [0, 15]


def test_frame_de_movimento_reinicia_a_contagem_da_vigia():
    """A vigia conta do último frame inferido, qualquer que tenha sido o motivo. Contar
    só das vigias faria uma inferência extra logo depois de cada movimento."""
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0), cena(4.0, pessoa_em=100)])  # vigia em 0, movimento em 4
    assert sequencia(portao, [cena(6.1, pessoa_em=100)]) == [Abertura.FECHADO]
    assert sequencia(portao, [cena(9.0, pessoa_em=100)]) == [Abertura.VIGIA]


# --- o que não é movimento -----------------------------------------------------


def test_ruido_de_sensor_nao_abre():
    """Câmera barata à noite oscila uns poucos níveis em todo pixel. Se isso abrisse, o
    portão ficaria aberto a noite inteira — exatamente quando a loja está vazia."""
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0, nivel=60)])
    ruidoso = cena(0.33, nivel=60 + LIMIAR_NIVEL - 5)
    assert sequencia(portao, [ruidoso]) == [Abertura.FECHADO]


def test_mudanca_pequena_demais_nao_abre():
    """Um ponto isolado (reflexo, inseto na lente) não é uma pessoa."""
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0)])
    imagem = np.zeros((ALTURA, LARGURA, 3), dtype=np.uint8)
    imagem[0:8, 0:8] = 255  # uma amostra só, de 4 800
    ponto = Frame(imagem.tobytes(), LARGURA, ALTURA, PixelFormat.BGR24, 2, 0.33)
    assert sequencia(portao, [ponto]) == [Abertura.FECHADO]


def test_limiar_de_movimento_e_configuracao():
    """O mesmo movimento abre ou não conforme `movimento_min`: é o ajuste por câmera."""
    exigente = DetectionOptions(portao_movimento=True, movimento_min=0.5)
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0)], options=exigente)
    assert sequencia(portao, [cena(0.33, pessoa_em=100)], options=exigente) == [Abertura.FECHADO]


# --- câmeras e formatos ----------------------------------------------------------


def test_cada_camera_tem_o_proprio_estado():
    """O movimento numa câmera não abre a outra, nem lhe serve de referência."""
    portao = PortaoMovimento()
    portao.avalia("porta", cena(0.0), tem_track=False, options=LIGADO)
    portao.avalia("geladeira", cena(0.0), tem_track=False, options=LIGADO)

    mexeu = portao.avalia("porta", cena(1.0, pessoa_em=100), tem_track=False, options=LIGADO)
    parada = portao.avalia("geladeira", cena(1.0), tem_track=False, options=LIGADO)
    assert (mexeu, parada) == (Abertura.MOVIMENTO, Abertura.FECHADO)


def test_resolucao_nova_abre_como_vigia_e_vira_a_referencia():
    """Reconexão com outro substream: comparar grades de tamanhos diferentes estouraria.
    Abre — na dúvida, abre — e passa a comparar com a resolução nova."""
    portao = PortaoMovimento()
    sequencia(portao, [cena(0.0)])
    menor = frame(1, received_at=0.33, width=320, height=240)
    assert sequencia(portao, [menor]) == [Abertura.VIGIA]
    assert sequencia(portao, [frame(2, received_at=0.66, width=320, height=240)]) == [
        Abertura.FECHADO
    ]


@pytest.mark.parametrize("formato", [PixelFormat.NV12, PixelFormat.YUV420P])
def test_formato_planar_usa_o_plano_de_luminancia(formato: PixelFormat):
    """O box com decode por hardware entrega NV12. As primeiras `height` linhas são o
    Y; a cor vem depois e não pode entrar na grade, ou a comparação mistura planos."""
    tamanho = formato.frame_bytes(LARGURA, ALTURA)
    dados = bytearray(tamanho)
    dados[LARGURA * ALTURA :] = b"\xff" * (tamanho - LARGURA * ALTURA)  # croma saturado
    quadro = Frame(bytes(dados), LARGURA, ALTURA, formato, 1, 0.0)

    grade = amostra_cinza(quadro)
    assert grade.shape == (ALTURA // 8, LARGURA // 8)
    assert int(grade.max()) == 0


def test_fracao_mudada_conta_amostras_e_nao_soma_diferencas():
    """Uma lâmpada que oscila muda tudo um pouco; uma pessoa muda pouco muito."""
    base = np.zeros((10, 10), dtype=np.int16)
    tudo_um_pouco = base + LIMIAR_NIVEL
    pouco_muito = base.copy()
    pouco_muito[0, :5] = 200

    assert fracao_mudada(base, tudo_um_pouco) == 0.0
    assert fracao_mudada(base, pouco_muito) == 0.05


def test_grade_vazia_nao_divide_por_zero():
    vazia = np.zeros((0, 0), dtype=np.int16)
    assert fracao_mudada(vazia, vazia) == 0.0


def test_abertura_diz_se_infere():
    assert [a for a in Abertura if not a.infere] == [Abertura.FECHADO]
