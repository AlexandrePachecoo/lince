"""CLI de desenvolvimento do agente.

Não é o ponto de entrada de produção — o agente de verdade vai puxar a lista de
câmeras e as zonas da nuvem (§5.2). Isto existe para apontar o pipeline para uma
câmera e ver o que sai, que é como as pendências do §10 vão ser medidas.

    uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 --stats
    uv run python -m lince_agent --camera rtsp://localhost:8554/cam1 \\
        --outbox redis --dry-run --trigger-every 20 --stats

**O gatilho é andaime.** O estágio 4 (motor de regras) não existe, então nada dispara
eventos sozinho. `--trigger-after`, `--trigger-every` e `SIGUSR1` ocupam esse lugar
para que o caminho gatilho → clipe → fila → nuvem possa ser exercitado inteiro hoje.
Os eventos que eles produzem vão marcados como `source: "manual"`, e isso continua
valendo em produção: gatilho manual existe para testar câmera na instalação, e a
nuvem precisa distingui-lo para não contaminar a métrica de falso positivo (R-1).
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

from lince_agent.config import (
    AgentConfig,
    CameraConfig,
    CloudOptions,
    DecodeOptions,
    DetectionOptions,
    HwAccel,
    OutboxOptions,
    PixelFormat,
    TrackingOptions,
)
from lince_agent.detect.state import DetectorStats
from lince_agent.ffmpeg.capabilities import select_hwaccel
from lince_agent.ffmpeg.probe import ProbeError, probe_stream
from lince_agent.ffmpeg.process import IngestCallbacks
from lince_agent.ingest.state import CameraHealth
from lince_agent.ingest.supervisor import CameraSupervisor
from lince_agent.outbox.http import CloudResponse, HttpCloudClient
from lince_agent.outbox.state import OutboxStats
from lince_agent.outbox.store import MemoryOutbox, OutboxStore
from lince_agent.runtime import AgentHealth, AgentRuntime
from lince_agent.track.draw import Rastros, desenha
from lince_agent.track.state import TrackerStats, TrackingResult

log = logging.getLogger("lince_agent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lince-agent",
        description="Agente da borda: ingestão RTSP, clipe e envio para a nuvem",
    )
    parser.add_argument("--camera", required=True, help="URL RTSP da câmera")
    parser.add_argument("--camera-id", default="cam", help="identificador nos logs")
    parser.add_argument("--fps", type=float, default=3.0, help="taxa entregue à detecção")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument(
        "--pix-fmt",
        default=PixelFormat.BGR24.value,
        choices=[fmt.value for fmt in PixelFormat],
    )
    parser.add_argument(
        "--hwaccel",
        default=HwAccel.NONE.value,
        choices=[accel.value for accel in HwAccel],
        help="verificado em tempo de execução; cai para CPU se não funcionar",
    )
    parser.add_argument("--duration", type=float, help="segundos até parar sozinho")
    parser.add_argument("--stats", action="store_true", help="imprime saúde a cada segundo")
    parser.add_argument(
        "--dump-fragments",
        type=Path,
        metavar="DIR",
        help="grava init.mp4 e frag_NNNNN.mp4; concatenados formam um MP4 reproduzível",
    )
    parser.add_argument("--probe-only", action="store_true", help="só roda o ffprobe e sai")
    parser.add_argument("-v", "--verbose", action="store_true")

    estagio6 = parser.add_argument_group("estágio 6 — fila local e envio (§3.6)")
    estagio6.add_argument("--tenant-id", default="dev", help="NFR-6: toda entidade tem tenant")
    estagio6.add_argument("--store-id", default="loja-dev")
    estagio6.add_argument(
        "--clips-dir",
        type=Path,
        default=Path("./clipes"),
        help="onde os clipes esperam o upload; são apagados depois dele (NFR-3)",
    )
    estagio6.add_argument(
        "--outbox",
        default="memory",
        choices=("memory", "redis"),
        help="memory é andaime de desenvolvimento e não sobrevive a restart",
    )
    estagio6.add_argument(
        "--redis-url",
        default=os.environ.get("LINCE_REDIS_URL", OutboxOptions().redis_url),
        help="fila local durável (ADR-004)",
    )
    estagio6.add_argument(
        "--api-url",
        default=os.environ.get("LINCE_API_URL", CloudOptions().api_url),
        help="control plane (§5.2)",
    )
    estagio6.add_argument("--api-token", default=os.environ.get("LINCE_AGENT_TOKEN"))
    estagio6.add_argument(
        "--dry-run",
        action="store_true",
        help="aceita tudo sem falar com a nuvem; só registra o que teria enviado",
    )

    estagio2 = parser.add_argument_group("estágio 2 — detecção (§3.2)")
    estagio2.add_argument(
        "--model",
        type=Path,
        default=_env_path("LINCE_MODEL_PATH"),
        metavar="ONNX",
        help="modelo .onnx; sem ele o agente ingere e grava clipe, mas não detecta nada",
    )
    estagio2.add_argument(
        "--input-size",
        type=int,
        default=640,
        help="lado da entrada do modelo; conferido contra o que o .onnx declara",
    )
    estagio2.add_argument(
        "--provider",
        action="append",
        dest="providers",
        metavar="NOME",
        help="provider do ONNX Runtime, em ordem de preferência; repetível. "
        "O default tenta CUDA e cai para CPU (§10.10)",
    )
    padrao = DetectionOptions()
    estagio2.add_argument("--score-threshold", type=float, default=padrao.score_threshold)
    estagio2.add_argument("--iou-threshold", type=float, default=padrao.iou_threshold)
    estagio2.add_argument(
        "--no-detect",
        action="store_true",
        help="ignora o modelo e roda com NullDetector, como uma câmera inelegível (R-3)",
    )

    estagio3 = parser.add_argument_group("estágio 3 — tracking (§3.3)")
    estagio3.add_argument(
        "--no-track",
        action="store_true",
        help="detecta sem dar identidade; as caixas param no estágio 2",
    )
    estagio3.add_argument(
        "--track-high-threshold",
        type=float,
        default=TrackingOptions().high_threshold,
        help="acima disto uma caixa pode criar track; abaixo, só continua um existente",
    )
    estagio3.add_argument(
        "--dump-tracks",
        type=Path,
        metavar="DIR",
        help="grava um MP4 por câmera com as caixas, os pés e o rastro de cada track. "
        "Cor por ID: troca de cor no meio do percurso é troca de ID (R-2)",
    )

    andaime = parser.add_argument_group("andaime do gatilho (o estágio 4 não existe)")
    andaime.add_argument("--trigger-after", type=float, metavar="S", help="um gatilho após S s")
    andaime.add_argument(
        "--trigger-every",
        type=float,
        metavar="S",
        help="gatilho a cada S s; é o que exercita rajada, teto de disco e fila represada",
    )
    return parser


class ClienteSeco:
    """Nuvem de mentira para `--dry-run`: aceita tudo e só registra.

    Serve ao caso mais comum de desenvolvimento — ver o pipeline inteiro funcionando
    sem uma API do outro lado. Não é dublê de teste: os testes usam a nuvem de
    verdade, num servidor HTTP local.
    """

    def post_event(self, payload: dict, *, event_id: str) -> CloudResponse:
        log.info(
            "[dry-run] POST /v1/events %s câmera=%s clipe=%s",
            event_id,
            payload.get("camera_id"),
            payload.get("clip", {}).get("status"),
        )
        return CloudResponse(
            status=202,
            body={"schema_version": 1, "clip_upload_url": "dry-run://upload"},
        )

    def put_clip(self, url: str, path: Path, *, content_type: str = "video/mp4") -> CloudResponse:
        log.info("[dry-run] PUT clipe %s (%d bytes)", path.name, path.stat().st_size)
        return CloudResponse(status=200)

    def patch_event(self, event_id: str, payload: dict) -> CloudResponse:
        clipe = payload.get("clip", {})
        log.info("[dry-run] PATCH /v1/events/%s clipe=%s", event_id, clipe.get("status"))
        return CloudResponse(status=200)


def _env_path(nome: str) -> Path | None:
    valor = os.environ.get(nome)
    return Path(valor) if valor else None


def _monta_deteccao(args: argparse.Namespace) -> DetectionOptions:
    """Traduz as flags em `DetectionOptions`, avisando quando o estágio fica desligado.

    O aviso importa porque um agente sem modelo é indistinguível de um agente com
    modelo até alguém reparar que nenhum alerta saiu naquela loja.
    """
    if args.no_detect or args.model is None:
        if not args.no_detect:
            log.warning(
                "sem --model: o agente ingere, mantém o buffer e responde a gatilho manual, "
                "mas não detecta nada (§3.2). Rode `bash scripts/modelo.sh` para baixar um."
            )
        return DetectionOptions(enabled=False)

    padrao = DetectionOptions()
    return DetectionOptions(
        enabled=True,
        model_path=args.model,
        input_size=args.input_size,
        providers=tuple(args.providers) if args.providers else padrao.providers,
        score_threshold=args.score_threshold,
        iou_threshold=args.iou_threshold,
    )


def _monta_tracking(args: argparse.Namespace) -> TrackingOptions:
    return TrackingOptions(
        enabled=not args.no_track,
        high_threshold=args.track_high_threshold,
    )


def _monta_fila(args: argparse.Namespace, options: OutboxOptions) -> OutboxStore:
    if args.outbox == "memory":
        log.warning(
            "fila em RAM: andaime de desenvolvimento. Um restart perde todos os eventos "
            "que ainda não subiram — em loja, use --outbox redis (ADR-004)."
        )
        return MemoryOutbox(options)

    from lince_agent.outbox.redis_store import RedisOutbox

    return RedisOutbox(args.store_id, options)


def _format_health(health: CameraHealth, frame_bytes: int) -> str:
    last = (
        "nunca"
        if health.last_frame_at is None
        else f"{time.monotonic() - health.last_frame_at:.1f}s atrás"
    )
    return (
        f"[{health.camera_id}] {health.status:<12} "
        f"frames={health.frames:<6} {health.sampled_fps:5.2f} fps "
        f"({frame_bytes} B/frame, {health.frames_dropped} descartados)  "
        f"fragmentos={health.fragments:<4} {health.fragment_bytes / 1024:8.1f} KiB "
        f"({health.fragments_dropped} descartados)  último frame {last}"
    )


def _format_outbox(fila: OutboxStats, saude: AgentHealth) -> str:
    """A linha que o heartbeat do §5.3 vai carregar: fila, idade e desfechos."""
    envio = saude.sender
    return (
        f"[fila] {fila.depth} pendentes ({fila.events} ev + {fila.clips} clipes, "
        f"{fila.ready} prontos)  mais antigo {fila.oldest_age_s:6.1f}s  "
        f"{fila.payload_bytes / 1024:6.1f} KiB + {saude.clip_disk_bytes / 1024:8.1f} KiB em disco  "
        f"enviados={envio.sent} clipes={envio.clips_uploaded} "
        f"falhas={envio.failures} mortos={fila.dead} vencidos={envio.expired}"
    )


def _format_deteccao(stats: DetectorStats) -> str:
    """A linha do estágio 2: o que o §5.3 pede por câmera, mais o modelo que produziu.

    `inference_fps` perto de 3 e `descartados` em zero é o estado saudável. Descarte
    subindo com a fila cheia é GPU que não acompanha a taxa amostrada — o sinal de
    saturação do R-4, e é para vê-lo cedo que a profundidade da fila sai junto.
    """
    if not stats.enabled:
        return "[detecção] desligada (sem modelo)"

    info = stats.info
    modelo = "?" if info is None else f"{info.model_version} em {info.provider}"
    por_camera = "  ".join(
        f"{camera.camera_id}={camera.inference_fps:4.2f} fps "
        f"({camera.detections} caixas, {camera.dropped} descartados)"
        for camera in stats.cameras
    )
    return (
        f"[detecção] {modelo}  fila {stats.queue_depth}/{stats.queue_size}  "
        f"inferidos={stats.frames_inferred} erros={stats.errors}  {por_camera}"
    )


def _format_tracking(stats: TrackerStats) -> str:
    """A linha do estágio 3. `criados_por_minuto` é o número a vigiar.

    Numa loja de bairro ele deveria se parecer com quantas pessoas entraram no quadro.
    Muito acima disso é o tracker quebrando uma pessoa em pedaços — a fragmentação do
    §3.3 —, e track que nasce já fora do caixa é o falso positivo do R-2.
    """
    if not stats.enabled:
        return "[tracking] desligado"

    por_camera = "  ".join(
        f"{camera.camera_id}={camera.ativos} ativos "
        f"(+{camera.provisorios} novos, {camera.perdidos} perdidos, "
        f"{camera.criados_por_minuto:4.1f}/min, vida {camera.vida_media_s:4.1f}s, "
        f"{camera.associacoes_baixa} fracas)"
        for camera in stats.cameras
    )
    return f"[tracking] criados={stats.criados} encerrados={stats.encerrados}  {por_camera}"


class _TrackDumper:
    """Grava um MP4 por câmera com os tracks desenhados.

    É a única forma de verificar o estágio 3: troca de ID não aparece em contador
    nenhum, porque o número de tracks sobe igual quando o tracker acerta e quando erra.
    No vídeo, cada pessoa tem uma cor — se a cor muda no meio do corredor, houve troca.

    Os frames são casados com o resultado do tracking por `sequence`, e não pelo último
    frame recebido: entre a chegada do frame e a saída do tracker passa uma inferência
    inteira, e a fila do §3.2 pode ter descartado outros no meio. Desenhar tracks sobre
    o frame errado produziria exatamente a aparência de um bug de tracking.
    """

    def __init__(self, directory: Path, decode: DecodeOptions, *, ffmpeg_bin: str = "ffmpeg"):
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._decode = decode
        self._ffmpeg_bin = ffmpeg_bin
        self._processos: dict[str, subprocess.Popen] = {}
        self._pendentes: dict[str, dict[int, bytes]] = {}
        self._rastros: dict[str, Rastros] = {}
        self._escritos: dict[str, int] = {}
        self._lock = threading.Lock()

    def on_frame(self, camera_id: str, frame) -> None:  # noqa: ANN001 - Frame
        with self._lock:
            pendentes = self._pendentes.setdefault(camera_id, {})
            pendentes[frame.sequence] = frame.data
            # Teto pequeno: só precisa cobrir a latência de uma inferência. Sem ele, um
            # tracking desligado encheria a RAM de frames de 921 KB.
            for antigo in sorted(pendentes)[:-16]:
                del pendentes[antigo]

    def on_tracking(self, resultado: TrackingResult) -> None:
        with self._lock:
            dados = self._pendentes.get(resultado.camera_id, {}).pop(resultado.sequence, None)
        if dados is None:
            return

        imagem = np.frombuffer(dados, dtype=np.uint8).reshape(
            self._decode.height, self._decode.width, 3
        )
        rastros = self._rastros.setdefault(resultado.camera_id, Rastros())
        tela = desenha(imagem, resultado.tracks, rastros.registra(resultado.tracks))

        processo = self._processo(resultado.camera_id)
        if processo.stdin is None:
            return
        try:
            processo.stdin.write(tela.tobytes())
        except BrokenPipeError:
            log.warning("ffmpeg da gravação de %s morreu", resultado.camera_id)
            return
        self._escritos[resultado.camera_id] = self._escritos.get(resultado.camera_id, 0) + 1

    def _processo(self, camera_id: str) -> subprocess.Popen:
        if camera_id in self._processos:
            return self._processos[camera_id]

        destino = self._directory / f"tracks_{camera_id}.mp4"
        argv = [
            *(self._ffmpeg_bin, "-hide_banner", "-loglevel", "error", "-y"),
            *("-f", "rawvideo", "-pix_fmt", "bgr24"),
            *("-s", f"{self._decode.width}x{self._decode.height}"),
            *("-r", str(self._decode.sample_fps)),
            *("-i", "pipe:0"),
            *("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"),
            str(destino),
        ]
        log.info("gravando tracks de %s em %s", camera_id, destino)
        self._processos[camera_id] = subprocess.Popen(argv, stdin=subprocess.PIPE)  # noqa: S603
        return self._processos[camera_id]

    def close(self) -> None:
        for camera_id, processo in self._processos.items():
            if processo.stdin is not None:
                processo.stdin.close()
            processo.wait(timeout=30)
            log.info(
                "tracks de %s: %d frames em %s",
                camera_id,
                self._escritos.get(camera_id, 0),
                self._directory / f"tracks_{camera_id}.mp4",
            )


class _FragmentDumper:
    """Grava init segment e fragmentos para a verificação de ponta a ponta.

    `cat init.mp4 frag_*.mp4 > clipe.mp4` tem que produzir um MP4 reproduzível. É a
    demonstração de que a unidade do buffer circular está correta e de que o corte
    `-c copy` do §3.5 vai ter do que se alimentar.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._directory.mkdir(parents=True, exist_ok=True)
        self._count = 0

    def on_init_segment(self, item) -> None:  # noqa: ANN001 - InitSegment
        (self._directory / "init.mp4").write_bytes(item.data)
        log.info("init segment gravado: %d bytes, timescale %d", len(item.data), item.timescale)

    def on_fragment(self, fragment) -> None:  # noqa: ANN001 - Fragment
        self._count += 1
        name = self._directory / f"frag_{self._count:05d}.mp4"
        name.write_bytes(fragment.data)
        log.debug(
            "fragmento %d: %d bytes, início em %.3fs",
            self._count,
            len(fragment),
            fragment.start_seconds,
        )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    decode = DecodeOptions(
        sample_fps=args.fps,
        width=args.width,
        height=args.height,
        pixel_format=PixelFormat(args.pix_fmt),
        hwaccel=select_hwaccel(HwAccel(args.hwaccel)),
    )

    try:
        info = probe_stream(args.camera, decode)
    except ProbeError as error:
        log.error("%s", error)
        return 1
    log.info(
        "câmera entrega %s %dx%d, %.2f fps (nominal %.2f)",
        info.codec,
        info.width,
        info.height,
        info.avg_frame_rate,
        info.r_frame_rate,
    )
    if args.probe_only:
        return 0

    opcoes_fila = OutboxOptions(redis_url=args.redis_url)
    config = AgentConfig(
        tenant_id=args.tenant_id,
        store_id=args.store_id,
        cameras=(CameraConfig(camera_id=args.camera_id, url=args.camera, decode=decode),),
        clips_dir=args.clips_dir,
        outbox=opcoes_fila,
        cloud=CloudOptions(api_url=args.api_url, token=args.api_token),
        detection=_monta_deteccao(args),
        tracking=_monta_tracking(args),
    )
    fila = _monta_fila(args, opcoes_fila)
    cliente = (
        ClienteSeco()
        if args.dry_run
        else HttpCloudClient(
            config.cloud.api_url,
            token=config.cloud.token,
            timeout_s=config.cloud.timeout_s,
            upload_timeout_s=config.cloud.upload_timeout_s,
        )
    )

    dumper = _FragmentDumper(args.dump_fragments) if args.dump_fragments else None
    tracks_dumper = _TrackDumper(args.dump_tracks, decode) if args.dump_tracks else None
    runtime = AgentRuntime(
        config,
        store=fila,
        client=cliente,
        supervisor_factory=_com_dumper(config, dumper, tracks_dumper)
        if dumper or tracks_dumper
        else None,
        on_tracking=tracks_dumper.on_tracking if tracks_dumper else None,
    )

    finished = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: finished.set())
    signal.signal(signal.SIGTERM, lambda *_: finished.set())

    pedido = threading.Event()
    if hasattr(signal, "SIGUSR1"):
        # O handler só sinaliza. Chamar `trigger()` aqui seria trabalhar dentro de um
        # handler de sinal, que pode interromper qualquer thread em qualquer ponto —
        # inclusive segurando o lock que o próprio `trigger` vai querer.
        signal.signal(signal.SIGUSR1, lambda *_: pedido.set())
        log.info("gatilho manual: kill -USR1 %d", os.getpid())

    runtime.start()
    deadline = time.monotonic() + args.duration if args.duration else None
    proximo_gatilho = _primeiro_gatilho(args)
    try:
        while not finished.is_set():
            agora = time.monotonic()
            if deadline is not None and agora >= deadline:
                break
            if pedido.is_set():
                pedido.clear()
                _dispara(runtime, "SIGUSR1")
            if proximo_gatilho is not None and agora >= proximo_gatilho:
                _dispara(runtime, "andaime")
                proximo_gatilho = agora + args.trigger_every if args.trigger_every else None
            if args.stats:
                saude = runtime.health()
                for camera in saude.cameras:
                    print(_format_health(camera, decode.frame_bytes), flush=True)  # noqa: T201
                print(_format_deteccao(saude.detector), flush=True)  # noqa: T201
                print(_format_tracking(saude.tracker), flush=True)  # noqa: T201
                print(_format_outbox(saude.outbox, saude), flush=True)  # noqa: T201
            finished.wait(1.0)
    finally:
        runtime.stop()
        fila.close()
        if tracks_dumper is not None:
            tracks_dumper.close()

    saude = runtime.health()
    frames = sum(camera.frames for camera in saude.cameras)
    log.info(
        "encerrado: %d frames, %d inferidos, %d caixas, %d eventos enviados, %d ainda na fila",
        frames,
        saude.detector.frames_inferred,
        saude.detector.detections,
        saude.sender.sent,
        saude.outbox.depth,
    )
    return 0 if frames > 0 else 1


def _primeiro_gatilho(args: argparse.Namespace) -> float | None:
    atraso = args.trigger_after if args.trigger_after is not None else args.trigger_every
    return None if atraso is None else time.monotonic() + atraso


def _dispara(runtime: AgentRuntime, origem: str) -> None:
    for camera_id in runtime.camera_ids:
        event_id = runtime.trigger(camera_id)
        log.info("gatilho de andaime (%s) na câmera %s: evento %s", origem, camera_id, event_id)


def _com_dumper(
    config: AgentConfig,
    dumper: _FragmentDumper | None,
    tracks: _TrackDumper | None = None,
):
    """Encaixa `--dump-fragments` e `--dump-tracks` sem o runtime saber que existem.

    O buffer circular e o detector continuam recebendo tudo; os dumpers só escutam
    junto. É a mesma razão de sempre: uma ferramenta de diagnóstico não pode mudar o
    caminho que ela existe para observar.
    """

    def fabrica(camera: CameraConfig, callbacks: IngestCallbacks) -> CameraSupervisor:
        def on_init(item) -> None:  # noqa: ANN001 - InitSegment
            if dumper is not None:
                dumper.on_init_segment(item)
            if callbacks.on_init_segment:
                callbacks.on_init_segment(item)

        def on_fragment(item) -> None:  # noqa: ANN001 - Fragment
            if dumper is not None:
                dumper.on_fragment(item)
            if callbacks.on_fragment:
                callbacks.on_fragment(item)

        def on_frame(item) -> None:  # noqa: ANN001 - Frame
            if tracks is not None:
                tracks.on_frame(camera.camera_id, item)
            if callbacks.on_frame:
                callbacks.on_frame(item)

        espiados = IngestCallbacks(
            on_frame=on_frame,
            on_init_segment=on_init,
            on_fragment=on_fragment,
        )
        return CameraSupervisor(camera, espiados, ffmpeg_bin=config.ffmpeg_bin)

    return fabrica


if __name__ == "__main__":
    sys.exit(main())
