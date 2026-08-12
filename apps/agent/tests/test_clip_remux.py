"""O remux do clipe, com ffmpeg de verdade.

O que este passo protege é o triador: um MP4 que não abre, ou que abre sem barra
de progresso, transforma um alerta em ruído.

Dois testes aqui existem para registrar o que o remux **não** protege, medido e não
suposto: entrada truncada na cauda vira clipe mais curto em silêncio, e mídia de
sessões misturadas passa com código 0 e stderr vazio. O ffmpeg valida o contêiner,
não a coerência da mídia. É por isso que a fronteira de sessão do `ClipBuffer` é a
única defesa contra o pior defeito deste estágio, e não uma redundância dele.
"""

from __future__ import annotations

import subprocess

import pytest

from lince_agent.clip.remux import RemuxError, remux_to_mp4
from lince_agent.ffmpeg.fmp4 import Fmp4Parser, Fragment, InitSegment, iter_boxes


def concatenado(dados: bytes, quantos: int = 8) -> bytes:
    """Exatamente o que o `ClipBuffer.snapshot()` entrega ao remux."""
    itens = Fmp4Parser().feed(dados, received_at=0.0)
    init = next(item for item in itens if isinstance(item, InitSegment))
    fragmentos = [item for item in itens if isinstance(item, Fragment)][:quantos]
    return init.data + b"".join(fragmento.data for fragmento in fragmentos)


def ffprobe(caminho, *entradas: str, contar: bool = False) -> list[str]:
    resultado = subprocess.run(  # noqa: S603
        [
            *("ffprobe", "-hide_banner", "-loglevel", "error"),
            *("-select_streams", "v:0"),
            *(("-count_frames",) if contar else ()),
            *("-show_entries", ",".join(entradas)),
            *("-of", "csv=p=0"),
            str(caminho),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return resultado.stdout.strip().split(",")


def test_remux_produz_mp4_progressivo_com_moov_no_inicio(fmp4_sessao_longa: bytes, tmp_path):
    """`+faststart`: o fMP4 toca no ffmpeg mas tropeça no player do dashboard, que
    precisa do índice antes da mídia para abrir sem baixar o arquivo inteiro."""
    destino = tmp_path / "clipe.mp4"

    remux_to_mp4(concatenado(fmp4_sessao_longa), destino)

    tipos = [box.type for box in iter_boxes(destino.read_bytes())]
    assert "moov" in tipos
    assert tipos.index("moov") < tipos.index("mdat")
    assert "moof" not in tipos, "a saída é MP4 progressivo, não fragmentado"


def test_remux_normaliza_o_primeiro_timestamp_para_zero(fmp4_sessao_longa: bytes, tmp_path):
    """Captura ao vivo não nasce em zero. Sem `-avoid_negative_ts make_zero`, um
    arquivo cujo primeiro sample está lá adiante no timeline quebra a barra de
    progresso e o cálculo de duração em qualquer player.

    Vale também como documentação executável do que o `-use_wallclock_as_timestamps`
    produz de fato: seja qual for a origem do `tfdt` na entrada, a saída começa em
    zero — e é por isso que o resto do estágio só usa **diferenças**.
    """
    destino = tmp_path / "clipe.mp4"

    remux_to_mp4(concatenado(fmp4_sessao_longa), destino)

    (inicio,) = ffprobe(destino, "stream=start_time")
    assert float(inicio) == pytest.approx(0.0, abs=0.05)


def test_remux_nao_recodifica(fmp4_sessao_longa: bytes, tmp_path):
    """Recodificar estouraria o orçamento do §3.7 num box que já está com a GPU
    ocupada pela inferência. Codec, resolução e contagem de frames têm que
    atravessar intactos."""
    destino = tmp_path / "clipe.mp4"

    remux_to_mp4(concatenado(fmp4_sessao_longa, quantos=8), destino)

    resultado = subprocess.run(  # noqa: S603
        [
            *("ffprobe", "-hide_banner", "-loglevel", "error"),
            *("-select_streams", "v:0", "-count_frames"),
            *("-show_entries", "stream=codec_name,width,height,nb_read_frames"),
            *("-of", "csv=p=0"),
            str(destino),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    codec, largura, altura, frames = resultado.stdout.strip().split(",")
    assert codec == "h264"
    assert (int(largura), int(altura)) == (64, 64)
    assert int(frames) == 80, "8 fragmentos de 10 frames, nenhum perdido no caminho"


def test_duracao_do_arquivo_bate_com_a_medida_reportada(fmp4_sessao_longa: bytes, tmp_path):
    """O número que vai no evento e o arquivo que o humano assiste são a mesma
    coisa. Divergir aqui faria o triador procurar uma travessia que, para ele,
    acontece num instante que não existe no vídeo."""
    destino = tmp_path / "clipe.mp4"

    remux_to_mp4(concatenado(fmp4_sessao_longa, quantos=6), destino)

    resultado = subprocess.run(  # noqa: S603
        [
            *("ffprobe", "-hide_banner", "-loglevel", "error"),
            *("-show_entries", "format=duration", "-of", "csv=p=0"),
            str(destino),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    # 6 fragmentos de 1 s cada na fixture (`-g 10` a 10 fps).
    assert float(resultado.stdout.strip()) == pytest.approx(6.0, abs=0.2)


def test_remux_de_stream_truncado_encurta_o_clipe_em_silencio(fmp4_sessao_longa: bytes, tmp_path):
    """**Medido, não suposto:** o ffmpeg não recusa entrada truncada na cauda. Ele
    sai com código 0, stderr vazio, e escreve um arquivo válido com metade dos
    frames.

    É comportamento correto para fMP4 — num stream ao vivo o último fragmento está
    sempre incompleto — mas destrói a ideia de que o remux serve de validação geral.
    A proteção contra meio fragmento vem de antes: o `Fmp4Parser` só emite
    `Fragment` quando `moof` e `mdat` estão completos, então o buffer nunca chega a
    ter um pela metade. Este teste guarda essa fronteira; se alguém um dia alimentar
    o remux direto do pipe, ficará registrado o que acontece.
    """
    destino = tmp_path / "clipe.mp4"
    dados = concatenado(fmp4_sessao_longa)

    remux_to_mp4(dados[: len(dados) // 2 + 1], destino)

    (frames,) = ffprobe(destino, "stream=nb_read_frames", contar=True)
    assert 0 < int(frames) < 80, "truncar encurta o clipe, sem avisar ninguém"


def test_misturar_sessoes_produz_lixo_que_o_ffmpeg_aceita(
    fmp4_sessao_longa: bytes, fmp4_outra_resolucao: bytes, tmp_path
):
    """A justificativa empírica do `session_id`, e o motivo de ele não ser luxo.

    Init de uma execução com fragmentos de outra: o ffmpeg **aceita**, sai com
    código 0 e escreve um arquivo que abre normalmente e reporta a resolução do
    init. Só que os pixels são de outro vídeo, com outro tamanho — o decoder reclama
    de macroblock corrompido e o operador vê chuvisco.

    Ou seja: o remux valida o contêiner, não a coerência da mídia. Se a fronteira de
    sessão do `ClipBuffer` regredir, **nada** a jusante vai perceber, e o defeito só
    aparece quando um humano abrir o clipe de um alerta.
    """
    itens_b = Fmp4Parser().feed(fmp4_outra_resolucao, received_at=0.0)
    init = next(item for item in itens_b if isinstance(item, InitSegment))
    itens_a = Fmp4Parser().feed(fmp4_sessao_longa, received_at=0.0)
    intrusos = [item for item in itens_a if isinstance(item, Fragment)][:5]
    destino = tmp_path / "misturado.mp4"

    remux_to_mp4(init.data + b"".join(f.data for f in intrusos), destino)

    largura, altura = ffprobe(destino, "stream=width,height")
    assert (int(largura), int(altura)) == (96, 96), (
        "o arquivo herda o cabeçalho do init e nada denuncia a mídia de 64x64 dentro"
    )
    assert destino.stat().st_size > 0


def test_remux_sem_init_segment_falha(fmp4_sessao_longa: bytes, tmp_path):
    """Fragmento sem o `moov` não é reproduzível. É o caso do gatilho que chega
    logo depois de uma reconexão, antes de o init novo ter sido processado."""
    itens = Fmp4Parser().feed(fmp4_sessao_longa, received_at=0.0)
    fragmentos = [item for item in itens if isinstance(item, Fragment)][:4]
    destino = tmp_path / "clipe.mp4"

    with pytest.raises(RemuxError):
        remux_to_mp4(b"".join(f.data for f in fragmentos), destino)

    assert not destino.exists()


def test_remux_de_buffer_vazio_falha_sem_chamar_o_ffmpeg(tmp_path):
    """Reinício do container (§3.5): o buffer em RAM se foi. Vale gastar a
    exceção, não um processo."""
    destino = tmp_path / "clipe.mp4"

    with pytest.raises(RemuxError, match="vazio"):
        remux_to_mp4(b"", destino)

    assert not destino.exists()


def test_remux_respeita_o_prazo(fmp4_sessao_longa: bytes, tmp_path):
    """Um ffmpeg pendurado não pode segurar os clipes das outras câmeras: o
    recorder é uma thread só e o §3.7 tem 15 s de orçamento inteiro.

    O dublê é um script que dorme, e não um mock: o que precisa ser exercitado é o
    encerramento real do processo pelo timeout do `subprocess`, não a nossa
    lembrança de que ele existe. Um ffmpeg que trava sob demanda não é reproduzível.
    """
    pendurado = tmp_path / "ffmpeg-pendurado"
    pendurado.write_text("#!/bin/sh\nsleep 30\n")
    pendurado.chmod(0o755)
    destino = tmp_path / "clipe.mp4"

    with pytest.raises(RemuxError, match="passou de"):
        remux_to_mp4(
            concatenado(fmp4_sessao_longa),
            destino,
            timeout_s=0.3,
            ffmpeg_bin=str(pendurado),
        )


def test_remux_com_binario_inexistente_vira_erro_de_dominio(fmp4_sessao_longa: bytes, tmp_path):
    """Instalação quebrada no box da loja vira `clip_failed` com motivo legível no
    evento, não `FileNotFoundError` derrubando a thread do recorder."""
    with pytest.raises(RemuxError, match="executar o ffmpeg"):
        remux_to_mp4(
            concatenado(fmp4_sessao_longa),
            tmp_path / "clipe.mp4",
            ffmpeg_bin=str(tmp_path / "nao-existe"),
        )


def test_remux_cria_o_diretorio_de_destino(fmp4_sessao_longa: bytes, tmp_path):
    """Primeira execução numa loja nova: o diretório de clipes ainda não existe."""
    destino = tmp_path / "pendentes" / "cam1" / "clipe.mp4"

    remux_to_mp4(concatenado(fmp4_sessao_longa, quantos=3), destino)

    assert destino.stat().st_size > 0
