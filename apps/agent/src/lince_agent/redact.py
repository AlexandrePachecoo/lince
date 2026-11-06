"""Redação de segredos em URL antes de ela virar texto que sai do processo.

O caso que motivou o módulo é o `PUT` do clipe (§4.4): a URL pré-assinada **é** a
credencial. `X-Amz-Signature` e `X-Amz-Credential` estão na query, e quem tem a URL
inteira escreve no bucket da loja até ela vencer. Uma mensagem de erro com essa URL
desce para o log, para o `last_error` do item no Redis e para o campo `error` do
`PATCH` — ou seja, sai do box e vai parar no banco da nuvem, ao lado do evento.

**Troca o valor, não remove a query.** Remover tudo seria mais simples, mas apagaria
justamente o que se precisa para depurar um upload que falhou: `X-Amz-Date` e
`X-Amz-Expires` dizem se a URL já tinha vencido quando o `PUT` saiu, que é a primeira
pergunta diante de um `403` do bucket. O custo é ser lista de negação: um parâmetro
sensível de nome novo passaria. Por isso a lista é generosa, a comparação ignora
caixa, e o *userinfo* (`usuario:senha@`) e o fragmento são tratados por inteiro — não
há parâmetro inocente ali que valha o risco.

Funções puras, sem dependência do resto do agente: qualquer módulo que formata erro
com URL pode usá-las sem puxar o outbox junto.
"""

from __future__ import annotations

import re
from urllib.parse import SplitResult, unquote_plus, urlsplit, urlunsplit

MASCARA = "***"

PARAMETROS_SENSIVEIS = frozenset(
    nome.lower()
    for nome in (
        # SigV4 (S3, R2, MinIO)
        "X-Amz-Signature",
        "X-Amz-Credential",
        "X-Amz-Security-Token",
        # SigV2 e CloudFront
        "Signature",
        "AWSAccessKeyId",
        "Policy",
        "Key-Pair-Id",
        # GCS
        "X-Goog-Signature",
        "X-Goog-Credential",
        "GoogleAccessId",
        # Azure SAS e genéricos
        "sig",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "api_key",
        "apikey",
        "key",
        "secret",
        "password",
        "senha",
    )
)
"""Comparados sem caixa e depois de desfazer o percent-encoding do nome."""

_URL_EM_TEXTO = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
"""Qualquer `esquema://...` até o primeiro espaço ou aspa. Largo de propósito: um
pedaço a mais na URL só aumenta o que passa pela redação."""


_PARAMETRO_EM_TEXTO = re.compile(
    r"(?<![\w%-])("
    + "|".join(re.escape(nome) for nome in sorted(PARAMETROS_SENSIVEIS, key=len, reverse=True))
    + r")=[^&#\s'\"<>]*",
    re.IGNORECASE,
)
"""`nome=valor` solto num texto, para os nomes sensíveis. O lookbehind impede que
`object_key=` case com `key=`: só conta o nome inteiro."""

_ESQUEMA = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://")


def redige_url(url: str) -> str:
    """A mesma URL com o valor de cada segredo trocado por `***`.

    Esquema, host, porta e caminho ficam como estão — são o que diz *para onde* a
    requisição ia, e é isso que se quer ler num log. Parâmetros que não são segredo
    também ficam, byte a byte: a URL sem segredo nenhum volta idêntica.
    """
    try:
        partes = urlsplit(url)
        # `.port` valida a porta e levanta ValueError se ela for lixo; melhor
        # descobrir aqui do que devolver uma URL meio processada.
        _ = partes.port
    except ValueError:
        return _redige_ilegivel(url)

    netloc = partes.netloc
    if "@" in netloc:
        # Usuário sozinho também pode ser o token (`https://TOKEN@host`): some tudo.
        netloc = MASCARA + "@" + netloc.rsplit("@", 1)[1]

    redigida = SplitResult(
        scheme=partes.scheme,
        netloc=netloc,
        path=partes.path,
        query=_redige_parametros(partes.query),
        fragment=_redige_parametros(partes.fragment),
    )
    if redigida == partes:
        # Nada a esconder: devolve a string original, sem passar pela recomposição do
        # `urlunsplit`, que normaliza detalhes (`?` vazio, por exemplo).
        return url
    return urlunsplit(redigida)


def redige_texto(texto: str) -> str:
    """Aplica `redige_url` a toda URL que aparecer num texto livre.

    É para mensagem que não fomos nós que montamos — o `str()` de uma exceção da
    biblioteca, por exemplo, que às vezes inclui a URL inteira.
    """
    sem_urls = _URL_EM_TEXTO.sub(lambda achado: redige_url(achado.group(0)), texto)
    # Segunda rede, para o que não tem esquema: o `InvalidURL` do `http.client` cita
    # só `/caminho?query`, e a primeira passada não o reconhece como URL.
    return _PARAMETRO_EM_TEXTO.sub(lambda achado: f"{achado.group(1)}={MASCARA}", sem_urls)


def _redige_parametros(query: str) -> str:
    """Troca o valor dos parâmetros sensíveis, preservando a grafia dos demais.

    Não usa `parse_qsl`/`urlencode`: a ida e volta reescreveria o encoding de todo
    parâmetro, e a URL que aparece no log deixaria de ser a que foi usada.
    """
    if not query:
        return query
    pedacos = []
    for pedaco in query.split("&"):
        nome, separador, _valor = pedaco.partition("=")
        if unquote_plus(nome).strip().lower() in PARAMETROS_SENSIVEIS:
            pedacos.append(f"{nome}={MASCARA}" if separador else MASCARA)
        else:
            pedacos.append(pedaco)
    return "&".join(pedacos)


def _redige_ilegivel(url: str) -> str:
    """Último recurso para o que `urlsplit` recusa: fica só o esquema. Perde-se
    informação, mas nunca um segredo — cortar no `@` ou no `?` deixaria passar
    `usuario:senha` de uma URL com porta inválida."""
    esquema = _ESQUEMA.match(url)
    return (esquema.group(0) if esquema else "") + MASCARA
