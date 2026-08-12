"""A aritmética da janela do clipe.

Nada aqui estoura quando erra. Um offset errado entrega um clipe deslocado que
abre, toca e parece perfeito — só não tem dentro o que o operador precisava ver.
Por isso a régua toda leva teste tabelado.
"""

from __future__ import annotations

import pytest

from lince_agent.clip.timeline import estimate_media_offset, measure_rolls, select_window
from lince_agent.ffmpeg.fmp4 import Fragment

TIMESCALE = 1000


def fragmento(start_s: float, received_at: float) -> Fragment:
    return Fragment(
        data=b"",
        base_media_decode_time=round(start_s * TIMESCALE),
        timescale=TIMESCALE,
        received_at=received_at,
        session_id=1,
    )


def camera(inicios: list[float], *, offset: float, gop_s: float = 2.0) -> list[Fragment]:
    """Fragmentos como uma câmera ao vivo os entrega.

    O fragmento é emitido quando seu GOP fecha — ou seja, quando a mídia já chegou
    ao início do fragmento seguinte. `offset` é a diferença real entre o timeline
    de mídia e o relógio monotônico, que é justamente o que o estimador tem que
    recuperar.
    """
    return [fragmento(inicio, (inicio + gop_s) - offset) for inicio in inicios]


def test_offset_converte_monotonico_para_tempo_de_midia():
    """O gatilho vem monotônico e os fragmentos estão em tempo de mídia. Errar
    esta conversão desloca todo clipe do sistema, e ninguém percebe olhando: o
    arquivo continua reproduzindo normalmente."""
    fragmentos = camera([1000.0, 1002.0, 1004.0, 1006.0], offset=500.0)

    offset = estimate_media_offset(fragmentos)

    assert offset == pytest.approx(500.0)
    # A conversão precisa devolver o instante de mídia correspondente ao gatilho.
    assert 503.0 + offset == pytest.approx(1003.0)


def test_offset_ignora_fragmento_atrasado_na_fila():
    """A `DropOldestQueue` fica entre a thread leitora e a despachante. Um
    fragmento represado ali chega com `received_at` tardio; se o estimador usasse
    o mínimo das amostras, esse único atraso moveria a janela de todos os clipes
    daquela câmera."""
    fragmentos = camera([0.0, 2.0, 4.0, 6.0, 8.0, 10.0], offset=100.0)
    atrasado = fragmentos[2]
    fragmentos[2] = fragmento(atrasado.start_seconds, atrasado.received_at + 3.0)

    assert estimate_media_offset(fragmentos) == pytest.approx(100.0)


def test_offset_ignora_buraco_no_timeline():
    """Fragmento descartado pela fila deixa um buraco: a amostra que o atravessa
    vem inflada de um GOP inteiro. Um estimador que usasse o máximo leria esse
    buraco como desvio de relógio."""
    fragmentos = camera([0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 12.0], offset=100.0)
    del fragmentos[3]

    assert estimate_media_offset(fragmentos) == pytest.approx(100.0)


def test_offset_exige_dois_fragmentos():
    """Com um fragmento só o viés é de um GOP inteiro — 10 s na cam2, o pós-roll
    inteiro. Dizer que não sabe deixa o recorder degradar de forma explícita;
    chutar produziria um clipe errado com aparência de certo."""
    assert estimate_media_offset([]) is None
    assert estimate_media_offset(camera([0.0], offset=0.0)) is None
    assert estimate_media_offset(camera([0.0, 2.0], offset=0.0)) is not None


def test_janela_inclui_o_fragmento_que_contem_o_inicio():
    """O pré-roll começa no meio de um fragmento. Pegar o primeiro que começa
    *depois* do instante pedido cortaria fora justamente o pré-roll."""
    fragmentos = camera([0.0, 2.0, 4.0, 6.0, 8.0], offset=0.0)

    primeiro, _ = select_window(fragmentos, start_s=5.0, end_s=7.0)

    assert fragmentos[primeiro].start_seconds == 4.0


def test_janela_inclui_o_fragmento_que_contem_o_fim():
    """O instante final também cai no meio de um fragmento — e é lá que está a
    travessia da linha de saída, a única coisa que o operador precisa ver."""
    fragmentos = camera([0.0, 2.0, 4.0, 6.0, 8.0], offset=0.0)

    _, ultimo = select_window(fragmentos, start_s=1.0, end_s=7.0)

    assert fragmentos[ultimo].start_seconds == 6.0


def test_janela_pedida_antes_do_buffer_devolve_o_que_existe():
    """Gatilho logo depois de o agente subir, ou logo depois de uma reconexão: o
    §3.5 manda degradar (pré-roll menor, registrado no evento), não falhar."""
    fragmentos = camera([10.0, 12.0, 14.0], offset=0.0)

    primeiro, ultimo = select_window(fragmentos, start_s=0.0, end_s=13.0)

    assert primeiro == 0
    assert fragmentos[ultimo].start_seconds == 12.0


def test_janela_alem_do_buffer_para_no_ultimo_fragmento():
    """O pós-roll pedido pode ainda não ter chegado; quem espera é o recorder, e
    a seleção só entrega o que existe."""
    fragmentos = camera([0.0, 2.0, 4.0], offset=0.0)

    primeiro, ultimo = select_window(fragmentos, start_s=1.0, end_s=99.0)

    assert (primeiro, ultimo) == (0, 2)


def test_pos_roll_medido_usa_o_inicio_do_fragmento_seguinte():
    """O último fragmento selecionado dura um GOP inteiro. Medir pelo seu início
    subestimaria o pós-roll em até 10 s na cam2 — e esse número vai no evento,
    então o operador acharia que o clipe termina antes do que termina."""
    fragmentos = camera([0.0, 2.0, 4.0, 6.0, 8.0], offset=0.0)
    primeiro, ultimo = select_window(fragmentos, start_s=3.0, end_s=7.0)

    _, pos_roll = measure_rolls(fragmentos, primeiro, ultimo, trigger_media_s=5.0)

    # último selecionado começa em 6.0 e o seguinte em 8.0: o clipe vai até 8.0.
    assert pos_roll == pytest.approx(3.0)


def test_pre_roll_efetivo_pode_passar_do_pedido():
    """O corte se alinha ao keyframe, então o clipe começa um pouco antes do
    pré-roll pedido. O §3.5 aceita explicitamente — clipe maior não atrapalha
    triagem — mas o número reportado tem que ser o real, não o pedido."""
    fragmentos = camera([0.0, 2.0, 4.0, 6.0], offset=0.0)
    primeiro, ultimo = select_window(fragmentos, start_s=3.0, end_s=6.5)

    pre_roll, _ = measure_rolls(fragmentos, primeiro, ultimo, trigger_media_s=5.0)

    # pediu 2 s de pré-roll (5.0 - 3.0), recebeu 3 s porque o keyframe está em 2.0.
    assert pre_roll == pytest.approx(3.0)


def test_pos_roll_sem_fragmento_seguinte_e_subestimado():
    """Quando a seleção termina onde o buffer termina, não há como saber onde o
    último fragmento acaba. Subestimar é o lado seguro: reportar mais pós-roll do
    que existe faria o operador procurar no arquivo algo que não está lá."""
    fragmentos = camera([0.0, 2.0, 4.0], offset=0.0)

    pre_roll, pos_roll = measure_rolls(fragmentos, 0, 2, trigger_media_s=1.0)

    assert pre_roll == pytest.approx(1.0)
    assert pos_roll == pytest.approx(3.0)


def test_selecao_sem_fragmentos_e_erro():
    """Buffer vazio não é janela vazia — é ausência de vídeo, e o recorder precisa
    tratar como `clip_failed` em vez de montar um arquivo de zero byte."""
    with pytest.raises(ValueError, match="fragmentos"):
        select_window([], start_s=0.0, end_s=1.0)
    with pytest.raises(ValueError, match="fragmentos"):
        measure_rolls([], 0, 0, trigger_media_s=0.0)


def test_janela_invertida_e_erro():
    fragmentos = camera([0.0, 2.0], offset=0.0)
    with pytest.raises(ValueError, match="invertida"):
        select_window(fragmentos, start_s=5.0, end_s=1.0)
