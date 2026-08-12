"""O parser é o que transforma um pipe de bytes na unidade do buffer circular.

A propriedade que importa, e que todos estes testes cercam: **init segment mais
fragmentos concatenados formam um MP4 válido**. É ela que permite ao estágio 5
cortar com `-c copy` sem recodificar nada.
"""

from __future__ import annotations

import subprocess

import pytest

from lince_agent.ffmpeg.fmp4 import (
    Fmp4ParseError,
    Fmp4Parser,
    Fragment,
    InitSegment,
    find_box,
    iter_boxes,
    parse_timescale,
)


def parse_all(data: bytes, chunk_size: int | None = None) -> list[InitSegment | Fragment]:
    parser = Fmp4Parser()
    if chunk_size is None:
        return parser.feed(data, received_at=0.0)
    items: list[InitSegment | Fragment] = []
    for offset in range(0, len(data), chunk_size):
        items += parser.feed(data[offset : offset + chunk_size], received_at=0.0)
    return items


def test_emite_um_init_e_varios_fragmentos(fmp4_stream: bytes):
    items = parse_all(fmp4_stream)
    inits = [item for item in items if isinstance(item, InitSegment)]
    fragments = [item for item in items if isinstance(item, Fragment)]
    assert len(inits) == 1
    assert len(fragments) >= 4, "-g 10 em 50 frames deveria dar ~5 fragmentos"


def test_init_comeca_com_ftyp_e_contem_moov(fmp4_stream: bytes):
    init = next(item for item in parse_all(fmp4_stream) if isinstance(item, InitSegment))
    assert init.data[4:8] == b"ftyp"
    assert find_box(init.data, ("moov",)) is not None
    assert init.timescale > 0


def test_cada_fragmento_e_moof_seguido_de_mdat(fmp4_stream: bytes):
    fragments = [item for item in parse_all(fmp4_stream) if isinstance(item, Fragment)]
    for fragment in fragments:
        tipos = [box.type for box in iter_boxes(fragment.data)]
        assert tipos == ["moof", "mdat"]


def test_tempos_de_inicio_sao_crescentes(fmp4_stream: bytes):
    """O `tfdt` dá o início de cada fragmento no timeline de mídia. É por ele que
    o estágio 5 vai selecionar a janela de pré-roll e pós-roll."""
    fragments = [item for item in parse_all(fmp4_stream) if isinstance(item, Fragment)]
    tempos = [fragment.start_seconds for fragment in fragments]
    assert tempos == sorted(tempos)
    assert len(set(tempos)) == len(tempos)


def test_reconstrucao_preserva_toda_a_midia(fmp4_stream: bytes):
    """Nenhum byte de mídia se perde: init mais fragmentos reproduzem o stream
    inteiro, tirando só o índice do fim (ver o teste seguinte)."""
    items = parse_all(fmp4_stream)
    reconstruido = b"".join(item.data for item in items)
    assert fmp4_stream.startswith(reconstruido)
    assert len(fmp4_stream) - len(reconstruido) < 512


def test_indice_mfra_do_fim_e_descartado(fmp4_stream: bytes):
    """Ao encerrar, o ffmpeg escreve `mfra`/`mfro` — um índice de *todos* os
    fragmentos daquela execução. Ele não pode ir para o buffer: qualquer clipe é
    um subconjunto dos fragmentos, e um índice que aponta para o que não está no
    arquivo é pior que índice nenhum. Numa câmera rodando de verdade ele nem
    chega a existir, porque o stream não termina."""
    tipos_no_stream = [box.type for box in iter_boxes(fmp4_stream)]
    assert "mfra" in tipos_no_stream

    reconstruido = b"".join(item.data for item in parse_all(fmp4_stream))
    assert "mfra" not in [box.type for box in iter_boxes(reconstruido)]


@pytest.mark.parametrize("chunk_size", [1, 7, 64, 1000, 65536])
def test_independe_do_fatiamento_do_pipe(fmp4_stream: bytes, chunk_size: int):
    """Os chunks do pipe não respeitam fronteira de box nenhuma — chegam do
    tamanho que o kernel resolver entregar."""
    inteiro = parse_all(fmp4_stream)
    fatiado = parse_all(fmp4_stream, chunk_size=chunk_size)
    assert [item.data for item in fatiado] == [item.data for item in inteiro]


def test_reconstrucao_e_reproduzivel(fmp4_stream: bytes, tmp_path):
    """A prova que fecha o estágio 1: o que sai do parser é vídeo de verdade."""
    items = parse_all(fmp4_stream)
    clipe = tmp_path / "clipe.mp4"
    clipe.write_bytes(b"".join(item.data for item in items))

    result = subprocess.run(  # noqa: S603
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
    codec, frames = result.stdout.strip().split(",")
    assert codec == "h264"
    assert int(frames) == 50


def test_fragmento_sozinho_nao_e_reproduzivel(fmp4_stream: bytes, tmp_path):
    """O outro lado da moeda, e a razão de o init segment ser guardado para
    sempre: o fragmento carrega mídia, não o cabeçalho que a descreve."""
    fragment = next(item for item in parse_all(fmp4_stream) if isinstance(item, Fragment))
    solto = tmp_path / "solto.mp4"
    solto.write_bytes(fragment.data)

    result = subprocess.run(  # noqa: S603
        ["ffprobe", "-hide_banner", "-loglevel", "error", str(solto)],
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0


def test_timescale_ausente_e_erro_explicito():
    with pytest.raises(Fmp4ParseError, match="mdhd"):
        parse_timescale(b"\x00\x00\x00\x08ftyp")


def test_box_de_tamanho_zero_e_recusado():
    """Num arquivo significa "até o fim"; num pipe ao vivo não existe fim."""
    with pytest.raises(Fmp4ParseError, match="tamanho 0"):
        list(iter_boxes(b"\x00\x00\x00\x00mdat"))


def test_box_incompleto_apenas_aguarda():
    """Faltar bytes é o estado normal de um pipe, não erro."""
    assert list(iter_boxes(b"\x00\x00\x00\x40mdat\x01\x02")) == []


def test_moof_sem_init_e_erro():
    with pytest.raises(Fmp4ParseError, match="ftyp/moov"):
        Fmp4Parser().feed(b"\x00\x00\x00\x08moof", received_at=0.0)
