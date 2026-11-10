"""O gesto de pôr o item na bolsa, como regra — antes de qualquer classificador.

A regra é uma linha de base: alcançar (o pulso longe do tronco) e, em seguida,
levar o pulso à região do quadril — onde a bolsa fica pendurada — e parar ali,
ou sumir ali. Os dois modos de errar não estouram:

- disparar em quem só apoia a mão no quadril, ou leva a mão para trás das costas
  (o pulso some sem ter passado pela bolsa) — o gerente recebe um clipe de gente
  parada, e é assim que o R-1 desliga a notificação;
- não disparar porque a pessoa está mais longe da câmera e tudo ficou menor. Por
  isso a régua é o próprio tronco, não o pixel.

As posições abaixo estão em "troncos" (do meio dos ombros ao meio dos quadris),
com o centro no meio do tronco e y crescendo para baixo: o quadril fica em
y ≈ +0,5, a bolsa a tiracolo entre +0,2 e +1,4.
"""

from __future__ import annotations

import math

import pytest
from gesto_util import pose_com, resultado, sequencia

from lince_agent.gesto.corpo import normaliza, referencia_do_tronco
from lince_agent.gesto.motor import MotorGesto, ParametrosGesto

LONGE = (1.6, -0.1)  # braço esticado para a prateleira
MEIO = (0.7, 0.0)  # voltando
BOLSA = (0.6, 0.7)  # lateral do quadril, onde a bolsa fica
COSTAS = None  # pulso não visto


# --- a régua do tronco --------------------------------------------------------


def test_referencia_e_o_meio_do_tronco_na_escala_do_tronco():
    ref = referencia_do_tronco(pose_com(), limiar=0.3)
    assert ref is not None
    assert (ref.centro_x, ref.centro_y, ref.escala) == pytest.approx((320.0, 240.0, 100.0))


def test_a_mesma_postura_mais_longe_da_camera_da_o_mesmo_ponto():
    """Pessoa a 5 m tem metade dos pixels de quem está a 2,5 m. Um limiar em pixel
    dispararia numa e não na outra para o mesmo gesto."""
    perto = pose_com(pulso_direito=BOLSA, escala=120.0)
    longe = pose_com(pulso_direito=BOLSA, escala=60.0, cx=900.0, cy=100.0)
    pontos = []
    for pose in (perto, longe):
        ref = referencia_do_tronco(pose, limiar=0.3)
        pulso = pose.juntas[10]
        pontos.append(normaliza(pulso.x, pulso.y, ref))
    assert pontos[0] == pytest.approx(pontos[1])
    assert pontos[0] == pytest.approx(BOLSA)


def test_um_ombro_e_um_quadril_bastam():
    """De perfil, o ombro e o quadril de trás somem. Exigir os quatro deixaria a
    regra cega justamente na câmera oblíqua do plano."""
    pose = pose_com()
    juntas = list(pose.juntas)
    for indice in (6, 12):  # ombro e quadril direitos
        j = juntas[indice]
        juntas[indice] = type(j)(indice=j.indice, x=j.x, y=j.y, confianca=0.05)
    ref = referencia_do_tronco(type(pose)(juntas=tuple(juntas)), limiar=0.3)
    assert ref is not None
    assert ref.escala == pytest.approx(100.0, rel=0.05)


def test_sem_ombro_ou_sem_quadril_nao_ha_referencia():
    assert referencia_do_tronco(pose_com(tronco=False), limiar=0.3) is None


def test_tronco_degenerado_nao_vira_divisao_por_zero():
    """Ombro e quadril no mesmo pixel é erro do modelo. Dividir por zero faria
    todo pulso ficar infinitamente longe — e todo frame, um alcance."""
    pose = pose_com(escala=0.0)
    assert referencia_do_tronco(pose, limiar=0.3) is None


# --- o gesto --------------------------------------------------------------------


def test_alcanca_e_leva_a_mao_a_bolsa_dispara():
    motor = MotorGesto()
    disparos = sequencia(motor, [LONGE, LONGE, MEIO, BOLSA, BOLSA, BOLSA, BOLSA])
    assert len(disparos) == 1
    (d,) = disparos
    assert (d.camera_id, d.track_id, d.lado, d.motivo) == ("cam1", 1, "direito", "permaneceu")


def test_alcanca_e_a_mao_some_dentro_da_bolsa_dispara():
    """O que se viu nos vídeos 11 e 12 do dataset: a mão entra na bolsa e o pulso
    some ali. Exigir que ele continue visível perderia o caso mais comum."""
    motor = MotorGesto()
    disparos = sequencia(motor, [LONGE, MEIO, BOLSA, COSTAS, COSTAS, COSTAS])
    assert [d.motivo for d in disparos] == ["sumiu"]


def test_mao_que_some_sem_passar_pela_bolsa_nao_dispara():
    """Vídeos 10 e 13: o braço vai para trás do corpo e o pulso some. Sem ter
    passado pela região da bolsa, sumir é oclusão, não gesto."""
    motor = MotorGesto()
    assert sequencia(motor, [LONGE, LONGE, MEIO, COSTAS, COSTAS, COSTAS, COSTAS]) == []


def test_mao_apoiada_no_quadril_sem_alcance_nao_dispara():
    """Quem espera na fila com a mão na cintura passa segundos na região da
    bolsa. Sem um alcance antes, não há item para esconder."""
    motor = MotorGesto()
    assert sequencia(motor, [BOLSA] * 24) == []


def test_alcance_velho_demais_nao_conta():
    """A janela é de 2 s. Um alcance de 5 s atrás e a mão na cintura agora são
    dois momentos sem relação — a pessoa já pôs o item na cesta e seguiu."""
    motor = MotorGesto()
    trajeto = [LONGE] * 2 + [MEIO] * 40 + [BOLSA] * 6
    assert sequencia(motor, trajeto) == []


def test_passagem_rapida_pela_bolsa_nao_dispara():
    """O braço que desce balançando atravessa a região do quadril num frame. A
    permanência mínima separa passar por ali de parar ali."""
    motor = MotorGesto()
    assert sequencia(motor, [LONGE, MEIO, BOLSA, MEIO, MEIO, MEIO]) == []


def test_mesma_forma_em_escala_menor_dispara_igual():
    motor = MotorGesto()
    trajeto = [LONGE, LONGE, MEIO, BOLSA, BOLSA, BOLSA, BOLSA]
    assert len(sequencia(motor, trajeto, escala=40.0, cx=100.0, cy=80.0)) == 1


def test_pulso_esquerdo_tambem_vale():
    motor = MotorGesto()
    espelho = [(-x, y) for x, y in (LONGE, LONGE, MEIO, BOLSA, BOLSA, BOLSA, BOLSA)]
    (d,) = sequencia(motor, espelho, lado="pulso_esquerdo")
    assert d.lado == "esquerdo"


def test_sem_tronco_a_regra_se_abstem_e_conta():
    """Sem ombro e quadril não há régua: a regra não chuta. A abstenção é contada
    para aparecer no número, não sumir nele."""
    motor = MotorGesto()
    assert sequencia(motor, [LONGE, MEIO, BOLSA, BOLSA, BOLSA], tronco=False) == []
    assert motor.stats().sem_tronco == 5


def test_um_disparo_por_gesto_e_nao_um_por_frame():
    """A mão parada dentro da bolsa por um segundo são oito frames que satisfazem
    a regra. Oito alertas do mesmo gesto é a rajada que o R-12 manda evitar."""
    motor = MotorGesto()
    assert len(sequencia(motor, [LONGE, MEIO] + [BOLSA] * 16)) == 1


def test_segundo_gesto_depois_do_refratario_dispara_de_novo():
    motor = MotorGesto(ParametrosGesto(refratario_s=3.0))
    gesto = [LONGE, MEIO, BOLSA, BOLSA, BOLSA]
    primeiro = sequencia(motor, gesto, inicio=0.0)
    cedo = sequencia(motor, gesto, inicio=1.5)
    tarde = sequencia(motor, gesto, inicio=5.0)
    assert (len(primeiro), len(cedo), len(tarde)) == (1, 0, 1)


def test_tracks_e_pulsos_nao_se_misturam():
    """O alcance de uma pessoa e a mão na cintura de outra não fazem um gesto."""
    motor = MotorGesto()
    disparos = []
    for i in range(8):
        a = pose_com(pulso_direito=LONGE if i < 2 else MEIO)
        b = pose_com(pulso_direito=BOLSA)
        disparos += motor.observa(resultado(i / 8, (1, a), (2, b)))
    assert disparos == []


def test_reconexao_zera_o_estado_da_camera():
    """Na reconexão os IDs de track recomeçam do 1. O alcance do "track 1" antigo
    não pode completar o gesto do "track 1" novo, que é outra pessoa."""
    motor = MotorGesto()
    for i, posicao in enumerate([LONGE, LONGE, MEIO]):
        motor.observa(resultado(10 + i / 8, (1, pose_com(pulso_direito=posicao)), sequence=80 + i))
    disparos = []
    for i in range(5):
        r = resultado(10.5 + i / 8, (1, pose_com(pulso_direito=BOLSA)), sequence=i)
        disparos += motor.observa(r)
    assert disparos == []
    assert motor.stats().reconexoes == 1


def test_reconexao_de_uma_camera_nao_apaga_a_outra():
    motor = MotorGesto()
    disparos = []
    for i, posicao in enumerate([LONGE, MEIO, BOLSA, BOLSA, BOLSA]):
        r2 = resultado(i / 8, (1, pose_com(pulso_direito=posicao)), camera_id="cam2")
        disparos += motor.observa(r2)
        if i == 2:
            motor.observa(resultado(i / 8, camera_id="cam1", sequence=500))
            motor.observa(resultado(i / 8, camera_id="cam1", sequence=1))
    assert [d.camera_id for d in disparos] == ["cam2"]


def test_track_que_some_e_esquecido():
    """Estado por track sem poda cresce com cada cliente do dia. Um track sem
    amostra há mais de duas janelas é esquecido."""
    motor = MotorGesto()
    for t in range(1, 50):
        motor.observa(resultado(t * 1.0, (t, pose_com(pulso_direito=MEIO))))
    assert motor.tracks_em_memoria() <= 6


def test_confianca_nao_finita_conta_como_pulso_nao_visto():
    motor = MotorGesto()
    pose = pose_com(pulso_esquerdo=LONGE, pulso_direito=LONGE)
    juntas = list(pose.juntas)
    j = juntas[10]
    juntas[10] = type(j)(indice=10, x=math.nan, y=math.nan, confianca=math.nan)
    motor.observa(resultado(0.0, (1, type(pose)(juntas=tuple(juntas)))))
    assert motor.stats().pulso_nao_visto == 1


def test_parametros_sao_validados():
    with pytest.raises(ValueError, match="zona"):
        ParametrosGesto(zona_y_min=1.0, zona_y_max=0.5)
    with pytest.raises(ValueError, match="janela"):
        ParametrosGesto(janela_s=0.0)
