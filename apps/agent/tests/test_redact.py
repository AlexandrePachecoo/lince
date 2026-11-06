"""A redação de URL, sozinha.

O que quebra no mundo real se isto regredir: a URL pré-assinada do clipe é a
credencial de escrita no bucket da loja até vencer, e a mensagem de erro que a carrega
sai do box no campo `error` do `PATCH` (§4.4). Um parâmetro que escapa aqui vai parar
no banco da nuvem, ao lado do evento, legível por quem lê a triagem.

As assinaturas abaixo são inventadas; nenhuma é credencial de verdade.
"""

from __future__ import annotations

import pytest

from lince_agent.redact import MASCARA, redige_texto, redige_url

ASSINATURA = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c4b5a69788796a5b4c3d2e1f0"
CREDENCIAL = "CHAVEFALSA0000000000%2F20261106%2Fauto%2Fs3%2Faws4_request"
PRE_ASSINADA = (
    "https://conta.r2.exemplo/lince-clipes/tenant-1/2026-11-06/e1.mp4"
    "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    f"&X-Amz-Credential={CREDENCIAL}"
    "&X-Amz-Date=20261106T120000Z"
    "&X-Amz-Expires=900"
    "&X-Amz-SignedHeaders=host"
    "&X-Amz-Security-Token=sessao-falsa"
    f"&X-Amz-Signature={ASSINATURA}"
)


def test_url_pre_assinada_perde_os_segredos_e_mantem_o_destino():
    """Host e caminho dizem para onde o upload ia, e é isso que se lê no log. A
    assinatura, a credencial e o token de sessão são o que permite escrever no bucket."""
    redigida = redige_url(PRE_ASSINADA)

    assert ASSINATURA not in redigida
    assert "CHAVEFALSA" not in redigida
    assert "sessao-falsa" not in redigida
    assert redigida.startswith("https://conta.r2.exemplo/lince-clipes/tenant-1/2026-11-06/e1.mp4?")
    assert f"X-Amz-Signature={MASCARA}" in redigida
    assert f"X-Amz-Credential={MASCARA}" in redigida
    assert f"X-Amz-Security-Token={MASCARA}" in redigida


def test_data_e_validade_continuam_legiveis():
    """Diante de um `403` do bucket a primeira pergunta é "a URL já tinha vencido?", e
    só `X-Amz-Date` e `X-Amz-Expires` respondem. É o motivo de trocar o valor em vez de
    apagar a query inteira."""
    redigida = redige_url(PRE_ASSINADA)

    assert "X-Amz-Date=20261106T120000Z" in redigida
    assert "X-Amz-Expires=900" in redigida
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in redigida


@pytest.mark.parametrize(
    "nome",
    [
        "X-Amz-Signature",
        "X-Amz-Credential",
        "X-Amz-Security-Token",
        "Signature",
        "token",
        "access_token",
        "sig",
        # A caixa não protege nada: S3 aceita a query em qualquer grafia.
        "x-amz-signature",
        "SIG",
        # Nome com percent-encoding é o mesmo nome para o servidor.
        "X%2DAmz%2DSignature",
    ],
)
def test_cada_parametro_sensivel_e_mascarado(nome):
    redigida = redige_url(f"https://h.exemplo/p?a=1&{nome}=segredo-falso&b=2")

    assert "segredo-falso" not in redigida
    assert redigida == f"https://h.exemplo/p?a=1&{nome}={MASCARA}&b=2"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.exemplo/v1/events",
        "https://api.exemplo/v1/events/e1?triagem=pendentes&limite=5",
        "http://127.0.0.1:3000/v1/agents/config",
        # Encoding peculiar tem que sobreviver: a URL do log precisa ser a que foi usada.
        "https://h.exemplo/a%20b/c?q=x%2By&vazio=&sem-valor",
    ],
)
def test_url_sem_segredo_volta_identica(url):
    """Redação que reescreve URL inocente muda o que se lê no log, e aí o log deixa de
    servir para reproduzir a requisição."""
    assert redige_url(url) == url


def test_userinfo_some_por_inteiro():
    """`usuario:senha@` na URL da API é o mesmo vazamento da senha RTSP. E o usuário
    sozinho pode ser o token (`https://TOKEN@host`), então não sobra nem ele."""
    assert redige_url("https://agente:senha-falsa@api.exemplo/v1") == "https://***@api.exemplo/v1"
    assert redige_url("https://tokenfalso@api.exemplo:8443/v1") == "https://***@api.exemplo:8443/v1"


def test_segredo_no_fragmento_tambem_e_mascarado():
    """Fluxos OAuth põem `access_token` no fragmento."""
    redigida = redige_url("https://h.exemplo/cb#access_token=tok-falso&estado=1")

    assert "tok-falso" not in redigida
    assert redigida.endswith(f"#access_token={MASCARA}&estado=1")


def test_url_ilegivel_nao_vaza_nada():
    """`urlsplit` recusa porta inválida. O último recurso não pode cortar no `@`, senão
    `usuario:senha` atravessa justamente quando a URL é a mais estranha."""
    redigida = redige_url("https://agente:senha-falsa@h.exemplo:porta/p?sig=x")

    assert "senha-falsa" not in redigida
    assert redigida == f"https://{MASCARA}"


def test_texto_livre_tem_toda_url_redigida():
    """O `str()` de exceção da biblioteca às vezes repete a URL, sem contrato nenhum
    sobre quando. O que estiver ao redor dela fica."""
    texto = f"falhou em '{PRE_ASSINADA}' depois de 3 s; ver https://api.exemplo/v1?token=t1"

    redigido = redige_texto(texto)

    assert ASSINATURA not in redigido
    assert "token=t1" not in redigido
    assert redigido.startswith("falhou em 'https://conta.r2.exemplo/lince-clipes/")
    assert "' depois de 3 s; ver https://api.exemplo/v1?token=***" in redigido


def test_texto_sem_url_volta_identico():
    assert redige_texto("<urlopen error [Errno 111] Connection refused>") == (
        "<urlopen error [Errno 111] Connection refused>"
    )


def test_query_sem_esquema_no_texto_tambem_e_redigida():
    """O `InvalidURL` do `http.client` cita só `/caminho?query`. Sem esquema, a URL não
    é reconhecida como URL — e a assinatura atravessaria intacta."""
    texto = "URL can't contain control characters. '/b/e1.mp4?X-Amz-Signature=abc123&a=1'"

    redigido = redige_texto(texto)

    assert "abc123" not in redigido
    assert "'/b/e1.mp4?X-Amz-Signature=***&a=1'" in redigido


def test_nome_que_so_termina_em_parametro_sensivel_nao_e_mascarado():
    """`object_key` não é `key`. Mascarar demais apaga do log o que se precisa para
    depurar — a chave do objeto é exatamente o que liga o evento ao clipe."""
    assert redige_texto("object_key=loja/e1.mp4") == "object_key=loja/e1.mp4"
