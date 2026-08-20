"""O poll de configuração da §5.2, com relógio falso e nuvem roteirizada.

O que se testa aqui não é "a requisição sai certa" — isso é `test_cloud_client.py`,
contra um socket de verdade. É a **decisão**: o que o agente faz com cada resposta, e
principalmente o que ele *não* faz. A regra que governa tudo é a última linha da §5.4:
nenhuma falha de rede pode parar a detecção. Um poller que levanta mata a thread e a
loja nunca mais recebe calibração — sem erro, sem alerta, sem sintoma até alguém
reparar que aquela loja está rodando uma versão de três meses atrás.

Nada aqui usa `sleep`: o intervalo é observado pelo que o `tick` faz, e o relógio é
injetado (regra 6 do CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from lince_agent.config import AgentConfig
from lince_agent.config_cache import ConfigCache
from lince_agent.config_loader import ConfigError, monta_config
from lince_agent.config_poller import INTERVALO_S, ConfigPoller
from lince_agent.outbox.http import CloudResponse, NetworkError
from lince_agent.outbox.policy import ConfigOutcome

DOCUMENTO = {
    "schema_version": 1,
    "config_version": "v1",
    "tenant_id": "rede-abc",
    "store_id": "loja-1",
    "cameras": [{"camera_id": "cam1", "url": "rtsp://x/cam1"}],
}


@dataclass
class ClienteRoteirizado:
    """Dublê no nível do Protocol: aqui o que importa é a sequência de decisões, e um
    servidor HTTP real só acrescentaria threads a um teste sobre lógica."""

    roteiro: list[object] = field(default_factory=list)
    etags: list[str | None] = field(default_factory=list)
    padrao: CloudResponse = field(default_factory=lambda: CloudResponse(status=304))

    def get_config(self, *, etag: str | None = None) -> CloudResponse:
        self.etags.append(etag)
        resposta = self.roteiro.pop(0) if self.roteiro else self.padrao
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


class RelogioFalso:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


@dataclass
class Aplicador:
    """Guarda o que foi entregue para aplicar. Uma lista vazia é uma asserção forte:
    significa que nada chegou ao runtime."""

    recebidas: list[AgentConfig] = field(default_factory=list)

    def __call__(self, config: AgentConfig) -> None:
        self.recebidas.append(config)


def monta(tmp_path: Path):
    def _monta(documento: dict) -> AgentConfig:
        return monta_config(documento, clips_dir=tmp_path / "clipes")

    return _monta


def poller(tmp_path, cliente, *, cache=None, etag=None, aplica=None, clock=None):
    return ConfigPoller(
        cliente,
        monta=monta(tmp_path),
        aplica=aplica or Aplicador(),
        cache=cache,
        etag=etag,
        clock=clock or RelogioFalso(),
        jitter=lambda: 0.0,
    )


def test_primeiro_poll_aplica_e_guarda_o_etag(tmp_path):
    """O `ETag` da resposta é o que o próximo poll manda em `If-None-Match`. Sem
    guardá-lo, o agente baixa o documento inteiro de 30 em 30 segundos para sempre e o
    `304` do §5.2 nunca acontece."""
    cliente = ClienteRoteirizado(
        roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO), etag='"v1"')]
    )
    aplica = Aplicador()
    p = poller(tmp_path, cliente, aplica=aplica)

    assert p.tick() is ConfigOutcome.NOVA
    assert cliente.etags == [None]
    assert [c.config_version for c in aplica.recebidas] == ["v1"]

    p.tick()
    assert cliente.etags[1] == '"v1"'
    assert p.stats().etag == '"v1"'


def test_304_nao_chama_o_loader_nem_o_aplicador(tmp_path):
    """O caso comum, milhares de vezes por dia. Remontar a configuração a cada `304`
    seria trabalho puro; entregá-la ao runtime seria pior — cada `reconfigure` zera os
    tracks em curso, e a loja piscaria de 30 em 30 segundos por estar tudo bem."""
    aplica = Aplicador()
    p = poller(tmp_path, ClienteRoteirizado(), aplica=aplica, etag='"v1"')

    assert p.tick() is ConfigOutcome.SEM_MUDANCA
    assert aplica.recebidas == []
    assert p.stats().sem_mudanca == 1
    assert p.stats().recebidos == 0


def test_documento_invalido_nao_aplica_e_nao_cacheia(tmp_path):
    """Divergência de contrato entre a API e o agente. O que **não** pode acontecer é o
    documento ruim entrar no cache: ele viraria a configuração de subida da próxima vez
    que o box reiniciasse sem rede, e aí a loja não sobe mais — com conserto que exige
    ir até o box."""
    cache = ConfigCache(tmp_path / "cache.json")
    cache.grava(DOCUMENTO, etag='"v1"')
    aplica = Aplicador()
    cliente = ClienteRoteirizado(
        roteiro=[CloudResponse(status=200, body={"schema_version": 1}, etag='"v2"')]
    )
    p = poller(tmp_path, cliente, cache=cache, aplica=aplica, etag='"v1"')

    p.tick()

    assert aplica.recebidas == []
    assert p.stats().invalidos == 1
    guardado = cache.le()
    assert guardado is not None
    assert guardado.documento["config_version"] == "v1"
    assert guardado.etag == '"v1"'


def test_documento_invalido_nao_avanca_o_etag(tmp_path):
    """Avançar o `ETag` faria a nuvem responder `304` para um documento que este agente
    nunca conseguiu ler — e o **conserto**, publicado em seguida, também viria como
    `304` se o servidor gerar o mesmo `ETag`. O agente ficaria esperando para sempre uma
    correção que já foi feita."""
    cliente = ClienteRoteirizado(
        roteiro=[CloudResponse(status=200, body={"schema_version": 1}, etag='"quebrado"')]
    )
    p = poller(tmp_path, cliente, etag='"v1"')

    p.tick()
    p.tick()

    assert cliente.etags == ['"v1"', '"v1"']


def test_falha_de_rede_nao_levanta_e_conta(tmp_path):
    """Se `tick` propagasse, a thread do poller morreria na primeira queda de link. O
    agente seguiria detectando — que é o que a §5.4 quer — e nunca mais receberia
    calibração, sem nada quebrar de forma visível."""
    cliente = ClienteRoteirizado(roteiro=[NetworkError("link caiu")])
    p = poller(tmp_path, cliente)

    assert p.tick() is ConfigOutcome.TENTAR_DEPOIS
    assert p.stats().falhas == 1
    assert p.stats().ultimo_sucesso_s is None


@pytest.mark.parametrize("status", [500, 502, 503, 429, 408])
def test_falha_temporaria_mantem_a_configuracao_em_pe(tmp_path, status):
    """Nuvem em apuros não é motivo para mexer na loja. `429` e `408` são `4xx` e ainda
    assim temporários — tratá-los como recusa definitiva faria o agente registrar um
    problema humano onde só havia congestionamento."""
    aplica = Aplicador()
    p = poller(tmp_path, ClienteRoteirizado(roteiro=[CloudResponse(status=status)]), aplica=aplica)

    assert p.tick() is ConfigOutcome.TENTAR_DEPOIS
    assert aplica.recebidas == []
    assert p.stats().recusados == 0


@pytest.mark.parametrize("status", [401, 403, 404])
def test_recusa_da_nuvem_e_contada_separadamente(tmp_path, status):
    """Credencial errada ou agente desconhecido continua sendo retry — não pode parar o
    poll —, mas o conserto é humano. Contá-la junto com falha de rede esconderia uma
    credencial vencida atrás de "a internet da loja é ruim"."""
    p = poller(tmp_path, ClienteRoteirizado(roteiro=[CloudResponse(status=status)]))

    assert p.tick() is ConfigOutcome.RECUSADA
    assert p.stats().recusados == 1
    assert p.stats().falhas == 1


def test_200_sem_corpo_e_tratado_como_falha_de_transporte(tmp_path):
    """Um `200` vazio é proxy, redirecionamento capturado ou API meio implantada — não
    é configuração. Passá-lo ao loader contaria erro de contrato num problema que é de
    rede, e mandaria procurar o bug no lugar errado."""
    p = poller(tmp_path, ClienteRoteirizado(roteiro=[CloudResponse(status=200, body=None)]))

    assert p.tick() is ConfigOutcome.TENTAR_DEPOIS
    assert p.stats().invalidos == 0


def test_documento_valido_vai_para_o_cache_com_o_etag(tmp_path):
    """O cache é o que faz um reinício sem rede continuar funcionando (§5.4). Guardar o
    `ETag` junto é o que permite ao primeiro poll depois do boot já levar `304` em vez
    de baixar tudo de novo."""
    cache = ConfigCache(tmp_path / "cache.json")
    cliente = ClienteRoteirizado(
        roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO), etag='"v9"')]
    )

    poller(tmp_path, cliente, cache=cache).tick()

    guardado = cache.le()
    assert guardado is not None
    assert guardado.documento["config_version"] == "v1"
    assert guardado.etag == '"v9"'


def test_cache_e_gravado_depois_de_aplicar(tmp_path):
    """Ordem que importa: cachear antes de aplicar guardaria como "última válida" uma
    configuração que o runtime pode ainda recusar por outro motivo. O contrário — o que
    se testa aqui — garante que o cache só contém o que já passou pelo caminho inteiro."""
    cache = ConfigCache(tmp_path / "cache.json")
    ordem: list[str] = []

    def aplica(_config):
        ordem.append("aplica")
        assert cache.le() is None

    cliente = ClienteRoteirizado(roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO))])
    p = ConfigPoller(
        cliente, monta=monta(tmp_path), aplica=aplica, cache=cache, clock=RelogioFalso()
    )
    p.tick()

    assert ordem == ["aplica"]
    assert cache.le() is not None


def test_aplicacao_que_levanta_nao_derruba_o_laco(tmp_path):
    """Um erro dentro do `aplica_config` — que mexe em motores de regra vivos — não pode
    matar a thread do poll. O `_run` engole e conta; este teste fixa que o `tick` é
    quem deixa passar, para o laço poder contabilizar."""
    cliente = ClienteRoteirizado(roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO))])

    def explode(_config):
        raise RuntimeError("motor sumiu")

    p = poller(tmp_path, cliente, aplica=explode)

    with pytest.raises(RuntimeError):
        p.tick()


def test_erro_de_contrato_nao_entra_no_backoff(tmp_path):
    """Documento ilegível não é falha de rede. Se entrasse no backoff, um erro de
    contrato aumentaria o intervalo até o teto — e a loja demoraria minutos para receber
    o documento **corrigido**, que é justamente o que se quer que chegue rápido."""
    cliente = ClienteRoteirizado(
        roteiro=[CloudResponse(status=200, body={"schema_version": 1})], padrao=CloudResponse(304)
    )
    p = poller(tmp_path, cliente)

    p.tick()

    assert p._espera() == INTERVALO_S


def test_falha_isolada_nao_desacelera_o_poll(tmp_path):
    """O backoff soma ao intervalo em vez de substituí-lo, e o intervalo já é 30 s. O
    efeito é que um `502` solto — reinício da API, hiccup do proxy — não muda nada: a
    loja continua no ritmo normal. Desacelerar na primeira falha faria toda recalibração
    depois de um soluço de rede chegar tarde sem motivo."""
    p = poller(tmp_path, ClienteRoteirizado(roteiro=[CloudResponse(502)]))

    p.tick()

    assert p._espera() == INTERVALO_S


def test_queda_prolongada_desacelera_ate_o_teto(tmp_path):
    """Nuvem fora do ar por horas, com todas as lojas insistindo a cada 30 s, é uma
    tempestade em cima de quem já está em apuros. O crescimento só começa quando o
    backoff ultrapassa o piso de 30 s — antes disso não há nada a economizar."""
    cliente = ClienteRoteirizado(padrao=CloudResponse(503))
    p = poller(tmp_path, cliente)

    esperas = []
    for _ in range(8):
        p.tick()
        esperas.append(p._espera())

    assert esperas[0] == INTERVALO_S
    assert esperas[-1] > INTERVALO_S
    assert esperas == sorted(esperas), "a espera nunca pode encolher enquanto a nuvem não volta"
    assert max(esperas) <= 300.0, "o teto do RetryOptions é o que garante que a loja volta a tentar"


def test_primeiro_sucesso_devolve_o_ritmo_normal(tmp_path):
    """Uma queda longa não pode deixar a loja em espera de cinco minutos depois de tudo
    já ter voltado: a próxima recalibração ficaria parada esse tempo todo."""
    cliente = ClienteRoteirizado(padrao=CloudResponse(503))
    p = poller(tmp_path, cliente)
    for _ in range(8):
        p.tick()
    assert p._espera() > INTERVALO_S

    cliente.padrao = CloudResponse(304)
    p.tick()

    assert p._espera() == INTERVALO_S


def test_retry_after_e_piso_e_nunca_atalho(tmp_path):
    """O servidor sabe quando volta, e ignorá-lo martela uma nuvem em apuros. Mas
    obedecer a um `Retry-After: 1` no lugar do backoff desfaria a proteção — a mesma
    regra do envio de evento (§5.4)."""
    cliente = ClienteRoteirizado(roteiro=[CloudResponse(503, retry_after="600")])
    p = poller(tmp_path, cliente)

    p.tick()

    assert p._espera() >= 600.0


def test_stats_nao_levanta_antes_do_primeiro_tick(tmp_path):
    """A linha `[config]` do `--stats` é impressa desde o primeiro segundo, e o poll só
    acontece depois. Um `stats()` que exigisse estado pronto quebraria a saída de quem
    está subindo uma loja pela primeira vez."""
    stats = poller(tmp_path, ClienteRoteirizado(), etag='"v1"').stats()

    assert stats.etag == '"v1"'
    assert stats.ultimo_sucesso_s is None
    assert stats.falhas == 0


def test_config_error_do_loader_e_o_unico_erro_engolido(tmp_path):
    """`ConfigError` é "a nuvem mandou algo que não entendo" e vira contador. Qualquer
    outra exceção é bug do agente e tem que subir até o `_run`, que loga com traceback —
    engolir tudo transformaria um bug em contador silencioso."""
    cliente = ClienteRoteirizado(roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO))])

    def monta_que_explode(_documento):
        raise ValueError("isto não é ConfigError")

    p = ConfigPoller(cliente, monta=monta_que_explode, aplica=Aplicador(), clock=RelogioFalso())

    with pytest.raises(ValueError, match="não é ConfigError"):
        p.tick()


def test_config_error_e_contado_e_nao_propaga(tmp_path):
    """O par do teste acima, pelo lado que tem que ser engolido."""
    cliente = ClienteRoteirizado(roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO))])

    def monta_que_recusa(_documento):
        raise ConfigError("cameras[0].url: esperava texto")

    p = ConfigPoller(cliente, monta=monta_que_recusa, aplica=Aplicador(), clock=RelogioFalso())

    assert p.tick() is ConfigOutcome.NOVA
    assert p.stats().invalidos == 1


def test_200_sem_etag_esquece_o_etag_anterior(tmp_path):
    """Uma API que serve o documento sem `ETag` não sabe responder condicionalmente.
    Continuar mandando o `If-None-Match` da versão anterior pediria um `304` que
    significaria "você ainda tem a v1" — mas o agente já está na v2, e o poll pararia de
    enxergar toda mudança seguinte. Falha permanente e sem sintoma nenhum."""
    cliente = ClienteRoteirizado(
        roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO), etag=None)]
    )
    p = poller(tmp_path, cliente, etag='"v1"')

    p.tick()
    p.tick()

    assert cliente.etags == ['"v1"', None]
    assert p.stats().etag is None


def test_documento_recusado_pelo_runtime_conta_como_recebido_e_nao_como_aplicado(tmp_path):
    """O poller não sabe se o runtime aplicou — e não pode fingir que sabe.

    Um documento válido com mudança estrutural é recebido, validado e cacheado, e o
    runtime o recusa. Contar isso como "aplicado" faria a linha `[config]` do `--stats`
    e, depois, o heartbeat afirmarem que a loja pegou uma calibração que ela não está
    rodando — exatamente a divergência silenciosa entre dashboard e box que o
    `pendente_version` existe para tornar visível.
    """
    cliente = ClienteRoteirizado(roteiro=[CloudResponse(status=200, body=dict(DOCUMENTO))])

    def recusa(_config):
        """O que `AgentRuntime.aplica_config` faz num diff estrutural: não levanta."""

    p = poller(tmp_path, cliente, aplica=recusa)
    p.tick()

    assert p.stats().recebidos == 1
    assert p.stats().invalidos == 0
