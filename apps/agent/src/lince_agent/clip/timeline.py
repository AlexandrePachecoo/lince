"""Conversão entre os dois relógios e seleção da janela do clipe.

Tudo aqui é função pura sobre uma sequência de fragmentos — sem thread, sem lock,
sem I/O. É de propósito: a aritmética de janela é onde um erro não estoura, apenas
entrega um clipe deslocado que continua tocando normalmente. Isolada assim, ela
leva teste tabelado exaustivo.

**As duas réguas.** O gatilho chega do motor de regras carimbado com
`time.monotonic()`, porque é isso que o `Frame` carrega. Os fragmentos, porém, são
posicionados pelo `tfdt` — o timeline de mídia. As duas não são a mesma coisa e a
diferença não é constante conhecida: `-use_wallclock_as_timestamps 1` carimba o
pacote na chegada com o relógio de parede, e o muxer pode ou não normalizar o
primeiro DTS. Por isso o offset é **medido** (`estimate_media_offset`), e só se
usam diferenças dentro de uma mesma sessão.

**Por que não selecionar pelo `received_at`.** Um fragmento só chega quando o GOP
fecha. Na `cam1` (`-g 30`) isso é ~2 s depois do início da mídia que ele carrega;
na `cam2` (`-g 150`), ~10 s. Selecionar por hora de chegada erraria a janela por um
GOP inteiro numa câmera e por quase nada na outra — e o pré/pós-roll reportado no
evento não bateria com o arquivo que o operador assiste.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from lince_agent.ffmpeg.fmp4 import Fragment


def estimate_media_offset(fragments: Sequence[Fragment]) -> float | None:
    """Offset tal que `tempo_de_midia = t_monotonico + offset`.

    Cada amostra pareia o início do fragmento **seguinte** com a chegada do
    atual. O pareamento não é arbitrário: o fragmento *i* é emitido quando seu GOP
    fecha, ou seja, quando a mídia já avançou até onde o fragmento *i+1* começa.
    Parear com o próprio `start_seconds` embutiria um GOP inteiro de viés — 10 s na
    `cam2`, o pós-roll inteiro.

    A **mediana**, e não média, mínimo ou máximo:

    - um fragmento represado na `DropOldestQueue` chega tarde e deflaciona a
      amostra; o mínimo seria justamente esse;
    - um fragmento descartado pela fila deixa um buraco na sequência, e a amostra
      que atravessa o buraco vem inflada de um GOP; o máximo seria esse;
    - a média é envenenada pelos dois.

    Devolve `None` com menos de dois fragmentos: com um só o viés é de um GOP
    inteiro, e chutar seria pior que admitir que não dá para saber.
    """
    if len(fragments) < 2:
        return None
    amostras = [
        fragments[indice + 1].start_seconds - fragments[indice].received_at
        for indice in range(len(fragments) - 1)
    ]
    return median(amostras)


def select_window(fragments: Sequence[Fragment], start_s: float, end_s: float) -> tuple[int, int]:
    """Índices (primeiro, último), **inclusivos**, que cobrem `[start_s, end_s]`.

    Um fragmento cobre de seu `start_seconds` até o do seguinte. Então "cobrir
    `start_s`" é incluir o último fragmento que começa em `start_s` ou antes — não
    o primeiro que começa depois, que faria o clipe pular justamente o pré-roll
    pedido. O mesmo do outro lado: o último fragmento que começa antes de `end_s`
    é o que **contém** o instante final, e sem ele a travessia ficaria de fora.

    Janela pedida antes do que o buffer tem devolve o que existe, em vez de falhar:
    é o "gatilho perto do início do buffer" do §3.5, que degrada de forma
    controlada e vira pré-roll curto registrado no evento.
    """
    if not fragments:
        raise ValueError("não há fragmentos para selecionar")
    if end_s < start_s:
        raise ValueError(f"janela invertida: {start_s}s a {end_s}s")

    primeiro = 0
    for indice, fragment in enumerate(fragments):
        if fragment.start_seconds <= start_s:
            primeiro = indice
        else:
            break

    ultimo = primeiro
    for indice in range(primeiro, len(fragments)):
        if fragments[indice].start_seconds < end_s:
            ultimo = indice
        else:
            break

    return primeiro, ultimo


def measure_rolls(
    fragments: Sequence[Fragment], primeiro: int, ultimo: int, trigger_media_s: float
) -> tuple[float, float]:
    """Pré e pós-roll **efetivos** da seleção, em segundos de mídia.

    O §3.5 exige registrar estes dois números no evento: o operador que recebe um
    clipe com 1 s de pré-roll precisa saber que foi limitação da câmera, não do
    recorte. Eles também são os campos `pré/pós-roll efetivos` da entidade `CLIPE`
    do §6.

    O fim da janela é o **início do fragmento seguinte ao último selecionado**, não
    o início do último: o último fragmento tem um GOP inteiro de duração, e medir
    pelo seu início subestimaria o pós-roll em até 10 s numa câmera de GOP longo.
    Quando não há fragmento seguinte — o buffer termina onde a seleção termina — a
    medida cai para o início do último e sai subestimada, o que é o lado seguro:
    reportar menos pós-roll do que existe nunca faz ninguém procurar no arquivo
    algo que não está lá.
    """
    if not fragments:
        raise ValueError("não há fragmentos para medir")

    inicio = fragments[primeiro].start_seconds
    seguinte = ultimo + 1
    tem_seguinte = seguinte < len(fragments)
    fim = fragments[seguinte if tem_seguinte else ultimo].start_seconds

    return trigger_media_s - inicio, fim - trigger_media_s
