"""Montagem do argv do ffmpeg. Função pura: configuração entra, lista de strings sai.

Ser pura é o que permite testar toda a matriz de opções sem executar ffmpeg nenhum
— inclusive as variantes NVDEC, que a máquina de desenvolvimento não roda.

O comando tem **duas saídas a partir de uma única conexão RTSP**, e isso não é
otimização: o estágio 2 precisa de frames decodificados, e o buffer circular do
estágio 5 precisa dos pacotes comprimidos, porque 30 s de BGR24 640x480 ocupam
~414 MB por câmera contra ~2 MB do H.264 do substream — e porque `-c copy` só
existe sobre pacote comprimido. Duas conexões RTSP por câmera resolveriam o mesmo
problema, mas dariam duas bases de timestamp independentes, e aí alinhar o t0 do
gatilho com o clipe vira estimativa.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from lince_agent.config import DecodeOptions, HwAccel

MOVFLAGS = "+frag_keyframe+empty_moov+default_base_moof"
"""Flags do muxer MP4 na saída do buffer.

- `empty_moov` é **obrigatório** para saída não-buscável: num pipe o ffmpeg não
  pode voltar ao início do arquivo para escrever a tabela de samples no fim.
- `frag_keyframe` inicia um fragmento novo a cada keyframe. É isto que viabiliza o
  buffer circular: cada fragmento é decodificável de forma independente, então
  descartar o mais antigo nunca quebra os demais.
- `default_base_moof` deixa os offsets relativos ao próprio `moof` (padrão CMAF),
  o que torna o fragmento autocontido, sem depender da posição no arquivo.

Não acrescente `-frag_duration` aqui. Combinado com `frag_keyframe`, ele produz
fragmentos que começam no meio do GOP — não decodificáveis isoladamente, o que
quebra a garantia de "descarta o mais antigo". Se o GOP da câmera for longo demais,
a correção é no intervalo de I-frame da câmera.
"""

_RTSP_SCHEMES = frozenset({"rtsp", "rtsps"})


def is_rtsp(url: str) -> bool:
    return urlsplit(url).scheme.lower() in _RTSP_SCHEMES


def build_ingest_command(
    url: str,
    decode: DecodeOptions,
    fragment_fd: int,
    *,
    ffmpeg_bin: str = "ffmpeg",
) -> list[str]:
    """Argv completo da ingestão de uma câmera.

    Os frames decodificados saem em `pipe:1` (stdout) e os fragmentos fMP4 em
    `pipe:{fragment_fd}` — o ffmpeg aceita qualquer número de descritor, não
    precisa ser 3; quem manda é o `pass_fds` do lado Python.
    """
    argv = [ffmpeg_bin, "-hide_banner", "-nostdin", "-nostats"]
    argv += ["-loglevel", decode.log_level]
    argv += _hwaccel_args(decode.hwaccel)
    argv += _input_args(url, decode)
    argv += ["-i", url]
    argv += _detection_output_args(decode)
    argv += _buffer_output_args(fragment_fd)
    return argv


def _hwaccel_args(hwaccel: HwAccel) -> list[str]:
    match hwaccel:
        case HwAccel.NONE:
            return []
        case HwAccel.CUDA:
            # Sem `-hwaccel_output_format`, o ffmpeg baixa os frames da VRAM para a
            # RAM do sistema automaticamente — que é o que o pipe rawvideo exige.
            return ["-hwaccel", "cuda"]
        case HwAccel.CUDA_GPU_FILTER:
            # Os frames ficam na VRAM; a descida para a RAM acontece no
            # `hwdownload` do fim da cadeia de filtros, depois da amostragem.
            return ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    raise ValueError(f"hwaccel desconhecido: {hwaccel}")


def is_live_source(url: str) -> bool:
    """Se a entrada é um stream contínuo em vez de mídia gravada.

    Hoje coincide com `is_rtsp` porque a única fonte do agente é câmera RTSP
    (§3.1). São conceitos diferentes, e por isso predicados diferentes: um novo
    protocolo ao vivo (`srt://`, `udp://`) entraria aqui e **não** nas opções do
    demuxer RTSP.
    """
    return is_rtsp(url)


def _input_args(url: str, decode: DecodeOptions) -> list[str]:
    argv: list[str] = []

    if is_rtsp(url):
        # Opções do demuxer RTSP. Passá-las numa entrada que não seja RTSP faz o
        # ffmpeg abortar, então ficam condicionadas ao esquema da URL — é o que
        # permite apontar o agente para um arquivo em teste.
        argv += ["-rtsp_transport", decode.rtsp_transport]
        # Recusa áudio e dados no demuxer, antes de entrarem no processo. É mais
        # forte que `-an`, que só descarta na saída: áudio de loja é dado pessoal
        # desnecessário (R-9), e o melhor tratamento para ele é nunca ingerir.
        argv += ["-allowed_media_types", "video"]
        argv += ["-timeout", str(int(decode.socket_timeout_s * 1_000_000))]

    # Opções que só fazem sentido em fonte ao vivo. Aplicá-las a mídia gravada não
    # dá erro — dá perda de frames em silêncio, que é pior:
    #
    # - `nobuffer` descarta os pacotes consumidos durante a análise inicial em vez
    #   de reemiti-los. Numa câmera isso custa uma fração de segundo no instante da
    #   conexão e evita acumular latência para sempre; num arquivo custa o primeiro
    #   segundo inteiro, medido.
    # - `use_wallclock_as_timestamps` carimba cada pacote com o relógio local na
    #   chegada. Num arquivo, lido na velocidade máxima, tudo chega quase no mesmo
    #   instante e o filtro `fps` colapsa o vídeo inteiro a uns poucos frames.
    #
    # Nos dois casos o comportamento é o correto para o que o §3.1 pede da câmera.
    if is_live_source(url):
        if decode.low_latency:
            argv += ["-fflags", "nobuffer", "-flags", "low_delay"]
        if decode.wallclock_timestamps:
            argv += ["-use_wallclock_as_timestamps", "1"]

    argv += ["-probesize", str(decode.probesize_bytes)]
    argv += ["-analyzeduration", str(int(decode.analyze_duration_s * 1_000_000))]

    return argv


def _detection_output_args(decode: DecodeOptions) -> list[str]:
    return [
        # Explícito de propósito: câmera com dois perfis ou com canal de metadados
        # muda o que a seleção automática do ffmpeg escolheria.
        *("-map", "0:v:0"),
        *("-an", "-sn", "-dn"),
        *("-vf", _filter_chain(decode)),
        *("-pix_fmt", str(decode.pixel_format)),
        *("-f", "rawvideo", "pipe:1"),
    ]


def _filter_chain(decode: DecodeOptions) -> str:
    # `fps` entrega cadência constante duplicando ou descartando. Prefira-o a `-r`
    # (que é mudança de taxa de saída, não amostragem) e a `select` (baseado em
    # expressão, mais difícil de raciocinar). O decode segue em taxa cheia: o ganho
    # aqui é banda de pipe — 13,8 MB/s por câmera a 15 fps contra 2,8 MB/s a 3.
    fps = _format_number(decode.sample_fps)
    scale = f"{decode.width}:{decode.height}"

    if decode.hwaccel is HwAccel.CUDA_GPU_FILTER:
        # A amostragem vem antes da escala para não gastar GPU escalando frame que
        # vai ser descartado, e o hwdownload fecha a cadeia: só os frames que
        # sobreviveram atravessam o PCIe.
        return f"fps={fps},scale_cuda={scale},hwdownload,format=nv12"

    # A escala fixa o tamanho do frame, transformando-o em contrato em vez de
    # descoberta — o pipe rawvideo não tem framing, e errar a dimensão por um pixel
    # embaralha todos os frames em vez de dar erro. Quando o substream já está na
    # resolução alvo, o filtro é passagem direta e custa ~nada.
    return f"fps={fps},scale={scale}"


def _buffer_output_args(fragment_fd: int) -> list[str]:
    if fragment_fd < 0:
        raise ValueError(f"fragment_fd inválido: {fragment_fd}")
    return [
        *("-map", "0:v:0"),
        *("-an", "-sn", "-dn"),
        # Sem recodificação: é o que torna o corte do §3.5 barato o bastante para
        # caber no orçamento de latência. O custo é o alinhamento ao keyframe.
        *("-c", "copy"),
        *("-movflags", MOVFLAGS),
        *("-f", "mp4", f"pipe:{fragment_fd}"),
    ]


def _format_number(value: float) -> str:
    """3.0 vira "3", 7.5 continua "7.5" — o filtro `fps` aceita os dois, mas o
    comando fica legível no log e nos testes."""
    return str(int(value)) if float(value).is_integer() else str(value)


def build_probe_command(
    url: str, decode: DecodeOptions, *, ffprobe_bin: str = "ffprobe"
) -> list[str]:
    """Argv do ffprobe para descobrir o que a câmera realmente entrega.

    O §10 lista como pendência a taxa real de frames do substream, que é a base do
    dimensionamento de decode. Este comando é como esse número é medido.
    """
    argv = [ffprobe_bin, "-hide_banner", "-loglevel", "error"]
    if is_rtsp(url):
        argv += ["-rtsp_transport", decode.rtsp_transport]
        argv += ["-allowed_media_types", "video"]
        argv += ["-timeout", str(int(decode.socket_timeout_s * 1_000_000))]
    argv += ["-probesize", str(decode.probesize_bytes)]
    argv += ["-analyzeduration", str(int(decode.analyze_duration_s * 1_000_000))]
    argv += ["-select_streams", "v:0"]
    argv += ["-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate"]
    argv += ["-of", "json", url]
    return argv
