"""Buffer circular dos últimos 30 s de uma câmera, em RAM (§3.5).

Guarda **fragmentos comprimidos**, não frames decodificados. É a diferença entre
alguns MB e alguns GB por câmera: o `test_o_buffer_comprimido_e_ordens_de_grandeza_menor`
mede o fator, que passa de 100×. Nada disto vai para disco em momento nenhum —
o NFR-3 proíbe gravação contínua, e o único arquivo de vídeo do sistema é o clipe
do evento.

**Sessão.** Toda a sutileza deste módulo está aqui. Cada execução do ffmpeg produz
um init segment próprio e um `tfdt` que recomeça de outra origem; fragmentos de
execuções diferentes são inconcatenáveis. E a ordem de chegada não resolve, porque
os dois callbacks vêm por caminhos distintos: `on_init_segment` é chamado inline na
thread leitora, enquanto o fragmento atravessa a `DropOldestQueue` até a thread
despachante. Depois de uma reconexão, um fragmento da execução anterior pode chegar
**depois** do init da nova. Sem o `session_id` carimbado na origem esse fragmento
seria indistinguível, e o clipe sairia com mídia de duas origens: arquivo que não
abre, ou que abre mostrando lixo — descoberto na triagem, semanas depois.

**Nada aqui pode demorar.** `on_fragment` roda na thread despachante do
`FfmpegIngest`; segurá-la é contrapressão no ffmpeg, que o `ffmpeg/queues.py`
proíbe. Por isso o método só faz append, poda e `notify` — a espera do pós-roll
mora na thread do `ClipRecorder`.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

from lince_agent.clip.state import BufferStats
from lince_agent.config import ClipOptions
from lince_agent.ffmpeg.fmp4 import Fragment, InitSegment

log = logging.getLogger(__name__)


class ClipBuffer:
    """Os últimos `window_s` segundos de uma câmera, prontos para virar clipe."""

    def __init__(self, camera_id: str, options: ClipOptions | None = None) -> None:
        self._camera_id = camera_id
        self._options = options or ClipOptions()

        self._condition = threading.Condition()
        self._fragments: deque[Fragment] = deque()
        self._init: InitSegment | None = None
        self._session_id: int | None = None
        self._retained_bytes = 0
        self._ceiling_evictions = 0
        self._foreign_session_fragments = 0

    @property
    def camera_id(self) -> str:
        return self._camera_id

    # --- entrada: chamada pelas threads do FfmpegIngest ----------------------

    def on_init_segment(self, init: InitSegment) -> None:
        """Init novo significa execução nova do ffmpeg — o buffer inteiro morre.

        Guardar os fragmentos anteriores "por precaução" seria pior que descartá-los:
        eles não podem ser concatenados com este init, e um clipe montado a partir
        deles produz arquivo corrompido em vez de erro.
        """
        with self._condition:
            if init.session_id == self._session_id:
                self._init = init
                return

            if self._session_id is not None:
                log.info(
                    "câmera %s: sessão %s → %s, %d fragmentos descartados",
                    self._camera_id,
                    self._session_id,
                    init.session_id,
                    len(self._fragments),
                )
            self._init = init
            self._session_id = init.session_id
            self._fragments.clear()
            self._retained_bytes = 0
            self._condition.notify_all()

    def on_fragment(self, fragment: Fragment) -> None:
        with self._condition:
            if self._session_id is None or fragment.session_id != self._session_id:
                # Sem init não há como reproduzir; de outra sessão, não há como
                # concatenar. Nos dois casos guardar seria acumular lixo.
                self._foreign_session_fragments += 1
                return

            self._fragments.append(fragment)
            self._retained_bytes += len(fragment)
            self._prune()
            self._condition.notify_all()

    def _prune(self) -> None:
        """Descarta o mais antigo até caber na janela **e** no teto de bytes.

        O teto vence a janela: RAM é limite físico do box da loja, 30 s é desejo de
        produto. Quando ele morde, o pré-roll sai curto — e isso vira
        `ceiling_evictions` no heartbeat e `truncated_pre_roll` no evento, em vez de
        degradação silenciosa.

        Sempre sobra ao menos um fragmento: um buffer que se esvazia sozinho não
        teria como distinguir "câmera sem vídeo" de "teto mal dimensionado".
        """
        while len(self._fragments) > 1:
            span = self._fragments[-1].start_seconds - self._fragments[0].start_seconds
            if span > self._options.window_s:
                self._retained_bytes -= len(self._fragments.popleft())
                continue
            if self._retained_bytes > self._options.max_bytes:
                self._ceiling_evictions += 1
                self._retained_bytes -= len(self._fragments.popleft())
                continue
            break

    # --- saída: chamada pela thread do ClipRecorder -------------------------

    def snapshot(self) -> tuple[InitSegment | None, tuple[Fragment, ...], int | None]:
        """Congela o conteúdo do buffer: `(init, fragmentos, session_id)`.

        Chamado no instante do gatilho para fixar o pré-roll. Sem isso, a poda
        continuaria rodando durante os 10 s de espera do pós-roll e comeria
        justamente os fragmentos que o clipe precisa. Custa uma lista de ponteiros —
        os bytes são imutáveis e não são copiados.
        """
        with self._condition:
            return self._init, tuple(self._fragments), self._session_id

    def wait_for_media_time(self, session_id: int, media_s: float, timeout_s: float) -> bool:
        """Espera até o buffer cobrir `media_s`, ou até o prazo.

        "Cobrir" é ter recebido um fragmento que **começa** em `media_s` ou depois:
        é a prova de que a mídia avançou além do instante pedido, e portanto de que
        o fragmento anterior — o que contém `media_s` — está completo.

        O predicado é avaliado sob o lock, então tanto faz se os fragmentos chegam
        antes ou depois de a thread bloquear aqui. É isso que torna o teste
        determinístico sem `sleep`.

        Devolve `False` no esgotamento do prazo, que é o caso da câmera de GOP longo
        do §3.5: o pós-roll sai curto, marcado, e o alerta segue — o §3.7 não tem
        orçamento para esperar indefinidamente.
        """
        with self._condition:
            return self._condition.wait_for(
                lambda: self._cobre(session_id, media_s), timeout=timeout_s
            )

    def _cobre(self, session_id: int, media_s: float) -> bool:
        if self._session_id != session_id:
            # Reconexão no meio da espera: o pós-roll desta sessão nunca vai chegar.
            # Acordar aqui deixa o recorder cortar o pré-roll que já tinha.
            return True
        return bool(self._fragments) and self._fragments[-1].start_seconds >= media_s

    def fragments_after(self, session_id: int, base_media_decode_time: int) -> tuple[Fragment, ...]:
        """Fragmentos da mesma sessão que começam depois do instante dado.

        É a cauda que chegou durante a espera do pós-roll. Sessão diferente devolve
        vazio: o que chegou pertence a outra execução do ffmpeg.
        """
        with self._condition:
            if self._session_id != session_id:
                return ()
            return tuple(
                fragment
                for fragment in self._fragments
                if fragment.base_media_decode_time > base_media_decode_time
            )

    def stats(self) -> BufferStats:
        with self._condition:
            retained_s = (
                self._fragments[-1].start_seconds - self._fragments[0].start_seconds
                if len(self._fragments) > 1
                else 0.0
            )
            return BufferStats(
                camera_id=self._camera_id,
                session_id=self._session_id,
                has_init=self._init is not None,
                fragments=len(self._fragments),
                retained_bytes=self._retained_bytes,
                retained_s=retained_s,
                ceiling_evictions=self._ceiling_evictions,
                foreign_session_fragments=self._foreign_session_fragments,
            )
