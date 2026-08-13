"""Cliente HTTP da nuvem: as três chamadas do §5.2 que o agente faz.

`urllib` da biblioteca padrão, e não `httpx`, porque o que uma biblioteca de HTTP
compraria aqui — backoff, idempotência, retry — é justamente o que o §5.4 exige que
seja nosso e explícito. Pooling e HTTP/2 não valem nada a uma requisição por minuto, e
o runtime do box fica em `numpy + redis`. Como `CloudClient` é um Protocol, trocar
depois é um módulo.

Duas armadilhas do `urllib` que este módulo esconde do resto do agente:

1. **`HTTPError` é levantado *e* é a resposta.** Ele tem `.code`, `.headers` e `.read()`.
   Tratá-lo só como exceção jogaria fora o corpo e o `Retry-After` — que é
   exatamente a informação de que a política precisa para decidir.
2. **O corpo do `PUT` precisa ser reaberto a cada tentativa.** Um arquivo já lido está
   no fim; reenviar sem reabrir sobe zero byte e o R2 aceita, produzindo um clipe
   vazio que só aparece na triagem.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin

from lince_agent import __version__

log = logging.getLogger(__name__)

_JSON = "application/json"


class NetworkError(Exception):
    """A requisição não chegou a ter resposta: DNS, socket, TLS, timeout.

    Sempre temporário, por definição: sem resposta não há como distinguir "a nuvem
    recusou" de "o link da loja caiu", e a única suposição segura é a segunda.
    """


@dataclass(frozen=True, slots=True)
class CloudResponse:
    """Resposta crua. Quem interpreta é `policy.classify_response`."""

    status: int
    body: dict[str, object] | None = None
    retry_after: str | None = None
    """Cabeçalho `Retry-After` sem interpretar: convertê-lo exige relógio, e este
    módulo não tem nem quer ter um."""


class CloudClient(Protocol):
    """As três chamadas do agente para a nuvem (§5.2)."""

    def post_event(self, payload: dict[str, object], *, event_id: str) -> CloudResponse: ...

    def put_clip(self, url: str, path: Path, *, content_type: str = "video/mp4") -> CloudResponse:
        """`PUT` direto no R2 por URL pré-assinada. Não passa pela API (§4.4)."""
        ...

    def patch_event(self, event_id: str, payload: dict[str, object]) -> CloudResponse: ...


class HttpCloudClient:
    """Implementação sobre `urllib`, sem estado entre requisições."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout_s: float = 10.0,
        upload_timeout_s: float = 120.0,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url é obrigatória")
        # A barra final importa: sem ela, `urljoin` come o último segmento do caminho
        # e um base_url com prefixo (`https://api/lince`) perderia o `lince`.
        self._base_url = base_url if base_url.endswith("/") else base_url + "/"
        self._token = token
        self._timeout_s = timeout_s
        self._upload_timeout_s = upload_timeout_s
        self._opener = opener or urllib.request.build_opener()

    def post_event(self, payload: dict[str, object], *, event_id: str) -> CloudResponse:
        return self._json_request(
            "POST",
            urljoin(self._base_url, "v1/events"),
            payload,
            # Chave de idempotência também no cabeçalho, não só no corpo: é o que
            # permite a um proxy ou à própria API deduplicar sem abrir o payload.
            extra_headers={"Idempotency-Key": event_id},
        )

    def patch_event(self, event_id: str, payload: dict[str, object]) -> CloudResponse:
        return self._json_request(
            "PATCH", urljoin(self._base_url, f"v1/events/{event_id}"), payload
        )

    def put_clip(self, url: str, path: Path, *, content_type: str = "video/mp4") -> CloudResponse:
        tamanho = path.stat().st_size
        with path.open("rb") as arquivo:
            requisicao = urllib.request.Request(  # noqa: S310 - URL vem da própria API
                url,
                data=arquivo,
                method="PUT",
                headers={
                    "Content-Type": content_type,
                    # Obrigatório: sem isto o `http.client` usa `Transfer-Encoding:
                    # chunked`, que o R2 recusa numa URL pré-assinada.
                    "Content-Length": str(tamanho),
                    "User-Agent": self._user_agent,
                },
            )
            return self._enviar(requisicao, timeout_s=self._upload_timeout_s)

    # --- interno ------------------------------------------------------------

    @property
    def _user_agent(self) -> str:
        return f"lince-agent/{__version__}"

    def _json_request(
        self,
        metodo: str,
        url: str,
        payload: dict[str, object],
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> CloudResponse:
        cabecalhos = {
            "Content-Type": _JSON,
            "Accept": _JSON,
            "User-Agent": self._user_agent,
            **(extra_headers or {}),
        }
        if self._token:
            cabecalhos["Authorization"] = f"Bearer {self._token}"

        requisicao = urllib.request.Request(  # noqa: S310 - base_url é configuração
            url,
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            method=metodo,
            headers=cabecalhos,
        )
        return self._enviar(requisicao, timeout_s=self._timeout_s)

    def _enviar(self, requisicao: urllib.request.Request, *, timeout_s: float) -> CloudResponse:
        try:
            with self._opener.open(requisicao, timeout=timeout_s) as resposta:
                return CloudResponse(
                    status=resposta.status,
                    body=_corpo_json(resposta.read(), resposta.headers.get("Content-Type")),
                    retry_after=resposta.headers.get("Retry-After"),
                )
        except urllib.error.HTTPError as erro:
            # Não é falha de rede: é a resposta do servidor, com corpo e cabeçalhos.
            with erro:
                return CloudResponse(
                    status=erro.code,
                    body=_corpo_json(erro.read(), erro.headers.get("Content-Type")),
                    retry_after=erro.headers.get("Retry-After"),
                )
        except (urllib.error.URLError, TimeoutError, OSError) as erro:
            raise NetworkError(f"{requisicao.get_method()} {requisicao.full_url}: {erro}") from erro


def _corpo_json(bruto: bytes, content_type: str | None) -> dict[str, object] | None:
    """Corpo interpretado, ou `None`.

    Um `502` de proxy vem em HTML e um `204` vem vazio; nenhum dos dois pode virar
    exceção no meio do envio, porque a política já sabe o que fazer só com o status.
    """
    if not bruto or (content_type and _JSON not in content_type):
        return None
    try:
        corpo = json.loads(bruto)
    except (ValueError, UnicodeDecodeError):
        log.debug("resposta com Content-Type json mas corpo ilegível (%d bytes)", len(bruto))
        return None
    return corpo if isinstance(corpo, dict) else None
