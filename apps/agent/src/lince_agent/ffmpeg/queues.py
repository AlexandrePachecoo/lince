"""Fila limitada com descarte do mais antigo.

Existe por uma razão só, e ela é a restrição central do estágio 1:

**Um processo ffmpeg escreve nos dois pipes.** Cada pipe tem um buffer de kernel de
~64 KB. Se o Python parar de ler qualquer um dos dois, esse buffer enche e o ffmpeg
bloqueia **inteiro** — inclusive a outra saída, inclusive a leitura do RTSP, que
passa a acumular no socket e a perder pacotes.

Ou seja: um consumidor lento não pode virar contrapressão no ffmpeg em hipótese
nenhuma. A thread leitora despeja aqui e volta imediatamente para o pipe; quando
esta fila enche, o item mais antigo é descartado. É a política *drop oldest* que o
§3.2 já prescreve para a fila de detecção, aplicada uma camada antes.
"""

from __future__ import annotations

import threading
from collections import deque


class DropOldestQueue[T]:
    def __init__(self, maxsize: int) -> None:
        if maxsize <= 0:
            raise ValueError(f"maxsize deve ser positivo, recebi {maxsize}")
        self._items: deque[T] = deque(maxlen=maxsize)
        self._condition = threading.Condition()
        self._dropped = 0
        self._closed = False

    @property
    def dropped(self) -> int:
        """Quantos itens foram descartados por lentidão do consumidor.

        Métrica de saturação do box: com o link e a câmera saudáveis, um número
        crescendo aqui significa que o consumidor não acompanha a taxa entregue.
        """
        with self._condition:
            return self._dropped

    def put(self, item: T) -> T | None:
        """Nunca bloqueia. Ver o docstring do módulo.

        Devolve o item que não sobreviveu — o mais antigo, quando a fila estava cheia,
        ou o próprio `item`, quando a fila já estava fechada — e `None` quando coube.
        Só a fila sabe **qual** item morreu, e o §5.3 pede `dropped_frames` por câmera:
        com uma fila compartilhada entre todas elas, um contador agregado esconderia
        justamente a câmera que está saturando o box (R-4).
        """
        with self._condition:
            if self._closed:
                return item
            descartado: T | None = None
            if len(self._items) == self._items.maxlen:
                self._dropped += 1
                descartado = self._items[0]
            self._items.append(item)
            self._condition.notify()
            return descartado

    def get(self, timeout: float | None = None) -> T | None:
        """Devolve o próximo item, ou `None` no timeout ou após o fechamento."""
        with self._condition:
            if not self._items and not self._closed:
                self._condition.wait(timeout)
            if self._items:
                return self._items.popleft()
            return None

    def close(self) -> None:
        """Acorda todo mundo que estiver esperando; `get` passa a drenar o que
        sobrou e depois devolve `None`."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    def __len__(self) -> int:
        with self._condition:
            return len(self._items)
