"""A nuvem, roteirizada: um servidor HTTP de verdade numa thread.

Existe como módulo próprio porque duas suítes precisam dele — o cliente HTTP, que
testa o que sai no socket, e o sender, que testa a ordem das chamadas e o que sobra
no disco. Um dublê de `urllib` testaria a nossa leitura da documentação; um socket
de verdade testa o que a loja vai encontrar.
"""

from __future__ import annotations

import http.server
import json
import threading
from dataclasses import dataclass, field

PRAZO_S = 5.0


@dataclass
class Requisicao:
    metodo: str
    caminho: str
    cabecalhos: dict[str, str]
    corpo: bytes


@dataclass
class Resposta:
    status: int = 200
    corpo: dict | None = None
    cabecalhos: dict[str, str] = field(default_factory=dict)
    segura_ate: threading.Event | None = None
    """Quando presente, o handler espera este evento antes de responder. É assim que
    se produz um servidor lento sem `sleep` e sem depender de tempo real."""


class ServidorFalso:
    """A nuvem, roteirizada. Guarda o que recebeu e responde o que mandarem."""

    def __init__(self) -> None:
        self.recebidas: list[Requisicao] = []
        self.roteiro: list[Resposta] = []
        self.padrao = Resposta(status=202, corpo={"schema_version": 1})
        self._lock = threading.Lock()

        servidor = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _atende(self) -> None:
                tamanho = int(self.headers.get("Content-Length") or 0)
                corpo = self.rfile.read(tamanho) if tamanho else b""
                resposta = servidor._registra(
                    Requisicao(self.command, self.path, dict(self.headers), corpo)
                )
                if resposta.segura_ate is not None:
                    resposta.segura_ate.wait(PRAZO_S)

                bruto = json.dumps(resposta.corpo).encode() if resposta.corpo is not None else b""
                try:
                    self.send_response(resposta.status)
                    for nome, valor in resposta.cabecalhos.items():
                        self.send_header(nome, valor)
                    # `304` e `204` não podem ter corpo, e anunciar `Content-Length` num
                    # deles faz o cliente esperar bytes que nunca vêm — em HTTP/1.1 com
                    # conexão persistente, isso é o poll de config travando por timeout
                    # justamente no caminho que roda milhares de vezes por dia.
                    if resposta.status not in (204, 304):
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(bruto)))
                    self.end_headers()
                    if bruto and resposta.status not in (204, 304):
                        self.wfile.write(bruto)
                except (BrokenPipeError, ConnectionResetError):
                    # Esperado no teste de timeout: o cliente desistiu antes.
                    pass

            do_GET = _atende
            do_POST = _atende
            do_PUT = _atende
            do_PATCH = _atende

            def log_message(self, *_args) -> None:
                """O log padrão do http.server suja a saída do pytest."""

        self._servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # `serve_forever` acorda a cada `poll_interval` para ver se pediram parada; o
        # padrão de 0,5 s viraria meio segundo de teardown em cada teste.
        self._thread = threading.Thread(
            target=self._servidor.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()

    def _registra(self, requisicao: Requisicao) -> Resposta:
        with self._lock:
            self.recebidas.append(requisicao)
            return self.roteiro.pop(0) if self.roteiro else self.padrao

    @property
    def url(self) -> str:
        host, porta = self._servidor.server_address[:2]
        return f"http://{host}:{porta}/"

    def encerra(self) -> None:
        self._servidor.shutdown()
        self._servidor.server_close()
        self._thread.join(timeout=PRAZO_S)
