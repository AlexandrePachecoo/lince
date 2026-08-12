"""O buffer circular do §3.5 e a fronteira de sessão.

O bug que estes testes cercam não produz exceção: produz um clipe montado com
mídia de duas execuções do ffmpeg, que ou não abre ou abre mostrando lixo. Ele
aparece semanas depois, na triagem, como "o vídeo do alerta está quebrado" — sem
log, sem stack trace e sem como reproduzir.
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import replace

import pytest

from lince_agent.clip.buffer import ClipBuffer
from lince_agent.config import ClipOptions
from lince_agent.ffmpeg.fmp4 import Fmp4Parser, Fragment, InitSegment

GOP_S = 1.0
"""A fixture `fmp4_sessao_longa` usa `-g 10` a 10 fps: um fragmento por segundo."""

PRAZO_S = 5.0
"""Teto de paciência dos testes de concorrência. Nunca é atingido no caminho
feliz — existe para o teste falhar em vez de pendurar a suíte."""


def janela(window_s: float, **extra: float) -> ClipOptions:
    """`ClipOptions` com uma janela curta e rolls que cabem nela.

    A fixture tem 40 s de mídia; encolher a janela é como se exercita a poda sem
    gerar minutos de vídeo. Os rolls têm que encolher junto — o `ClipOptions`
    recusa configuração em que o buffer descartaria o pré-roll antes do corte.
    """
    return ClipOptions(
        window_s=window_s,
        pre_roll_s=window_s / 5,
        post_roll_s=window_s / 5 * 2,
        post_roll_grace_s=window_s / 5,
        **extra,
    )


def sessao(dados: bytes, *, offset: float = 0.0) -> tuple[InitSegment, list[Fragment]]:
    """Uma execução do ffmpeg, com `received_at` como uma câmera ao vivo o produz.

    O fragmento é emitido quando seu GOP fecha, então ele chega quando a mídia já
    avançou um GOP além do seu início. `offset` é a distância entre o timeline de
    mídia e o relógio monotônico — bytes reais, chegada sintética, resultado
    determinístico sem `sleep`.
    """
    itens = Fmp4Parser().feed(dados, received_at=0.0)
    init = next(item for item in itens if isinstance(item, InitSegment))
    fragmentos = [
        replace(item, received_at=(item.start_seconds + GOP_S) - offset)
        for item in itens
        if isinstance(item, Fragment)
    ]
    return init, fragmentos


def alimenta(buffer: ClipBuffer, fragmentos: list[Fragment]) -> None:
    for fragmento in fragmentos:
        buffer.on_fragment(fragmento)


def test_guarda_apenas_a_janela_configurada(fmp4_sessao_longa: bytes):
    """Buffer sem poda é OOM no box da loja: 30 s × 8 câmeras é o dimensionamento,
    e um buffer que cresce sozinho derruba a detecção de todas elas junto."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", janela(10.0))
    buffer.on_init_segment(init)

    alimenta(buffer, fragmentos)

    stats = buffer.stats()
    assert stats.retained_s <= 10.0
    assert stats.fragments < len(fragmentos)


def test_teto_de_bytes_vence_a_janela_de_tempo(fmp4_sessao_longa: bytes):
    """Câmera com bitrate acima do dimensionamento não pode derrubar o box: ela
    degrada e o número aparece no heartbeat (R-4)."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    teto = sum(len(fragmento) for fragmento in fragmentos[:5])
    buffer = ClipBuffer("cam1", ClipOptions(max_bytes=teto))
    buffer.on_init_segment(init)

    alimenta(buffer, fragmentos)

    stats = buffer.stats()
    assert stats.retained_bytes <= teto
    assert stats.retained_s < 30.0, "o teto de bytes deveria ter mordido antes da janela"
    assert stats.ceiling_evictions > 0, "degradar em silêncio é o que não pode acontecer"


def test_nunca_esvazia_por_causa_do_teto(fmp4_sessao_longa: bytes):
    """Um buffer que se esvazia sozinho não distingue "câmera sem vídeo" de "teto
    mal dimensionado" — e o segundo caso viraria `clip_failed` sem explicação."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", ClipOptions(max_bytes=1))
    buffer.on_init_segment(init)

    alimenta(buffer, fragmentos)

    assert buffer.stats().fragments == 1


def test_init_novo_descarta_a_sessao_anterior(fmp4_sessao_longa: bytes):
    """O bug central do estágio. Reconexão traz init novo e `tfdt` que recomeça de
    outra origem; concatenar as duas sessões produz arquivo corrompido, não erro."""
    init_a, fragmentos_a = sessao(fmp4_sessao_longa)
    init_b, _ = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init_a)
    alimenta(buffer, fragmentos_a[:5])
    assert buffer.stats().fragments == 5

    buffer.on_init_segment(init_b)

    stats = buffer.stats()
    assert stats.fragments == 0
    assert stats.retained_bytes == 0
    assert stats.session_id == init_b.session_id


def test_fragmento_atrasado_de_sessao_antiga_e_ignorado(fmp4_sessao_longa: bytes):
    """A corrida real: `on_init_segment` é chamado inline na thread leitora,
    enquanto o fragmento atravessa a `DropOldestQueue` até a despachante. Depois de
    uma reconexão, um fragmento da execução anterior chega **depois** do init novo.
    Sem o `session_id` ele seria indistinguível de um fragmento legítimo."""
    init_a, fragmentos_a = sessao(fmp4_sessao_longa)
    init_b, fragmentos_b = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init_a)
    alimenta(buffer, fragmentos_a[:3])
    buffer.on_init_segment(init_b)

    # Os retardatários da sessão A chegam agora, depois do init de B.
    alimenta(buffer, fragmentos_a[3:6])
    alimenta(buffer, fragmentos_b[:2])

    stats = buffer.stats()
    assert stats.fragments == 2, "só os fragmentos da sessão corrente entram"
    assert stats.foreign_session_fragments == 3
    _, retidos, _ = buffer.snapshot()
    assert all(fragmento.session_id == init_b.session_id for fragmento in retidos)


def test_fragmento_sem_init_e_ignorado(fmp4_sessao_longa: bytes):
    """Nenhum fragmento é reproduzível sem o init segment. Guardá-lo seria acumular
    bytes que nunca virariam clipe."""
    _, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")

    alimenta(buffer, fragmentos[:3])

    stats = buffer.stats()
    assert stats.fragments == 0
    assert stats.has_init is False
    assert stats.foreign_session_fragments == 3


def test_stats_reportam_segundos_e_bytes_retidos(fmp4_sessao_longa: bytes):
    """`retained_bytes` é a primeira medida real da pendência §10.8 — hoje o
    documento só tem estimativa."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:6])

    stats = buffer.stats()

    assert stats.camera_id == "cam1"
    assert stats.has_init is True
    assert stats.fragments == 6
    assert stats.retained_bytes == sum(len(fragmento) for fragmento in fragmentos[:6])
    assert stats.retained_s == pytest.approx(5.0, abs=0.2)


def test_snapshot_nao_e_afetado_pela_poda_posterior(fmp4_sessao_longa: bytes):
    """O gatilho congela o conteúdo justamente porque a poda continua rodando
    durante os 10 s de espera do pós-roll. Sem isso o pré-roll seria comido antes
    de o corte acontecer."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1", janela(5.0))
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:6])
    _, congelado, _ = buffer.snapshot()

    alimenta(buffer, fragmentos[6:20])

    assert len(congelado) == 6
    assert congelado[0].start_seconds == pytest.approx(fragmentos[0].start_seconds)


def test_snapshot_concatenado_e_reproduzivel(fmp4_sessao_longa: bytes, tmp_path):
    """É exatamente o que o remux vai receber pela stdin. Se a concatenação do
    buffer não for MP4 válido, o corte com `-c copy` não tem do que se alimentar."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:8])

    guardado_init, retidos, _ = buffer.snapshot()
    assert guardado_init is not None
    clipe = tmp_path / "clipe.mp4"
    clipe.write_bytes(guardado_init.data + b"".join(f.data for f in retidos))

    resultado = subprocess.run(  # noqa: S603
        [
            *("ffprobe", "-hide_banner", "-loglevel", "error"),
            *("-select_streams", "v:0"),
            *("-show_entries", "stream=codec_name,nb_read_frames"),
            *("-count_frames", "-of", "csv=p=0"),
            str(clipe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    codec, frames = resultado.stdout.strip().split(",")
    assert codec == "h264"
    assert int(frames) == 80, "8 fragmentos de 10 frames"


def test_wait_retorna_na_hora_se_ja_esta_coberto(fmp4_sessao_longa: bytes):
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:10])

    assert buffer.wait_for_media_time(init.session_id, media_s=5.0, timeout_s=PRAZO_S)


def test_wait_acorda_com_o_fragmento_que_cobre_o_pos_roll(fmp4_sessao_longa: bytes):
    """Os fragmentos chegam **depois** de a thread bloquear: é o caso normal, em
    que o pós-roll ainda não existe no instante do gatilho (§3.5)."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:2])
    alvo = fragmentos[8].start_seconds

    bloqueou = threading.Event()
    resultado: list[bool] = []

    def espera() -> None:
        bloqueou.set()
        resultado.append(buffer.wait_for_media_time(init.session_id, alvo, PRAZO_S))

    thread = threading.Thread(target=espera, name="espera-pos-roll")
    thread.start()
    bloqueou.wait(PRAZO_S)
    alimenta(buffer, fragmentos[2:10])
    thread.join(timeout=PRAZO_S)

    assert not thread.is_alive(), "a Condition não acordou com a chegada dos fragmentos"
    assert resultado == [True]


def test_wait_expira_no_prazo(fmp4_sessao_longa: bytes):
    """Câmera de GOP longo que parou de entregar: o pós-roll nunca chega. O alerta
    não pode esperar para sempre — o §3.7 não tem esse orçamento."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:3])

    assert not buffer.wait_for_media_time(init.session_id, media_s=999.0, timeout_s=0.05)


def test_wait_acorda_quando_a_sessao_muda(fmp4_sessao_longa: bytes):
    """Reconexão no meio da espera: o pós-roll desta sessão nunca vai chegar.
    Continuar esperando seria segurar o corte até o prazo por nada."""
    init_a, fragmentos_a = sessao(fmp4_sessao_longa)
    init_b, _ = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init_a)
    alimenta(buffer, fragmentos_a[:3])

    bloqueou = threading.Event()
    resultado: list[bool] = []

    def espera() -> None:
        bloqueou.set()
        resultado.append(buffer.wait_for_media_time(init_a.session_id, 999.0, PRAZO_S))

    thread = threading.Thread(target=espera, name="espera-sessao")
    thread.start()
    bloqueou.wait(PRAZO_S)
    buffer.on_init_segment(init_b)
    thread.join(timeout=PRAZO_S)

    assert not thread.is_alive(), "a troca de sessão deveria ter acordado a espera"
    assert buffer.snapshot()[2] == init_b.session_id


def test_fragments_after_devolve_so_a_cauda(fmp4_sessao_longa: bytes):
    """A cauda que chegou durante a espera do pós-roll, sem repetir o que o
    snapshot do gatilho já congelou."""
    init, fragmentos = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init)
    alimenta(buffer, fragmentos[:5])
    _, congelado, _ = buffer.snapshot()
    alimenta(buffer, fragmentos[5:9])

    cauda = buffer.fragments_after(init.session_id, congelado[-1].base_media_decode_time)

    assert [f.base_media_decode_time for f in cauda] == [
        f.base_media_decode_time for f in fragmentos[5:9]
    ]


def test_fragments_after_de_outra_sessao_e_vazio(fmp4_sessao_longa: bytes):
    """Depois de uma reconexão não existe cauda: o que chegou é de outra execução e
    não concatena com o init que o clipe está usando."""
    init_a, fragmentos_a = sessao(fmp4_sessao_longa)
    init_b, fragmentos_b = sessao(fmp4_sessao_longa)
    buffer = ClipBuffer("cam1")
    buffer.on_init_segment(init_a)
    alimenta(buffer, fragmentos_a[:4])
    buffer.on_init_segment(init_b)
    alimenta(buffer, fragmentos_b[:4])

    assert buffer.fragments_after(init_a.session_id, 0) == ()
