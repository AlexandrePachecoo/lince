"""O argv é o contrato com o ffmpeg. Estes testes o fixam sem executar nada —
inclusive as variantes NVDEC, que a máquina de desenvolvimento não roda."""

from __future__ import annotations

import pytest

from lince_agent.config import DecodeOptions, HwAccel, PixelFormat
from lince_agent.ffmpeg.command import (
    MOVFLAGS,
    build_clip_command,
    build_ingest_command,
    build_probe_command,
    is_live_source,
    is_rtsp,
    redige_credencial,
)

RTSP = "rtsp://cam.local/substream"


def argv(**kwargs) -> list[str]:
    return build_ingest_command(RTSP, DecodeOptions(**kwargs), 3)


def value_after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


def test_duas_saidas_de_uma_conexao():
    """Uma entrada, dois destinos: é o que evita abrir duas sessões RTSP por
    câmera e, com isso, ter duas bases de timestamp independentes."""
    command = argv()
    assert command.count("-i") == 1
    assert "pipe:1" in command
    assert "pipe:3" in command
    assert command.index("-i") < command.index("pipe:1") < command.index("pipe:3")


def test_saida_de_deteccao_e_rawvideo_sem_recodificar_o_buffer():
    command = argv()
    assert command[command.index("pipe:1") - 1] == "rawvideo"
    assert command[command.index("pipe:3") - 1] == "mp4"
    assert value_after(command, "-c") == "copy"


def test_movflags_do_buffer():
    """`empty_moov` é obrigatório em pipe (saída não-buscável) e `frag_keyframe` é
    o que torna cada fragmento descartável isoladamente."""
    flags = value_after(argv(), "-movflags")
    assert flags == MOVFLAGS
    assert "+empty_moov" in flags
    assert "+frag_keyframe" in flags


def test_frag_duration_nunca_aparece():
    """Com frag_keyframe, ele produziria fragmentos começando no meio do GOP —
    não decodificáveis sozinhos, o que quebra o descarte do mais antigo."""
    assert "-frag_duration" not in argv()


def test_nostdin_presente():
    """Sem isso o ffmpeg lê stdin procurando comandos interativos e um byte
    perdido encerra o processo."""
    assert "-nostdin" in argv()


def test_timestamp_e_o_da_borda():
    assert value_after(argv(), "-use_wallclock_as_timestamps") == "1"


def test_wallclock_pode_ser_desligado():
    assert "-use_wallclock_as_timestamps" not in argv(wallclock_timestamps=False)


def test_opcoes_de_fonte_ao_vivo_nao_se_aplicam_a_arquivo():
    """Nenhuma das duas dá erro em mídia gravada — dão perda de frames em silêncio,
    que é pior. `nobuffer` descarta o que foi consumido na análise inicial (um
    segundo inteiro, medido) e o wallclock carimba tudo no mesmo instante, fazendo
    o filtro `fps` colapsar o vídeo a uns poucos frames."""
    arquivo = build_ingest_command("/tmp/gravacao.mp4", DecodeOptions(), 3)
    for flag in ("-use_wallclock_as_timestamps", "-fflags", "-flags"):
        assert flag not in arquivo

    ao_vivo = argv()
    assert value_after(ao_vivo, "-fflags") == "nobuffer"
    assert value_after(ao_vivo, "-flags") == "low_delay"


def test_probesize_vale_para_qualquer_entrada():
    """Ao contrário das opções de fonte ao vivo, esta é só um limite de análise e
    não descarta nada."""
    arquivo = build_ingest_command("/tmp/gravacao.mp4", DecodeOptions(), 3)
    assert value_after(arquivo, "-probesize") == "1000000"


def test_fonte_ao_vivo_hoje_coincide_com_rtsp():
    assert is_live_source("rtsp://cam/x") is True
    assert is_live_source("/tmp/x.mp4") is False


def test_opcoes_rtsp_so_em_url_rtsp():
    """`-timeout` e `-rtsp_transport` são do demuxer RTSP; num arquivo o ffmpeg
    aborta. Condicionar ao esquema é o que permite apontar o agente para um
    arquivo em teste."""
    rtsp = build_ingest_command(RTSP, DecodeOptions(), 3)
    assert value_after(rtsp, "-rtsp_transport") == "tcp"
    assert value_after(rtsp, "-timeout") == "5000000"
    assert value_after(rtsp, "-allowed_media_types") == "video"

    arquivo = build_ingest_command("/tmp/x.mp4", DecodeOptions(), 3)
    for flag in ("-rtsp_transport", "-timeout", "-allowed_media_types"):
        assert flag not in arquivo


def test_timeout_em_microssegundos():
    command = build_ingest_command(RTSP, DecodeOptions(socket_timeout_s=2.5), 3)
    assert value_after(command, "-timeout") == "2500000"


def test_analyzeduration_em_microssegundos():
    command = argv(analyze_duration_s=1.5)
    assert value_after(command, "-analyzeduration") == "1500000"


@pytest.mark.parametrize(
    ("fps", "esperado"),
    [(3.0, "fps=3,scale=640:480"), (7.5, "fps=7.5,scale=640:480")],
)
def test_cadeia_de_filtros_em_cpu(fps: float, esperado: str):
    assert value_after(argv(sample_fps=fps), "-vf") == esperado


def test_sem_hwaccel_por_padrao():
    assert "-hwaccel" not in argv()


def test_cuda_baixa_os_frames_para_a_ram():
    """`-hwaccel cuda` sozinho: sem `-hwaccel_output_format`, o ffmpeg desce os
    frames da VRAM sozinho, que é o que o pipe rawvideo exige."""
    command = argv(hwaccel=HwAccel.CUDA)
    assert value_after(command, "-hwaccel") == "cuda"
    assert "-hwaccel_output_format" not in command
    assert value_after(command, "-vf") == "fps=3,scale=640:480"


def test_cuda_gpu_filter_amostra_antes_de_descer_pelo_pcie():
    command = build_ingest_command(
        RTSP,
        DecodeOptions(hwaccel=HwAccel.CUDA_GPU_FILTER, pixel_format=PixelFormat.NV12),
        3,
    )
    assert value_after(command, "-hwaccel_output_format") == "cuda"
    chain = value_after(command, "-vf")
    assert chain == "fps=3,scale_cuda=640:480,hwdownload,format=nv12"
    # A amostragem vem antes da escala e do download: não se gasta GPU nem PCIe
    # com frame que vai ser descartado.
    assert chain.index("fps=") < chain.index("scale_cuda") < chain.index("hwdownload")


def test_cuda_gpu_filter_exige_nv12():
    """hwdownload não converte frames CUDA para BGR24; a configuração precisa
    falhar na subida, não num filtro obscuro do ffmpeg."""
    with pytest.raises(ValueError, match="NV12"):
        DecodeOptions(hwaccel=HwAccel.CUDA_GPU_FILTER, pixel_format=PixelFormat.BGR24)


def test_fd_do_fragmento_e_respeitado():
    """O ffmpeg aceita qualquer número de descritor; quem manda é o pass_fds."""
    assert "pipe:17" in build_ingest_command(RTSP, DecodeOptions(), 17)


def test_fd_negativo_e_rejeitado():
    with pytest.raises(ValueError, match="fragment_fd"):
        build_ingest_command(RTSP, DecodeOptions(), -1)


@pytest.mark.parametrize(
    ("bruta", "esperada"),
    [
        (
            "rtsp://operador:s3nha-super-secreta@cam.loja:554/sub/stream",
            "rtsp://***@cam.loja:554/sub/stream",
        ),
        (
            "rtsps://operador:s3nha-super-secreta@cam.loja/sub",
            "rtsps://***@cam.loja/sub",
        ),
        ("rtsp://admin@cam.local:554/live", "rtsp://***@cam.local:554/live"),
        (
            "RTSP://Operador:S3nha@Host.Local:554/Path?x=1",
            "RTSP://***@Host.Local:554/Path?x=1",
        ),
        (
            "rtsp://op:seg%40redo%3A1@10.0.0.8:8554/cam/1?a=%40",
            "rtsp://***@10.0.0.8:8554/cam/1?a=%40",
        ),
        (
            "rtsp://user:my password@cam.local/path",
            "rtsp://***@cam.local/path",
        ),
        (
            "rtsp://user:p@ss@cam.local/path",
            "rtsp://***@cam.local/path",
        ),
        ("rtsp://cam.local:554/substream?profile=1", "rtsp://cam.local:554/substream?profile=1"),
        ("rtsp://cam.local/path?email=a@b.com", "rtsp://cam.local/path?email=a@b.com"),
        ("/tmp/gravacao.mp4", "/tmp/gravacao.mp4"),
        (
            "rtsp://user:pass@[2001:db8::1]:554/cam",
            "rtsp://***@[2001:db8::1]:554/cam",
        ),
    ],
)
def test_redige_credencial(bruta: str, esperada: str):
    """A senha sai, o host e o caminho ficam. Sem credencial, a string volta intacta —
    um `@` na query não é userinfo, e redigi-lo esconderia parâmetro legítimo."""
    assert redige_credencial(bruta) == esperada


def test_redige_linha_que_o_ffmpeg_escreve_no_stderr():
    """Medido no ffmpeg 6: ao falhar a abertura, o stderr repete a URL inteira, senha
    inclusive. O ponto final é pontuação da frase; a outra forma é a do ffprobe, com
    a mensagem de erro colada na URL por dois-pontos."""
    abertura = "Error opening input file rtsp://usuario:senha-secreta@127.0.0.1:9/canal."
    assert redige_credencial(abertura) == ("Error opening input file rtsp://***@127.0.0.1:9/canal.")
    recusa = "rtsp://usuario:senha-secreta@127.0.0.1:9/canal: Connection refused"
    assert redige_credencial(recusa) == "rtsp://***@127.0.0.1:9/canal: Connection refused"
    assert "senha-secreta" not in redige_credencial(abertura)
    assert "senha-secreta" not in redige_credencial(recusa)
    assert "127.0.0.1:9/canal" in redige_credencial(abertura)


def test_argv_preserva_a_credencial_para_o_ffmpeg():
    """A redação é para log e exceção. O demuxer autentica com a URL real; tirá-la
    do argv faria toda câmera com senha falhar com 401 e o log parecer limpo."""
    url = "rtsp://operador:s3nha-super-secreta@cam.loja:554/substream"
    assert url in build_ingest_command(url, DecodeOptions(), 3)
    assert build_probe_command(url, DecodeOptions())[-1] == url


def test_is_rtsp():
    assert is_rtsp("rtsp://x/y")
    assert is_rtsp("RTSPS://x/y")
    assert not is_rtsp("http://x/y")
    assert not is_rtsp("/caminho/arquivo.mp4")


def test_probe_pede_json_do_primeiro_stream_de_video():
    command = build_probe_command(RTSP, DecodeOptions())
    assert value_after(command, "-select_streams") == "v:0"
    assert value_after(command, "-of") == "json"
    assert command[-1] == RTSP


def test_comando_de_clipe_nao_recodifica():
    """`-c copy` é o que torna o corte barato o bastante para caber no §3.7. Num
    box com a GPU ocupada pela inferência, recodificar 15 s de vídeo a cada evento
    competiria justamente com a detecção."""
    command = build_clip_command("/tmp/clipe.mp4")
    assert value_after(command, "-c") == "copy"
    assert "-c:v" not in command
    assert "libx264" not in command


def test_comando_de_clipe_pede_faststart_e_timestamp_zerado():
    """`+faststart` põe o `moov` antes do `mdat`, que é o que o player do dashboard
    exige; `make_zero` evita um arquivo cuja barra de progresso começa no epoch."""
    command = build_clip_command("/tmp/clipe.mp4")
    assert value_after(command, "-movflags") == "+faststart"
    assert value_after(command, "-avoid_negative_ts") == "make_zero"


def test_comando_de_clipe_nao_corta_por_tempo():
    """Sem `-ss` e sem `-t` de propósito: a seleção já aconteceu sobre fragmentos,
    que são a unidade de corte. `-ss` com `-c copy` ancoraria no keyframe mais
    próximo e só mascararia o alinhamento que o §3.5 assume explícito."""
    command = build_clip_command("/tmp/clipe.mp4")
    assert "-ss" not in command
    assert "-t" not in command


def test_comando_de_clipe_descarta_audio():
    """R-9: o clipe é o único artefato que sai da loja. Áudio de mercado é dado
    pessoal que ninguém pediu, e a ingestão já o recusa no demuxer."""
    command = build_clip_command("/tmp/clipe.mp4")
    assert "-an" in command
    assert "-sn" in command
    assert "-dn" in command


def test_comando_de_clipe_le_da_stdin():
    """Os bytes já estão em RAM. Um arquivo bruto intermediário seria vídeo em
    disco que o NFR-3 não pede — e que sobreviveria a um crash."""
    command = build_clip_command("/tmp/clipe.mp4")
    assert value_after(command, "-i") == "pipe:0"
    assert value_after(command, "-f") == "mp4", "pipe não é buscável: o formato tem que vir dado"
    assert command[-1] == "/tmp/clipe.mp4"


def test_comando_de_clipe_nao_desliga_a_stdin():
    """`-nostdin` está na ingestão e **não** pode estar aqui: é justamente pela
    stdin que a concatenação chega."""
    assert "-nostdin" not in build_clip_command("/tmp/clipe.mp4")
