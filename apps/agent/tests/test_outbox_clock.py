"""A conversão do relógio monotônico da borda para o instante que a nuvem armazena.

O erro que esta suíte existe para impedir não aparece em teste manual e não gera log:
dois eventos com quinze minutos de diferença chegando à triagem fora de ordem, ou um
evento que ficou seis horas na fila chegando com o horário de quando o link voltou.
Nos dois casos o clipe está certo e a linha do tempo, errada — e é a linha do tempo
que o gerente usa para decidir se aquilo faz sentido.
"""

from __future__ import annotations

from lince_agent.outbox.clock import MonotonicAnchor, to_iso


class RelogioFalso:
    """Relógio controlado pelo teste. A regra 6 do CLAUDE.md proíbe `sleep`, e aqui
    ele seria pior que instável: passo de NTP não se reproduz esperando."""

    def __init__(self, inicio: float) -> None:
        self.agora = inicio

    def __call__(self) -> float:
        return self.agora

    def avanca(self, segundos: float) -> None:
        self.agora += segundos


def monta(*, mono: float = 1000.0, parede: float = 1_700_000_000.0, passo_s: float = 1.0):
    relogio = RelogioFalso(mono)
    parede_falsa = RelogioFalso(parede)
    ancora = MonotonicAnchor(clock=relogio, wall_clock=parede_falsa, step_threshold_s=passo_s)
    return ancora, relogio, parede_falsa


def test_formato_tem_milissegundos_e_z():
    """O schema do §5 exige exatamente este formato; `isoformat` entrega `+00:00`."""
    assert to_iso(0.0) == "1970-01-01T00:00:00.000Z"
    assert to_iso(1.9999) == "1970-01-01T00:00:01.999Z"


def test_instante_do_gatilho_e_o_do_gatilho_nao_o_da_conversao():
    """O clipe leva 15 s para ficar pronto e a fila pode segurar o evento por horas.
    Se a conversão usasse o relógio de agora, todo evento subiria com o horário do
    envio — e uma loja que passou a noite offline mandaria a madrugada inteira
    carimbada com a hora em que a internet voltou."""
    ancora, relogio, parede = monta()
    gatilho = relogio.agora

    relogio.avanca(6 * 3600)
    parede.avanca(6 * 3600)

    assert ancora.to_iso(gatilho) == to_iso(1_700_000_000.0)


def test_deriva_pequena_nao_reancora():
    """Deriva de cristal é contínua e minúscula. Reancorar a cada leitura aplicaria
    cada microcorreção do NTP e faria dois eventos consecutivos poderem trocar de
    ordem."""
    ancora, relogio, parede = monta(passo_s=1.0)
    relogio.avanca(10.0)
    parede.avanca(10.3)

    deriva = ancora.check_drift()

    assert 0.29 < deriva < 0.31
    assert ancora.reanchors == 0


def test_passo_de_relogio_reancora_e_conta():
    ancora, relogio, parede = monta(passo_s=1.0)
    relogio.avanca(10.0)
    parede.avanca(10.0 + 45.0)

    assert ancora.check_drift() == 45.0
    assert ancora.reanchors == 1
    # Depois de reancorar, a conversão passa a acompanhar o relógio novo.
    assert ancora.to_iso(relogio.agora) == to_iso(parede.agora)


def test_passo_para_tras_tambem_reancora():
    """`date -s` para trás é o caso que quebraria o monotônico se alguém tivesse
    usado `time.time()` no pipeline: sem reancorar, todo evento seguinte nasceria
    com horário no futuro."""
    ancora, relogio, parede = monta(passo_s=1.0)
    relogio.avanca(10.0)
    parede.avanca(10.0 - 30.0)

    assert ancora.check_drift() == -30.0
    assert ancora.reanchors == 1


def test_reancorar_nao_reordena_eventos_ja_convertidos():
    """O evento que já está na fila carrega uma string pronta. Reancorar depois não
    pode reescrever o passado — só corrigir o futuro."""
    ancora, relogio, parede = monta(passo_s=1.0)
    primeiro = ancora.to_iso(relogio.agora)

    relogio.avanca(60.0)
    parede.avanca(60.0 + 3600.0)
    ancora.check_drift()

    relogio.avanca(60.0)
    parede.avanca(60.0)
    segundo = ancora.to_iso(relogio.agora)

    assert primeiro < segundo
    assert primeiro == to_iso(1_700_000_000.0)


def test_reported_at_e_leitura_crua_da_parede():
    """`reported_at` existe justamente para não passar pela âncora: é ele que deixa a
    nuvem medir a deriva do box comparando com o `occurred_at` derivado."""
    ancora, relogio, parede = monta()
    relogio.avanca(5.0)
    parede.avanca(5.0 + 120.0)

    assert ancora.now_iso() == to_iso(parede.agora)
    assert ancora.now_iso() != ancora.to_iso(relogio.agora)
