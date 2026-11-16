"""O registro do agente (§5.1): token de bootstrap → credencial.

O token é de uso único, e é isso que torna cada erro aqui caro: tentar de novo o que a
nuvem já recusou só adia a mensagem que resolve, e desistir do que era só o link
subindo deixa o box sem credencial com um token ainda bom na mão.
"""

from __future__ import annotations

import json
import logging

import pytest
from nuvem_falsa import PRAZO_S, Resposta, ServidorFalso

from lince_agent.outbox.http import CloudResponse, HttpCloudClient, NetworkError
from lince_agent.outbox.policy import Outcome, classify_response
from lince_agent.registro import (
    RegistroIndisponivel,
    RegistroOutcome,
    RegistroRecusado,
    RespostaDeRegistroInvalida,
    classify_register_response,
    registra,
)

BOOTSTRAP = "bs-" + "9f3a" * 16
SEGREDO = "tok-" + "a1b2" * 16
ACEITE = {
    "schema_version": 1,
    "agent_id": "0b5c2c3e-1111-4222-8333-444455556666",
    "tenant_id": "rede-1",
    "store_id": "loja-7",
    "token": SEGREDO,
}


class NuvemRoteirizada:
    """Devolve, em ordem, respostas ou exceções. Sem socket: aqui se testa a decisão."""

    def __init__(self, *roteiro) -> None:
        self.roteiro = list(roteiro)
        self.tokens: list[str] = []

    def post_register(self, bootstrap_token: str) -> CloudResponse:
        self.tokens.append(bootstrap_token)
        passo = self.roteiro.pop(0)
        if isinstance(passo, Exception):
            raise passo
        return passo


def sem_dormir(esperas: list[float]):
    return esperas.append


# --- a classificação ------------------------------------------------------------------


@pytest.mark.parametrize("status", [200, 201])
def test_2xx_com_corpo_e_registro(status):
    assert classify_register_response(status, tem_corpo=True) is RegistroOutcome.REGISTRADO


def test_2xx_sem_corpo_nao_e_registro():
    """Proxy, portal cativo do Wi-Fi da loja, API meio implantada. A rota de verdade
    nunca responde sem corpo, então o token não foi gasto e vale insistir — tratá-lo
    como registro subiria um agente sem credencial nenhuma."""
    assert classify_register_response(200, tem_corpo=False) is RegistroOutcome.TENTAR_DEPOIS


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_recusa_da_nuvem_e_definitiva(status):
    """Não reconhecido, expirado e já usado não mudam com o tempo. Insistir só adia a
    mensagem que resolve: gere outro token."""
    assert classify_register_response(status, tem_corpo=True) is RegistroOutcome.RECUSADO


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_indisponibilidade_se_tenta_de_novo(status):
    assert classify_register_response(status, tem_corpo=True) is RegistroOutcome.TENTAR_DEPOIS


def test_409_nao_significa_aqui_o_que_significa_no_envio_de_evento():
    """No envio, `409` é "já tenho este evento" — sucesso. No registro é "este token já
    virou a credencial de alguém". Reusar `classify_response` deixaria o agente subir
    achando que se registrou, sem credencial, e tomar 401 em tudo."""
    assert classify_response(409) is Outcome.ACCEPTED
    assert classify_register_response(409, tem_corpo=True) is RegistroOutcome.RECUSADO


# --- a troca ---------------------------------------------------------------------------


def test_aceite_vira_credencial():
    nuvem = NuvemRoteirizada(CloudResponse(status=201, body=dict(ACEITE)))

    credencial = registra(nuvem, BOOTSTRAP, dorme=sem_dormir([]))

    assert (credencial.agent_id, credencial.tenant_id, credencial.store_id) == (
        ACEITE["agent_id"],
        "rede-1",
        "loja-7",
    )
    assert credencial.token == SEGREDO
    assert nuvem.tokens == [BOOTSTRAP]


@pytest.mark.parametrize(
    ("status", "erro"),
    [
        (401, "token de bootstrap expirado"),
        (401, "token de bootstrap não reconhecido"),
        (409, "token de bootstrap já foi usado"),
    ],
)
def test_recusa_nao_e_tentada_de_novo_e_diz_o_que_fazer(status, erro):
    """Uma tentativa só, e a mensagem leva o motivo da nuvem: "expirado" e "já foi
    usado" pedem outro token, "não reconhecido" pede conferir se copiou inteiro. Sem o
    motivo, o instalador na loja não sabe qual dos três."""
    nuvem = NuvemRoteirizada(CloudResponse(status=status, body={"erro": erro}))
    esperas: list[float] = []

    with pytest.raises(RegistroRecusado) as recusa:
        registra(nuvem, BOOTSTRAP, dorme=sem_dormir(esperas))

    assert len(nuvem.tokens) == 1
    assert esperas == []
    assert erro in str(recusa.value)
    assert "/box" in str(recusa.value)
    assert "LINCE_BOOTSTRAP_TOKEN" in str(recusa.value)


def test_link_subindo_nao_gasta_a_instalacao():
    """O box acabou de ser ligado e o roteador ainda não fechou a rota. Desistir na
    primeira falha de rede faria toda instalação depender de sorte com o link."""
    nuvem = NuvemRoteirizada(
        NetworkError("POST http://api/v1/agents/register: URLError: sem rota"),
        CloudResponse(status=503),
        CloudResponse(status=201, body=dict(ACEITE)),
    )
    esperas: list[float] = []

    credencial = registra(nuvem, BOOTSTRAP, dorme=sem_dormir(esperas))

    assert credencial.token == SEGREDO
    assert len(esperas) == 2
    assert esperas[1] > esperas[0] - 1.0, "o backoff não pode encolher entre tentativas"


def test_nuvem_fora_do_ar_desiste_com_erro_em_vez_de_ficar_de_pe_e_muda():
    """Sem credencial não há o que operar enquanto espera. Um processo de pé e mudo
    parece box instalado; sair com erro deixa o motivo no log e o Docker reinicia."""
    nuvem = NuvemRoteirizada(*[NetworkError("sem rota")] * 3)
    esperas: list[float] = []

    with pytest.raises(RegistroIndisponivel) as falha:
        registra(nuvem, BOOTSTRAP, tentativas=3, dorme=sem_dormir(esperas))

    assert len(nuvem.tokens) == 3
    assert len(esperas) == 2, "não se espera depois da última tentativa"
    assert "LINCE_API_URL" in str(falha.value)


@pytest.mark.parametrize("faltando", ["agent_id", "tenant_id", "store_id", "token"])
def test_aceite_sem_a_credencial_completa_e_dito_como_token_perdido(faltando):
    """O pior caso: a nuvem consumiu o token e a credencial não chegou inteira. Subir
    com um campo vazio daria um agente que toma 401 em tudo sem dizer por quê."""
    corpo = {chave: valor for chave, valor in ACEITE.items() if chave != faltando}
    nuvem = NuvemRoteirizada(CloudResponse(status=201, body=corpo))

    with pytest.raises(RespostaDeRegistroInvalida) as falha:
        registra(nuvem, BOOTSTRAP, dorme=sem_dormir([]))

    assert "/box" in str(falha.value)
    assert SEGREDO not in str(falha.value)


def test_nenhum_segredo_aparece_no_log_nem_nas_mensagens(caplog):
    """O log do box sai da loja por quem dá suporte. Nem o token de bootstrap, que
    ainda pode valer, nem a credencial, que vale por anos."""
    nuvem = NuvemRoteirizada(
        NetworkError("sem rota"),
        CloudResponse(status=201, body=dict(ACEITE)),
    )

    with caplog.at_level(logging.DEBUG):
        credencial = registra(nuvem, BOOTSTRAP, dorme=sem_dormir([]))

    assert BOOTSTRAP not in caplog.text
    assert SEGREDO not in caplog.text
    assert SEGREDO not in repr(credencial)


# --- o contrato e o socket -------------------------------------------------------------


@pytest.fixture
def nuvem():
    servidor = ServidorFalso()
    yield servidor
    servidor.encerra()


def test_o_pedido_que_sai_no_socket_bate_com_o_contrato(nuvem, validador):
    """O corpo é validado pela API com Ajv contra `agent-register.v1.json`, que tem
    `additionalProperties: false`: um campo a mais vira `400` e o box não se registra."""
    nuvem.roteiro.append(Resposta(status=201, corpo=dict(ACEITE)))
    cliente = HttpCloudClient(nuvem.url, token="credencial-antiga", timeout_s=PRAZO_S)

    registra(cliente, BOOTSTRAP, dorme=sem_dormir([]))

    (pedido,) = nuvem.recebidas
    assert (pedido.metodo, pedido.caminho) == ("POST", "/v1/agents/register")
    corpo = json.loads(pedido.corpo)
    validador("agent-register.v1.json").validate(corpo)
    assert corpo["bootstrap_token"] == BOOTSTRAP


def test_o_registro_nao_leva_authorization(nuvem):
    """É a única rota que se autentica pelo corpo. Mandar junto uma credencial antiga
    seria entregá-la a quem estiver do outro lado de um `LINCE_API_URL` trocado."""
    nuvem.roteiro.append(Resposta(status=201, corpo=dict(ACEITE)))
    cliente = HttpCloudClient(nuvem.url, token="credencial-antiga", timeout_s=PRAZO_S)

    cliente.post_register(BOOTSTRAP)

    cabecalhos = {nome.lower() for nome in nuvem.recebidas[0].cabecalhos}
    assert "authorization" not in cabecalhos


def test_a_resposta_do_contrato_e_a_que_o_agente_sabe_ler(validador):
    """O par do teste acima, do outro lado: a fixtura que os testes de decisão usam é
    um `agent-register-response.v1.json` válido. Sem isto, um campo renomeado no
    contrato passaria na suíte e viraria `RespostaDeRegistroInvalida` na loja, com o
    token já gasto."""
    validador("agent-register-response.v1.json").validate(ACEITE)


def test_a_recusa_da_api_chega_com_o_motivo_pelo_socket(nuvem):
    """`HTTPError` do `urllib` é a resposta, com corpo. Tratado só como exceção, o
    motivo se perde e toda recusa vira "sem detalhe"."""
    nuvem.roteiro.append(Resposta(status=409, corpo={"erro": "token de bootstrap já foi usado"}))
    cliente = HttpCloudClient(nuvem.url, timeout_s=PRAZO_S)

    with pytest.raises(RegistroRecusado) as recusa:
        registra(cliente, BOOTSTRAP, dorme=sem_dormir([]))

    assert recusa.value.status == 409
    assert "já foi usado" in str(recusa.value)
