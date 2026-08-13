"""O cliente HTTP contra um servidor de verdade, não contra um dublê.

Um dublê de `urllib` testaria a nossa leitura da documentação. O que quebra em campo
é outra coisa: um `PUT` que sobe zero byte porque o arquivo não foi reaberto, um
`HTTPError` tratado como falha de rede e reenviado para sempre, um servidor que não
responde e pendura a thread de envio junto com a fila inteira. Nada disso aparece sem
um socket real do outro lado.

O servidor roda numa thread, com um roteiro de respostas e um `Event` para segurar a
resposta sob comando — o que dá o teste de timeout sem `sleep` (regra 6 do CLAUDE.md).
"""

from __future__ import annotations

import hashlib
import json
import threading

import pytest
from nuvem_falsa import PRAZO_S, Resposta, ServidorFalso

from lince_agent.outbox.http import CloudResponse, HttpCloudClient, NetworkError


@pytest.fixture
def nuvem():
    servidor = ServidorFalso()
    yield servidor
    servidor.encerra()


@pytest.fixture
def cliente(nuvem):
    return HttpCloudClient(nuvem.url, token="tok-123", timeout_s=PRAZO_S, upload_timeout_s=PRAZO_S)


EVENTO = {"schema_version": 1, "event_id": "e1", "camera_id": "cam"}


def test_post_leva_o_event_id_como_chave_de_idempotencia(nuvem, cliente):
    """Sem a chave, um reenvio depois de um timeout cria um segundo alerta para a
    mesma ocorrência — e alerta repetido é o caminho mais curto para o gerente
    desligar a notificação (R-1)."""
    cliente.post_event(EVENTO, event_id="e1")

    recebida = nuvem.recebidas[0]
    assert recebida.metodo == "POST"
    assert recebida.caminho == "/v1/events"
    assert recebida.cabecalhos["Idempotency-Key"] == "e1"
    assert recebida.cabecalhos["Authorization"] == "Bearer tok-123"
    assert json.loads(recebida.corpo) == EVENTO


def test_resposta_de_aceite_chega_interpretada(nuvem, cliente):
    """A `clip_upload_url` da resposta é o que libera o upload: sem ela o clipe fica
    na fila para sempre, e o §2.3 depende dela para o clipe seguir o alerta."""
    nuvem.roteiro = [
        Resposta(
            status=202,
            corpo={
                "schema_version": 1,
                "event_id": "e1",
                "clip_upload_url": "https://r2.exemplo/loja/e1.mp4?sig=abc",
                "clip_object_key": "loja/e1.mp4",
                "clip_upload_expires_in_s": 900,
            },
        )
    ]

    resposta = cliente.post_event(EVENTO, event_id="e1")

    assert resposta.status == 202
    assert resposta.body["clip_upload_url"].startswith("https://r2.exemplo/")
    assert resposta.body["clip_upload_expires_in_s"] == 900


@pytest.mark.parametrize("status", [400, 401, 409, 422, 429, 500, 503])
def test_erro_http_vira_resposta_e_nao_excecao(nuvem, cliente, status):
    """`HTTPError` do urllib é levantado *e* é a resposta. Tratá-lo só como exceção
    perderia o status — e a política decide tudo a partir dele: um `422` que virasse
    "falha de rede" ficaria reenviando um payload inválido até o TTL."""
    nuvem.roteiro = [Resposta(status=status, corpo={"erro": "detalhe"})]

    resposta = cliente.post_event(EVENTO, event_id="e1")

    assert isinstance(resposta, CloudResponse)
    assert resposta.status == status
    assert resposta.body == {"erro": "detalhe"}


def test_retry_after_chega_ao_chamador(nuvem, cliente):
    nuvem.roteiro = [Resposta(status=429, cabecalhos={"Retry-After": "120"})]

    assert cliente.post_event(EVENTO, event_id="e1").retry_after == "120"


def test_corpo_que_nao_e_json_nao_derruba_o_envio(nuvem, cliente):
    """`502` de proxy vem em HTML e `204` vem vazio. Uma exceção de parsing aqui
    derrubaria a thread de envio com a fila cheia."""
    nuvem.roteiro = [Resposta(status=502, corpo=None)]
    assert cliente.post_event(EVENTO, event_id="e1").body is None

    nuvem.roteiro = [Resposta(status=204, corpo=None)]
    assert cliente.post_event(EVENTO, event_id="e1").status == 204


def test_servidor_que_nao_responde_nao_pendura_o_agente(nuvem):
    """O caso que trava a loja inteira: um socket aberto que nunca responde. Sem
    timeout, a thread de envio fica presa para sempre e a fila só cresce — e nada no
    heartbeat diria o porquê, porque o heartbeat sobe pela mesma thread."""
    solta = threading.Event()
    nuvem.roteiro = [Resposta(status=202, segura_ate=solta)]
    impaciente = HttpCloudClient(nuvem.url, timeout_s=0.2)

    try:
        with pytest.raises(NetworkError, match="POST"):
            impaciente.post_event(EVENTO, event_id="e1")
    finally:
        solta.set()


def test_nuvem_inalcancavel_vira_falha_de_rede():
    """Porta fechada é o estado normal de uma loja com o link caído. Tem que virar
    `NetworkError` — que a política sempre trata como temporário —, nunca uma exceção
    solta subindo pela thread."""
    cliente = HttpCloudClient("http://127.0.0.1:1/", timeout_s=1.0)

    with pytest.raises(NetworkError):
        cliente.post_event(EVENTO, event_id="e1")


def test_patch_vai_para_a_rota_do_evento(nuvem, cliente):
    cliente.patch_event("e1", {"schema_version": 1, "clip": {"status": "ok"}})

    recebida = nuvem.recebidas[0]
    assert recebida.metodo == "PATCH"
    assert recebida.caminho == "/v1/events/e1"


def test_base_url_com_prefixo_nao_perde_o_prefixo(nuvem):
    """`urljoin` come o último segmento quando falta a barra final — o agente
    apontaria para `/v1/events` na raiz e levaria 404 numa API atrás de prefixo."""
    cliente = HttpCloudClient(nuvem.url + "lince", timeout_s=PRAZO_S)

    cliente.post_event(EVENTO, event_id="e1")

    assert nuvem.recebidas[0].caminho == "/lince/v1/events"


def test_put_entrega_os_bytes_exatos_do_clipe(nuvem, cliente, tmp_path):
    """O servidor confere o hash. Um `PUT` que sobe corpo truncado produz um clipe que
    só é descoberto quebrado na triagem, quando alguém precisa dele."""
    clipe = tmp_path / "e1.mp4"
    conteudo = b"\x00\x01mp4" * 5000
    clipe.write_bytes(conteudo)

    resposta = cliente.put_clip(nuvem.url + "upload", clipe)

    assert resposta.status == 202
    recebida = nuvem.recebidas[0]
    assert recebida.metodo == "PUT"
    assert hashlib.sha256(recebida.corpo).hexdigest() == hashlib.sha256(conteudo).hexdigest()
    assert recebida.cabecalhos["Content-Length"] == str(len(conteudo))
    assert recebida.cabecalhos["Content-Type"] == "video/mp4"


def test_put_declara_o_tamanho_em_vez_de_usar_chunked(nuvem, cliente, tmp_path):
    """R2 recusa `Transfer-Encoding: chunked` em URL pré-assinada, e é o que o
    `http.client` faz sozinho quando o corpo é um arquivo sem `Content-Length`."""
    clipe = tmp_path / "e1.mp4"
    clipe.write_bytes(b"x" * 1024)

    cliente.put_clip(nuvem.url + "upload", clipe)

    cabecalhos = nuvem.recebidas[0].cabecalhos
    assert cabecalhos.get("Transfer-Encoding") is None
    assert cabecalhos["Content-Length"] == "1024"


def test_put_reabre_o_arquivo_a_cada_tentativa(nuvem, cliente, tmp_path):
    """Um arquivo já lido está no fim. Reenviar sem reabrir sobe zero byte, o R2
    aceita, e o evento fica com um clipe vazio que ninguém percebe até a triagem."""
    clipe = tmp_path / "e1.mp4"
    clipe.write_bytes(b"y" * 4096)

    nuvem.roteiro = [Resposta(status=500), Resposta(status=200)]
    primeira = cliente.put_clip(nuvem.url + "upload", clipe)
    segunda = cliente.put_clip(nuvem.url + "upload", clipe)

    assert (primeira.status, segunda.status) == (500, 200)
    assert [len(recebida.corpo) for recebida in nuvem.recebidas] == [4096, 4096]


def test_clipe_sumido_do_disco_nao_vira_falha_de_rede(cliente, tmp_path):
    """O teto de disco pode ter despejado o arquivo entre o agendamento e o envio. O
    sender precisa distinguir isso de link caído — reenviar para sempre um arquivo que
    não existe é fila que não drena."""
    with pytest.raises(FileNotFoundError):
        cliente.put_clip("http://127.0.0.1:1/upload", tmp_path / "sumiu.mp4")


def test_user_agent_identifica_a_versao_do_agente(nuvem, cliente):
    """No suporte, saber qual versão do agente mandou o evento é o que separa "essa
    loja não atualizou" de "a atualização quebrou" (R-7)."""
    cliente.post_event(EVENTO, event_id="e1")

    assert nuvem.recebidas[0].cabecalhos["User-Agent"].startswith("lince-agent/")
