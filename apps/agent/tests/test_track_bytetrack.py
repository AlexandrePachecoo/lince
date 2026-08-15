"""O tracker de uma câmera, em cenários roteirizados.

Este é o estágio mais frágil do sistema inteiro (§3.3), e o modo como ele falha é
silencioso: o contador de tracks sobe igual quando o tracker acerta e quando erra. Por
isso os testes aqui não medem quantidade — cada um encena uma situação de loja e verifica
**qual ID** ficou com **qual pessoa**.

O detector não entra: as detecções são fabricadas, e é isso que torna cada cenário
determinístico. A prova de que o tracker sobrevive a detecção real e intermitente está em
`test_integration.py`, sobre a cam3.
"""

from __future__ import annotations

import pytest

from lince_agent.config import TrackingOptions
from lince_agent.detect.state import Detection
from lince_agent.track.bytetrack import ByteTracker
from lince_agent.track.state import TrackStatus

PASSO_S = 1 / 3
"""Intervalo entre frames avaliados, a 3 fps (§3.2)."""


def pessoa(x: float, *, y: float = 250.0, altura: float = 200.0, score: float = 0.9) -> Detection:
    largura = altura * 0.4
    return Detection(class_id=0, score=score, x1=x, y1=y, x2=x + largura, y2=y + altura)


def tracker(**kwargs) -> ByteTracker:
    return ByteTracker("cam1", TrackingOptions(**kwargs))


def roda(unidade: ByteTracker, quadros: list[tuple[Detection, ...]], *, inicio: float = 0.0):
    """Alimenta uma sequência de frames e devolve a saída do último."""
    saida: tuple = ()
    for i, deteccoes in enumerate(quadros):
        saida = unidade.update(deteccoes, at=inicio + i * PASSO_S)
    return saida


# --- o caso feliz ---------------------------------------------------------------


def test_pessoa_andando_mantem_o_mesmo_id():
    """A propriedade mais básica, e a que o §3.4 assume: uma travessia é um track só.
    Se o ID trocar no meio, a pessoa "some" no caixa e "nasce" na linha de saída sem
    histórico — o falso positivo do R-2."""
    unidade = tracker()
    quadros = [(pessoa(100 + 30 * i),) for i in range(10)]
    tracks = roda(unidade, quadros)

    assert len(tracks) == 1
    assert tracks[0].track_id == 1
    assert tracks[0].status is TrackStatus.CONFIRMADO
    assert tracks[0].hits == 10
    assert unidade.counters.criados == 1, "a pessoa foi criada uma vez só"


@pytest.mark.parametrize(
    ("frames", "esperado"),
    [
        (1, TrackStatus.PROVISORIO),
        (2, TrackStatus.PROVISORIO),
        (3, TrackStatus.CONFIRMADO),
    ],
)
def test_track_confirma_depois_de_min_hits(frames: int, esperado: TrackStatus):
    """Cada caso monta um tracker novo: reaproveitar um já alimentado somaria hits de
    outro cenário e o teste passaria a medir outra coisa."""
    unidade = tracker(min_hits=3)
    tracks = roda(unidade, [(pessoa(100 + 30 * i),) for i in range(frames)])
    assert tracks[0].status is esperado


def test_duas_pessoas_ganham_ids_diferentes():
    unidade = tracker()
    tracks = roda(unidade, [(pessoa(100), pessoa(400)) for _ in range(4)])

    assert len({track.track_id for track in tracks}) == 2


# --- o cenário do R-2 -----------------------------------------------------------


def test_duas_pessoas_se_cruzando_nao_trocam_de_id():
    """**O teste que este arquivo existe para ter.**

    Duas pessoas andando em sentidos opostos num corredor: uma perto da câmera (caixa
    alta, embaixo do quadro) e outra ao fundo (caixa menor, mais acima). Elas se cruzam
    no meio do quadro, e é exatamente aí que o §3.3 prevê a primeira falha — "troca de
    ID em cruzamento de pessoas".

    A verificação não é por ID, é por identidade física: quem tinha a caixa alta no
    começo precisa continuar sendo o mesmo track no fim. Comparar só os IDs deixaria
    passar uma troca em que os dois números continuam existindo.

    As velocidades são proporcionais ao tamanho aparente — quem está ao fundo atravessa
    o quadro em menos pixels por segundo, mesmo andando no mesmo ritmo. Dar a mesma
    velocidade em pixels às duas encenaria uma cena que não existe, e o teste passaria a
    medir o comportamento do tracker diante de dados impossíveis.
    """
    unidade = tracker()
    perto = lambda i: pessoa(80 + 40 * i, y=250.0, altura=200.0)  # noqa: E731
    longe = lambda i: pessoa(400 - 24 * i, y=180.0, altura=120.0)  # noqa: E731

    primeiro = unidade.update((perto(0), longe(0)), at=0.0)
    id_perto = next(t.track_id for t in primeiro if t.height > 150)
    id_longe = next(t.track_id for t in primeiro if t.height < 150)
    assert id_perto != id_longe

    tracks: tuple = ()
    for i in range(1, 9):
        tracks = unidade.update((perto(i), longe(i)), at=i * PASSO_S)

    assert len(tracks) == 2, "ninguém pode ter sido perdido no cruzamento"
    assert next(t.track_id for t in tracks if t.height > 150) == id_perto
    assert next(t.track_id for t in tracks if t.height < 150) == id_longe
    assert unidade.counters.criados == 2, "ninguém renasceu"


# --- oclusão --------------------------------------------------------------------


def test_oclusao_curta_recupera_o_mesmo_id():
    """Pessoa passa atrás de uma gôndola e reaparece. Manter o track vivo durante a
    ausência é o que impede a travessia de virar dois tracks."""
    unidade = tracker(max_perdido_s=2.0)
    for i in range(4):
        unidade.update((pessoa(100 + 30 * i),), at=i * PASSO_S)

    sumida = unidade.update((), at=4 * PASSO_S)
    assert sumida[0].status is TrackStatus.PERDIDO
    assert sumida[0].track_id == 1

    voltou = unidade.update((pessoa(100 + 30 * 5),), at=5 * PASSO_S)
    assert voltou[0].track_id == 1, "a mesma pessoa recebeu um ID novo"
    assert voltou[0].status is TrackStatus.CONFIRMADO
    assert unidade.counters.criados == 1


def test_oclusao_longa_encerra_e_a_volta_e_track_novo():
    """O §3.3 prescreve isto: track perdido por oclusão longa expira, e a reaparição
    vira track novo. Segurar o ID por mais tempo seria pior — a pessoa pode ter saído e
    outra entrado no mesmo lugar."""
    unidade = tracker(max_perdido_s=1.0)
    for i in range(4):
        unidade.update((pessoa(100 + 30 * i),), at=i * PASSO_S)

    vazio = unidade.update((), at=10.0)
    assert vazio == (), "o track devia ter encerrado"
    assert unidade.counters.encerrados == 1

    novo = unidade.update((pessoa(220),), at=10.0 + PASSO_S)
    assert novo[0].track_id == 2


def test_track_perdido_continua_sendo_previsto():
    """Durante a oclusão o Kalman continua andando com a pessoa. É isso que faz a caixa
    prevista encontrar a detecção quando ela volta, em vez de ficar parada onde a pessoa
    sumiu."""
    unidade = tracker()
    for i in range(6):
        unidade.update((pessoa(100 + 30 * i),), at=i * PASSO_S)

    onde_sumiu = unidade.update((), at=6 * PASSO_S)[0].x1
    depois = unidade.update((), at=7 * PASSO_S)[0].x1
    assert depois > onde_sumiu + 15


# --- a segunda passada: detecções fracas ----------------------------------------


def test_deteccao_fraca_sozinha_nunca_cria_track():
    """**A regra que preserva a precisão depois de o piso do detector cair para 0,10.**
    Uma sombra, um reflexo ou um manequim com score 0.2 não pode virar pessoa — se
    virasse, o R-1 explodiria junto com o recall."""
    unidade = tracker(high_threshold=0.5)
    tracks = roda(unidade, [(pessoa(100 + 30 * i, score=0.2),) for i in range(8)])

    assert tracks == ()
    assert unidade.counters.criados == 0


def test_deteccao_fraca_sustenta_um_track_que_ja_existia():
    """O ganho inteiro do ByteTrack. A pessoa entra em contraluz e o modelo despenca
    para 0.25 por um frame; sem a segunda passada, o track quebraria em dois."""
    unidade = tracker(high_threshold=0.5)
    for i in range(4):
        unidade.update((pessoa(100 + 30 * i),), at=i * PASSO_S)

    fraca = unidade.update((pessoa(100 + 30 * 4, score=0.25),), at=4 * PASSO_S)

    assert fraca[0].track_id == 1
    assert fraca[0].status is TrackStatus.CONFIRMADO, "não virou PERDIDO"
    assert unidade.counters.associacoes_baixa == 1


def test_deteccao_fraca_nao_ressuscita_track_perdido_ha_frames():
    """Um track que já estava perdido não volta por uma caixa duvidosa: é assim que o ID
    de quem saiu grudaria em quem entrou. A segunda passada só atende quem estava
    confirmado no frame anterior."""
    unidade = tracker(high_threshold=0.5, max_perdido_s=5.0)
    for i in range(4):
        unidade.update((pessoa(100 + 30 * i),), at=i * PASSO_S)

    unidade.update((), at=4 * PASSO_S)  # vira PERDIDO
    tracks = unidade.update((pessoa(100 + 30 * 5, score=0.2),), at=5 * PASSO_S)

    assert tracks[0].status is TrackStatus.PERDIDO
    assert unidade.counters.associacoes_baixa == 0


# --- o que não vira pessoa ------------------------------------------------------


def test_deteccao_de_um_frame_so_nao_deixa_rastro():
    """Sombra, reflexo, ou o "objeto estático detectado como pessoa" do §3.3. Nasce
    provisório e morre sem nunca chegar ao motor de regras."""
    unidade = tracker(min_hits=3)
    unidade.update((pessoa(100),), at=0.0)
    tracks = unidade.update((), at=PASSO_S)

    assert tracks == ()
    assert unidade.counters.encerrados == 1


def test_track_provisorio_nao_vale_para_regra():
    """`vale_para_regra` é o que o §3.4 vai consultar antes de sequer considerar um
    track. Provisório é candidato, não pessoa."""
    unidade = tracker(min_hits=3)
    (track,) = unidade.update((pessoa(100),), at=0.0)
    assert track.vale_para_regra is False


def test_objeto_estatico_acumula_idade_para_o_filtro_do_estagio_4():
    """O tracker **não** filtra por tempo de vida: expõe `age_s` e `hits` e o §3.4
    decide, porque o limiar é configuração por câmera (ADR-002). O que se garante aqui é
    que o dado existe e cresce."""
    unidade = tracker()
    tracks = roda(unidade, [(pessoa(300),) for _ in range(9)])

    assert tracks[0].age_s == pytest.approx(8 * PASSO_S)
    assert tracks[0].hits == 9
    assert tracks[0].vale_para_regra is True


# --- identidade -----------------------------------------------------------------


def test_id_encerrado_nunca_e_reusado():
    """Reciclar número faria dois percursos distintos parecerem o mesmo track num
    histórico de eventos."""
    unidade = tracker(min_hits=1, max_perdido_s=0.0)
    unidade.update((pessoa(100),), at=0.0)
    unidade.update((), at=1.0)
    unidade.update((), at=2.0)

    (novo,) = unidade.update((pessoa(100),), at=3.0)
    assert novo.track_id == 2


def test_base_central_acompanha_a_caixa_do_track():
    """O ponto que o §3.4 vai testar contra a linha de saída sai do track, não da
    detecção: durante uma oclusão a detecção não existe e o ponto ainda precisa."""
    unidade = tracker()
    tracks = roda(unidade, [(pessoa(100),) for _ in range(3)])
    x, y = tracks[0].base_central

    assert x == pytest.approx((tracks[0].x1 + tracks[0].x2) / 2)
    assert y == pytest.approx(tracks[0].y2)


# --- limites e recusas ----------------------------------------------------------


def test_frame_fora_de_ordem_e_recusado():
    """O Kalman não volta no tempo. Aceitar em silêncio corromperia a velocidade de
    todos os tracks daquela câmera."""
    unidade = tracker()
    unidade.update((pessoa(100),), at=1.0)
    with pytest.raises(ValueError, match="fora de ordem"):
        unidade.update((pessoa(130),), at=0.5)


def test_frame_descartado_nao_perde_o_track():
    """A fila do §3.2 descarta o frame mais antigo quando a inferência não acompanha, e
    o tracker recebe o dobro do intervalo. Com `dt` fixo a previsão pararia na metade do
    caminho, a associação falharia e o ID trocaria — justamente quando o box já está sob
    carga."""
    unidade = tracker()
    for i in range(5):
        unidade.update((pessoa(100 + 40 * i),), at=i * PASSO_S)

    # O frame 5 foi descartado: o próximo chega com o dobro do intervalo e o dobro do
    # deslocamento.
    tracks = unidade.update((pessoa(100 + 40 * 6),), at=6 * PASSO_S)
    assert tracks[0].track_id == 1
    assert tracks[0].status is TrackStatus.CONFIRMADO


def test_teto_de_tracks_e_respeitado():
    """Câmera apontada para a rua, ou modelo alucinando: o agente não pode crescer sem
    limite."""
    unidade = tracker(max_tracks=3, min_hits=1)
    lotado = unidade.update(tuple(pessoa(60 * i, altura=100) for i in range(10)), at=0.0)
    assert len(lotado) == 3


def test_frame_sem_ninguem_nao_estoura():
    """O caso comum numa loja de bairro fora do horário de pico."""
    assert tracker().update((), at=0.0) == ()


def test_caixa_do_track_nao_e_recortada_na_moldura():
    """Ao contrário da caixa de uma detecção, que o §3.2 recorta, a do track é
    estimativa do Kalman e pode passar da borda — quem está saindo pela porta tem mesmo
    os pés estimados um pouco além dela.

    Recortar aqui empurraria a `base_central` para a beirada do quadro, e é ela que o
    §3.4 vai testar contra a linha de saída: toda travessia passaria a ser detectada no
    mesmo lugar, independentemente de onde a pessoa realmente cruzou.
    """
    unidade = tracker()
    for i in range(6):
        unidade.update((pessoa(400 + 40 * i),), at=i * PASSO_S)

    saindo = unidade.update((), at=6 * PASSO_S)[0]
    assert saindo.x2 > 640, "o track parou na borda em vez de continuar saindo"
