"""CLI de desenvolvimento: argumentos e formatação da saída."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

import pytest

from lince_agent import __main__ as cli
from lince_agent.config import HwAccel, OutboxOptions, PixelFormat
from lince_agent.detect.state import CameraDetectionStats, DetectorInfo, DetectorStats
from lince_agent.ffmpeg.fmp4 import Fragment, InitSegment
from lince_agent.ffmpeg.probe import ProbeError, StreamInfo
from lince_agent.ingest.state import CameraHealth, CameraStatus
from lince_agent.outbox.state import OutboxStats, SenderStats
from lince_agent.outbox.store import MemoryOutbox
from lince_agent.rules.state import CameraRuleStats, RuleStats
from lince_agent.runtime import AgentHealth
from lince_agent.track.state import CameraTrackingStats, TrackerStats


def test_padroes_batem_com_a_arquitetura():
    """3 fps e 640x480 são o que o §3.1 e o §3.2 especificam; mudar aqui em
    silêncio mudaria o dimensionamento inteiro."""
    args = cli.build_parser().parse_args(["--camera", "rtsp://x/y"])
    assert args.fps == 3.0
    assert (args.width, args.height) == (640, 480)
    assert args.pix_fmt == PixelFormat.BGR24.value
    assert args.hwaccel == HwAccel.NONE.value


def test_camera_e_obrigatoria():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_pix_fmt_invalido_e_recusado():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--camera", "rtsp://x", "--pix-fmt", "rgb999"])


def saude(**kwargs) -> CameraHealth:
    base = {
        "camera_id": "cam1",
        "status": CameraStatus.OK,
        "consecutive_failures": 0,
        "restarts": 0,
        "sampled_fps": 3.0,
        "frames": 42,
        "frames_dropped": 0,
        "fragments": 7,
        "fragments_dropped": 0,
        "fragment_bytes": 2048,
        "last_frame_at": None,
    }
    return CameraHealth(**(base | kwargs))


def test_linha_de_status_traz_o_essencial():
    linha = cli._format_health(saude(), 921_600)
    for pedaço in ("cam1", "ok", "42", "3.00 fps", "921600"):
        assert pedaço in linha


def test_camera_sem_frame_nao_mente_sobre_o_ultimo():
    assert "nunca" in cli._format_health(saude(frames=0, last_frame_at=None), 921_600)


def test_probe_only_nao_sobe_o_pipeline(monkeypatch):
    subiu = False

    class SupervisorFalso:
        def __init__(self, *a, **k) -> None:
            nonlocal subiu
            subiu = True

    monkeypatch.setattr(cli, "CameraSupervisor", SupervisorFalso)
    monkeypatch.setattr(
        cli,
        "probe_stream",
        lambda *a, **k: StreamInfo("h264", 640, 480, 15.0, 15.0),
    )
    assert cli.main(["--camera", "rtsp://x/y", "--probe-only"]) == 0
    assert not subiu


def test_camera_inalcancavel_sai_com_erro(monkeypatch):
    def falha(*a, **k):
        raise ProbeError("Connection refused")

    monkeypatch.setattr(cli, "probe_stream", falha)
    assert cli.main(["--camera", "rtsp://x/y"]) == 1


def test_fila_em_ram_avisa_que_nao_e_duravel(caplog):
    """`--outbox memory` é andaime. Sem o aviso alto, é fácil rodar meia hora achando
    que a fila é durável e descobrir no primeiro restart que não era (ADR-004)."""
    args = cli.build_parser().parse_args(["--camera", "rtsp://x", "--outbox", "memory"])

    with caplog.at_level("WARNING"):
        fila = cli._monta_fila(args, OutboxOptions())

    assert isinstance(fila, MemoryOutbox)
    assert "restart" in caplog.text


def test_primeiro_gatilho_respeita_after_e_every():
    def quando(argv: list[str]) -> float | None:
        return cli._primeiro_gatilho(cli.build_parser().parse_args(["--camera", "rtsp://x", *argv]))

    assert quando([]) is None
    assert quando(["--trigger-after", "5"]) is not None
    assert quando(["--trigger-every", "20"]) is not None


class RuntimeFalso:
    def __init__(self) -> None:
        self.gatilhos: list[str] = []
        self.parado = False

    camera_ids = ("cam1", "cam2")

    def start(self) -> None:
        pass

    def stop(self, timeout: float = 30.0) -> None:
        self.parado = True

    def trigger(self, camera_id: str) -> str:
        self.gatilhos.append(camera_id)
        return f"evento-{len(self.gatilhos)}"

    def health(self) -> AgentHealth:
        return AgentHealth(cameras=(saude(),))


def test_gatilho_de_andaime_atinge_todas_as_cameras():
    runtime = RuntimeFalso()

    cli._dispara(runtime, "teste")

    assert runtime.gatilhos == ["cam1", "cam2"]


def test_trigger_after_dispara_uma_vez_e_para(monkeypatch, tmp_path):
    """O andaime tem que produzir evento sem o estágio 4 existir — é assim que o
    caminho gatilho → clipe → fila → nuvem é exercitado hoje."""
    runtime = RuntimeFalso()
    monkeypatch.setattr(cli, "AgentRuntime", lambda *a, **k: runtime)
    monkeypatch.setattr(cli, "probe_stream", lambda *a, **k: StreamInfo("h264", 640, 480, 15, 15))

    cli.main(
        [
            *("--camera", "rtsp://x/y", "--clips-dir", str(tmp_path)),
            *("--trigger-after", "0", "--duration", "0.1", "--dry-run"),
        ]
    )

    assert runtime.gatilhos == ["cam1", "cam2"], "um gatilho por câmera, uma vez só"
    assert runtime.parado, "o agente tem que ser desmontado mesmo saindo por --duration"


@pytest.mark.skipif(not hasattr(signal, "SIGUSR1"), reason="SIGUSR1 não existe nesta plataforma")
def test_sigusr1_dispara_um_evento(monkeypatch, tmp_path):
    """`kill -USR1` é como se testa uma câmera recém-instalada sem esperar alguém
    passar na frente dela.

    O handler só marca um `Event`; quem chama `trigger()` é o laço principal. Handler
    de sinal interrompe qualquer thread em qualquer ponto, inclusive segurando o lock
    que o próprio `trigger` iria querer.
    """
    runtime = RuntimeFalso()
    monkeypatch.setattr(cli, "AgentRuntime", lambda *a, **k: runtime)
    monkeypatch.setattr(cli, "probe_stream", lambda *a, **k: StreamInfo("h264", 640, 480, 15, 15))
    anterior = signal.getsignal(signal.SIGUSR1)

    def manda_sinal() -> None:
        # Espera o handler existir em vez de dormir um tempo fixo: o sinal enviado
        # antes da instalação mataria o processo de teste (SIGUSR1 default é terminar).
        limite = time.monotonic() + 5.0
        while time.monotonic() < limite:
            if signal.getsignal(signal.SIGUSR1) is not anterior:
                os.kill(os.getpid(), signal.SIGUSR1)
                return
            time.sleep(0.01)

    threading.Thread(target=manda_sinal, daemon=True).start()
    try:
        cli.main(
            [
                *("--camera", "rtsp://x/y", "--clips-dir", str(tmp_path)),
                *("--duration", "2", "--dry-run"),
            ]
        )
    finally:
        signal.signal(signal.SIGUSR1, anterior)

    assert runtime.gatilhos == ["cam1", "cam2"]


def test_dry_run_nao_fala_com_a_nuvem_e_libera_o_upload(tmp_path):
    """O `--dry-run` precisa devolver `clip_upload_url`, senão o caminho do clipe nunca
    é exercitado — que é justamente o que se quer ver rodando sem uma API do lado."""
    clipe = tmp_path / "e1.mp4"
    clipe.write_bytes(b"mp4")
    cliente = cli.ClienteSeco()

    aceite = cliente.post_event({"camera_id": "cam1", "clip": {"status": "ok"}}, event_id="e1")

    assert aceite.status == 202
    assert aceite.body["clip_upload_url"]
    assert cliente.put_clip(aceite.body["clip_upload_url"], clipe).status == 200
    assert cliente.patch_event("e1", {"clip": {"status": "ok"}}).status == 200


def test_linha_da_fila_mostra_o_que_o_heartbeat_vai_mostrar():
    """São os campos de fila do §5.3. Ver `mais antigo` crescendo na tela é como se
    percebe um link caído antes de o TTL começar a descartar."""
    saude_agente = AgentHealth(
        outbox=OutboxStats(depth=3, events=2, clips=1, ready=1, oldest_age_s=42.0),
        sender=SenderStats(sent=7, failures=2),
        clip_disk_bytes=2048,
    )

    linha = cli._format_outbox(saude_agente.outbox, saude_agente)

    for pedaço in ("3 pendentes", "2 ev + 1 clipes", "42.0", "enviados=7", "falhas=2"):
        assert pedaço in linha


def test_dumper_grava_init_e_fragmentos(tmp_path):
    """`cat init.mp4 frag_*.mp4` precisa dar um MP4 válido; a ordem dos nomes é
    parte disso, e por isso o contador é zero-padded."""
    dumper = cli._FragmentDumper(tmp_path)
    dumper.on_init_segment(InitSegment(data=b"init", timescale=90_000))
    for i in range(11):
        dumper.on_fragment(
            Fragment(
                data=f"frag{i}".encode(),
                base_media_decode_time=i * 90_000,
                timescale=90_000,
                received_at=0.0,
            )
        )

    assert (tmp_path / "init.mp4").read_bytes() == b"init"
    nomes = sorted(p.name for p in tmp_path.glob("frag_*.mp4"))
    assert nomes[0] == "frag_00001.mp4"
    assert nomes[-1] == "frag_00011.mp4", "ordenação alfabética tem que bater com a temporal"


# --- estágio 2: detecção ----------------------------------------------------


def test_sem_model_a_deteccao_fica_desligada(caplog):
    """Um agente sem modelo é indistinguível de um com modelo até alguém reparar que
    nenhum alerta saiu daquela loja. O aviso é o que encurta essa distância."""
    args = cli.build_parser().parse_args(["--camera", "rtsp://x/y"])
    with caplog.at_level("WARNING"):
        opcoes = cli._monta_deteccao(args)

    assert opcoes.enabled is False
    assert "não detecta nada" in caplog.text


def test_no_detect_desliga_sem_reclamar(tmp_path, caplog):
    """Desligar de propósito não é a mesma coisa que esquecer o modelo, e o log tem que
    distinguir as duas."""
    modelo = tmp_path / "yolox_s.onnx"
    args = cli.build_parser().parse_args(
        ["--camera", "rtsp://x/y", "--model", str(modelo), "--no-detect"]
    )
    with caplog.at_level("WARNING"):
        opcoes = cli._monta_deteccao(args)

    assert opcoes.enabled is False
    assert "não detecta nada" not in caplog.text


def test_model_liga_o_estagio_com_os_padroes_da_arquitetura(tmp_path):
    modelo = tmp_path / "yolox_s.onnx"
    args = cli.build_parser().parse_args(["--camera", "rtsp://x/y", "--model", str(modelo)])
    opcoes = cli._monta_deteccao(args)

    assert opcoes.enabled is True
    assert opcoes.model_path == modelo
    assert opcoes.input_size == 640
    assert opcoes.providers[0] == "CUDAExecutionProvider", "o box da loja tem GPU (§3.1)"
    assert opcoes.providers[-1] == "CPUExecutionProvider", "e cai para CPU (§10.10)"


def test_provider_repetido_define_a_ordem_de_preferencia(tmp_path):
    """A ordem é a política de fallback inteira do §3.2 — não há `if gpu` em lugar
    nenhum, só esta lista."""
    modelo = tmp_path / "m.onnx"
    args = cli.build_parser().parse_args(
        [
            "--camera",
            "rtsp://x/y",
            "--model",
            str(modelo),
            "--provider",
            "TensorrtExecutionProvider",
            "--provider",
            "CPUExecutionProvider",
        ]
    )
    assert cli._monta_deteccao(args).providers == (
        "TensorrtExecutionProvider",
        "CPUExecutionProvider",
    )


def test_model_pode_vir_do_ambiente(tmp_path, monkeypatch):
    """O agente de produção não recebe flags: a configuração vem de fora (§3.9)."""
    monkeypatch.setenv("LINCE_MODEL_PATH", str(tmp_path / "do-ambiente.onnx"))
    import importlib

    importlib.reload(cli)
    try:
        args = cli.build_parser().parse_args(["--camera", "rtsp://x/y"])
        assert args.model == tmp_path / "do-ambiente.onnx"
    finally:
        monkeypatch.delenv("LINCE_MODEL_PATH")
        importlib.reload(cli)


def test_linha_de_deteccao_desligada_diz_por_que():
    assert "sem modelo" in cli._format_deteccao(DetectorStats())


def test_linha_de_deteccao_mostra_modelo_fila_e_camera():
    """`inference_fps` perto de 3 e descarte em zero é o estado saudável; a fila sai
    junto para o R-4 aparecer antes de o descarte começar."""
    stats = DetectorStats(
        enabled=True,
        info=DetectorInfo(
            model_version="yolox_s@abc123",
            input_size=640,
            provider="CUDAExecutionProvider",
            classes=(0,),
        ),
        queue_depth=1,
        queue_size=8,
        errors=0,
        cameras=(
            CameraDetectionStats(
                camera_id="saida",
                frames_in=90,
                frames_inferred=88,
                dropped=2,
                detections=41,
                inference_fps=2.97,
            ),
        ),
    )
    linha = cli._format_deteccao(stats)

    for pedaço in ("yolox_s@abc123", "CUDAExecutionProvider", "1/8", "saida", "2.97", "41", "2"):
        assert pedaço in linha


# --- estágio 3: tracking ----------------------------------------------------


def test_tracking_nasce_ligado_na_cli():
    args = cli.build_parser().parse_args(["--camera", "rtsp://x/y"])
    assert cli._monta_tracking(args).enabled is True


def test_no_track_desliga_o_estagio_3():
    args = cli.build_parser().parse_args(["--camera", "rtsp://x/y", "--no-track"])
    assert cli._monta_tracking(args).enabled is False


def test_high_threshold_e_configuravel():
    """O limiar que separa "é uma pessoa" de "talvez" é o que se ajusta em campo quando
    uma câmera tem contraluz na porta (R-3)."""
    args = cli.build_parser().parse_args(
        ["--camera", "rtsp://x/y", "--track-high-threshold", "0.7"]
    )
    assert cli._monta_tracking(args).high_threshold == pytest.approx(0.7)


def test_linha_de_tracking_desligado_diz_por_que():
    assert "desligado" in cli._format_tracking(TrackerStats())


def test_linha_de_tracking_destaca_a_fragmentacao():
    """`criados_por_minuto` é o número a vigiar: numa loja de bairro ele deveria se
    parecer com quantas pessoas entraram no quadro."""
    stats = TrackerStats(
        enabled=True,
        cameras=(
            CameraTrackingStats(
                camera_id="saida",
                ativos=2,
                provisorios=1,
                perdidos=1,
                criados=9,
                encerrados=6,
                criados_por_minuto=7.5,
                vida_media_s=12.3,
                associacoes_baixa=4,
            ),
        ),
    )
    linha = cli._format_tracking(stats)

    for pedaço in ("saida", "2 ativos", "7.5/min", "12.3s", "4 fracas", "criados=9"):
        assert pedaço in linha


def test_dump_tracks_grava_um_mp4_reproduzivel(tmp_path):
    """Com ffmpeg de verdade: o vídeo anotado é a verificação do estágio mais frágil do
    sistema, e um arquivo que não abre não verifica nada."""
    from lince_agent.config import DecodeOptions
    from lince_agent.ffmpeg.rawframe import Frame
    from lince_agent.track.state import Track, TrackingResult, TrackStatus

    decode = DecodeOptions(width=64, height=48, sample_fps=3.0)
    dumper = cli._TrackDumper(tmp_path, decode)

    for sequencia in range(1, 7):
        frame = Frame(
            data=bytes(decode.frame_bytes),
            width=decode.width,
            height=decode.height,
            pixel_format=decode.pixel_format,
            sequence=sequencia,
            received_at=sequencia / 3,
        )
        dumper.on_frame("cam1", frame)
        dumper.on_tracking(
            TrackingResult(
                camera_id="cam1",
                sequence=sequencia,
                received_at=sequencia / 3,
                tracks=(
                    Track(
                        track_id=1,
                        camera_id="cam1",
                        status=TrackStatus.CONFIRMADO,
                        x1=5.0 + sequencia,
                        y1=5.0,
                        x2=25.0 + sequencia,
                        y2=40.0,
                        score=0.9,
                        hits=sequencia,
                        age_s=sequencia / 3,
                        time_since_update_s=0.0,
                        first_seen_at=0.0,
                        last_seen_at=sequencia / 3,
                    ),
                ),
                tracking_ms=1.0,
            )
        )
    dumper.close()

    saida = tmp_path / "tracks_cam1.mp4"
    assert saida.is_file() and saida.stat().st_size > 0
    conferido = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-i", str(saida)],
        capture_output=True,
        check=False,
    )
    assert conferido.returncode == 0, "o MP4 anotado não abre"


def test_dump_tracks_ignora_resultado_sem_frame_correspondente(tmp_path):
    """O casamento é por `sequence`, não pelo último frame recebido: entre a chegada do
    frame e a saída do tracker passa uma inferência inteira, e a fila do §3.2 pode ter
    descartado outros no meio. Desenhar sobre o frame errado pareceria um bug de
    tracking."""
    from lince_agent.config import DecodeOptions
    from lince_agent.track.state import TrackingResult

    dumper = cli._TrackDumper(tmp_path, DecodeOptions(width=64, height=48))
    dumper.on_tracking(
        TrackingResult(camera_id="cam1", sequence=99, received_at=1.0, tracks=(), tracking_ms=0.1)
    )
    dumper.close()

    assert not list(tmp_path.glob("*.mp4")), "não devia ter aberto ffmpeg sem frame"


# --- estágio 4: zonas na linha de comando ---------------------------------------

BASE = ["--camera", "rtsp://x/y"]
LINHA = ["--linha-saida", "0,400,640,400"]
CAIXA = ["--zona-caixa", "100,420,340,420,340,470,100,470"]


def test_sem_zonas_o_motor_de_regras_fica_desligado():
    """O estado de quem só quer exercitar ingestão e clipe. O andaime de gatilho
    continua sendo o caminho, e é por isso que ele não foi removido junto."""
    args = cli.build_parser().parse_args(BASE)
    assert cli._monta_regras(args).enabled is False


def test_linha_e_zona_ligam_a_regra():
    args = cli.build_parser().parse_args([*BASE, *LINHA, *CAIXA, "--tempo-caixa", "6"])
    regras = cli._monta_regras(args)

    assert regras.enabled
    assert regras.linha_saida.origem == (0.0, 400.0)
    assert regras.zonas_caixa[0].vertices[0] == (100.0, 420.0)
    assert regras.tempo_caixa_min_s == 6.0


def test_mais_de_uma_posicao_de_caixa():
    """Mercado de bairro tem duas ou três posições de caixa. Um polígono só engolindo o
    corredor entre elas transformaria o corredor em zona de caixa — quem passa direto
    sairia descartado."""
    args = cli.build_parser().parse_args(
        [*BASE, *LINHA, *CAIXA, "--zona-caixa", "400,420,500,420,500,470,400,470"]
    )
    assert len(cli._monta_regras(args).zonas_caixa) == 2


@pytest.mark.parametrize(
    "texto",
    ["0,400,640", "0,400", "a,b,c,d"],
    ids=["ímpar", "um ponto só", "não numérico"],
)
def test_linha_malformada_e_recusada_no_parse(texto: str):
    """Erro de digitação vira mensagem de uso, não zona silenciosamente errada."""
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([*BASE, "--linha-saida", texto])


def test_zona_com_dois_vertices_e_recusada():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([*BASE, "--zona-caixa", "0,0,10,10"])


def test_zona_sem_linha_sai_com_erro_legivel(monkeypatch, caplog):
    """A configuração recusa a combinação e a CLI mostra **a mensagem**, não um
    traceback: quem está desenhando zonas pela primeira vez é exatamente quem menos
    consegue ler um traceback do dataclass."""
    monkeypatch.setattr(
        cli, "probe_stream", lambda *a, **k: StreamInfo("h264", 640, 480, 15.0, 15.0)
    )
    with caplog.at_level("ERROR"):
        assert cli.main([*BASE, *CAIXA]) == 2
    assert "linha_saida" in caplog.text


def test_zona_fora_do_quadro_sai_com_erro_legivel(monkeypatch, caplog):
    """Zonas desenhadas sobre um frame 1080p contra um agente que decodifica em 640x480.
    Em runtime não daria erro nenhum: o polígono não conteria ninguém, o tempo de caixa
    ficaria em zero e a câmera alertaria para todo cliente que sai (R-1)."""
    monkeypatch.setattr(
        cli, "probe_stream", lambda *a, **k: StreamInfo("h264", 640, 480, 15.0, 15.0)
    )
    with caplog.at_level("ERROR"):
        codigo = cli.main([*BASE, "--linha-saida", "0,900,1900,900", *CAIXA])
    assert codigo == 2
    assert "fora do quadro" in caplog.text


def test_linha_de_regras_desligada_diz_por_que():
    assert "sem zonas desenhadas" in cli._format_regras(RuleStats())


def test_linha_de_regras_destaca_os_descartes():
    """Os descartes é que dizem se a loja está calibrada. `pagou` deveria dominar; se
    `vida curta` estiver alto, o problema é o tracker (R-2) e mexer no N não adianta."""
    linha = cli._format_regras(
        RuleStats(
            enabled=True,
            cameras=(
                CameraRuleStats(
                    camera_id="cam1",
                    configurada=True,
                    acompanhados=4,
                    no_caixa=1,
                    travessias_saida=30,
                    travessias_entrada=28,
                    eventos=2,
                    descartados_pagou=25,
                    descartados_vida_curta=3,
                    tempo_caixa_medio_s=11.5,
                ),
                CameraRuleStats(camera_id="cam2"),
            ),
        )
    )
    assert "eventos=2" in linha
    assert "25 pagou" in linha and "3 vida curta" in linha
    assert "11.5s" in linha
    assert "cam2=sem zonas" in linha
