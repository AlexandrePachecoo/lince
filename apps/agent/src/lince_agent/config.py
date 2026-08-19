"""Configuração da ingestão.

Tudo aqui é dado, não comportamento: o §3.9 da arquitetura exige que o mesmo
artefato rode na nuvem e na loja sem `if cloud`, e o que muda entre os dois é
configuração externa. Na prática isso significa que nenhum valor deste módulo pode
aparecer literal dentro da camada ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from lince_agent.rules.geometry import LinhaOrientada, Poligono


class PixelFormat(StrEnum):
    """Formato dos frames que saem no pipe de detecção.

    BGR24 custa 3 bytes por pixel e é o layout que OpenCV e Ultralytics consomem
    direto. NV12 e YUV420P custam 1,5 — metade da banda do pipe — mas cobram uma
    conversão por frame do lado Python. Comece em BGR24; os outros dois são a
    alavanca para quando o pipe saturar.
    """

    BGR24 = "bgr24"
    NV12 = "nv12"
    YUV420P = "yuv420p"

    def frame_bytes(self, width: int, height: int) -> int:
        """Tamanho exato de um frame decodificado, em bytes.

        Este número é o contrato do pipe de detecção: `rawvideo` não tem framing
        nenhum, então o leitor fatia o stream em blocos deste tamanho. Errar aqui
        não produz erro — produz todos os frames embaralhados.
        """
        if self is PixelFormat.BGR24:
            return width * height * 3
        # NV12 e YUV420P são planares 4:2:0: um plano Y de w×h mais dois planos de
        # croma subamostrados por 2 em cada eixo.
        if width % 2 or height % 2:
            raise ValueError(f"{self} exige largura e altura pares, recebi {width}x{height}")
        return width * height * 3 // 2


class HwAccel(StrEnum):
    """Estratégia de decode por hardware.

    A escolha é em tempo de execução (`capabilities.detect_hwaccel`), nunca em
    tempo de build: o box da loja tem GPU NVIDIA e a máquina de desenvolvimento
    não, e o §3.1 exige fallback para CPU.
    """

    NONE = "none"
    """Decode em CPU. Único modo disponível sem GPU."""

    CUDA = "cuda"
    """`-hwaccel cuda`: decodifica na GPU e devolve os frames para a RAM do
    sistema, que é o que o pipe rawvideo exige. Agnóstico de codec, então funciona
    igual numa frota com câmeras H.264 e H.265 misturadas."""

    CUDA_GPU_FILTER = "cuda-gpu-filter"
    """Mantém os frames na VRAM e faz amostragem e escala na própria GPU, descendo
    pelo PCIe só os 3 fps que sobreviveram em vez dos 15. É a configuração ótima do
    box de referência. Custo: obriga NV12, porque `hwdownload` não produz BGR24 a
    partir de frames CUDA."""


@dataclass(frozen=True, slots=True)
class DecodeOptions:
    """Opções que viram argumentos do ffmpeg. Ver docs do `ffmpeg/command.py`."""

    sample_fps: float = 3.0
    """Taxa entregue ao estágio de detecção (§3.2). O decode continua em taxa
    cheia — a amostragem economiza inferência e banda de pipe, não decode."""

    width: int = 640
    height: int = 480
    pixel_format: PixelFormat = PixelFormat.BGR24
    hwaccel: HwAccel = HwAccel.NONE

    rtsp_transport: str = "tcp"
    """RTP dentro do TCP. Em UDP, uma LAN carregada perde pacotes e produz a
    pixelação que o §3.1 lista como falha esperada; TCP retransmite."""

    socket_timeout_s: float = 5.0
    """Timeout de I/O do socket RTSP. Sem ele, uma câmera que para de responder
    deixa o ffmpeg pendurado para sempre. Vira microssegundos no argumento."""

    probesize_bytes: int = 1_000_000
    analyze_duration_s: float = 2.0
    """Quanto o ffmpeg consome antes de decidir os parâmetros do stream. O default
    do ffmpeg é 5 s de `analyzeduration`, ou seja, 5 s de atraso na subida de cada
    câmera. Baixar demais faz o ffmpeg errar o formato e abortar."""

    low_latency: bool = True
    """`-fflags nobuffer -flags low_delay`."""

    wallclock_timestamps: bool = True
    """`-use_wallclock_as_timestamps 1`. Implementa o §3.1: o timestamp de
    referência é o da borda, nunca o da câmera. Vale para as duas saídas, então o
    t0 do gatilho e o timeline do clipe compartilham o mesmo relógio.

    Aplicado **só a entradas RTSP**: a opção carimba cada pacote na chegada, e num
    arquivo — lido na velocidade máxima — todos chegariam no mesmo instante."""

    log_level: str = "warning"
    """`error` esconderia os avisos de corrupção de pacote, que são justamente o
    sinal de câmera degradada. `info` polui."""

    def __post_init__(self) -> None:
        if self.sample_fps <= 0:
            raise ValueError(f"sample_fps deve ser positivo, recebi {self.sample_fps}")
        if self.width <= 0 or self.height <= 0:
            raise ValueError(f"resolução inválida: {self.width}x{self.height}")
        if self.hwaccel is HwAccel.CUDA_GPU_FILTER and self.pixel_format is not PixelFormat.NV12:
            raise ValueError(
                "HwAccel.CUDA_GPU_FILTER exige PixelFormat.NV12: o filtro hwdownload "
                "não converte frames CUDA para BGR24"
            )

    @property
    def frame_bytes(self) -> int:
        return self.pixel_format.frame_bytes(self.width, self.height)


@dataclass(frozen=True, slots=True)
class SupervisionOptions:
    """Política de reconexão e watchdog (§3.1).

    Nada disto pode ser delegado ao ffmpeg: `-reconnect` e companhia são opções do
    protocolo HTTP e são silenciosamente ignoradas numa entrada RTSP.
    """

    backoff_base_s: float = 1.0
    backoff_cap_s: float = 60.0
    backoff_jitter_s: float = 1.0
    """Jitter aditivo. Sem ele, oito câmeras que caem juntas (queda do switch)
    voltam juntas e batem no mesmo instante, repetidamente."""

    offline_after_failures: int = 5
    """Falhas consecutivas até o estado virar `offline` no heartbeat. O §5.3 exige
    distinguir `reconnecting` de `offline`: a ação do operador é diferente."""

    stall_timeout_s: float = 10.0
    """Sem frame novo por este tempo, o stream travou sem fechar a conexão. O
    `socket_timeout_s` não pega este caso — o socket continua vivo."""

    stop_grace_s: float = 3.0
    """Espera entre SIGTERM e SIGKILL."""


@dataclass(frozen=True, slots=True)
class ClipOptions:
    """Buffer circular e corte do clipe (§3.5).

    Por câmera, não por loja: o §3.4 exige limiares por câmera, e aqui isso é
    concreto — uma câmera de saída com GOP de 10 s precisa de outra tolerância de
    pós-roll que a do corredor.
    """

    window_s: float = 30.0
    """Quanto de vídeo o buffer retém. Precisa cobrir o corte inteiro com folga:
    durante a espera do pós-roll a poda continua rodando, e uma janela apertada
    descartaria o pré-roll antes de o corte acontecer."""

    pre_roll_s: float = 5.0
    post_roll_s: float = 10.0
    """Os 10 s são incompressíveis por definição do produto e consomem dois terços
    do orçamento do §3.7. É por isso que o alerta sobe antes do clipe."""

    post_roll_grace_s: float = 5.0
    """Tolerância além do pós-roll antes de desistir e cortar o que houver. Sem
    ela, uma câmera de GOP longo que parou de entregar seguraria o clipe para
    sempre — e o alerta não pode esperar (§3.7)."""

    max_bytes: int = 16 * 1024 * 1024
    """Teto rígido de RAM por câmera (§3.5). Vence a janela de tempo: RAM é limite
    físico, 30 s é desejo de produto. Quando morde, o pré-roll sai curto e isso é
    registrado no evento em vez de silenciado."""

    max_disk_bytes: int = 2 * 1024**3
    """Teto do diretório de clipes pendentes. Ao estourar, o clipe mais antigo é
    apagado e o evento sobrevive sem ele (§3.6)."""

    remux_timeout_s: float = 30.0
    pending_requests: int = 8
    """Gatilhos aguardando corte. Cheia, descarta e conta: uma rajada não pode
    crescer memória nem bloquear o motor de regras."""

    def __post_init__(self) -> None:
        if self.pre_roll_s <= 0:
            raise ValueError(f"pre_roll_s deve ser positivo, recebi {self.pre_roll_s}")
        if self.post_roll_s <= 0:
            raise ValueError(f"post_roll_s deve ser positivo, recebi {self.post_roll_s}")
        if self.post_roll_grace_s < 0:
            raise ValueError(f"post_roll_grace_s não pode ser negativo: {self.post_roll_grace_s}")
        necessario = self.pre_roll_s + self.post_roll_s + self.post_roll_grace_s
        if self.window_s < necessario:
            raise ValueError(
                f"window_s de {self.window_s}s não cobre pré-roll + pós-roll + tolerância "
                f"({necessario}s): o buffer descartaria o pré-roll antes do corte"
            )
        if self.max_bytes <= 0:
            raise ValueError(f"max_bytes deve ser positivo, recebi {self.max_bytes}")
        if self.max_disk_bytes <= 0:
            raise ValueError(f"max_disk_bytes deve ser positivo, recebi {self.max_disk_bytes}")
        if self.pending_requests <= 0:
            raise ValueError(f"pending_requests deve ser positivo, recebi {self.pending_requests}")

    @property
    def clip_duration_s(self) -> float:
        """Duração nominal. A efetiva vai no evento e costuma diferir: o corte se
        alinha ao keyframe e o buffer pode não ter o pré-roll inteiro."""
        return self.pre_roll_s + self.post_roll_s


@dataclass(frozen=True, slots=True)
class RetryOptions:
    """Política de reenvio para a nuvem (§5.4).

    Os números são maiores que os da reconexão de câmera de propósito. Uma câmera que
    caiu precisa voltar rápido, porque enquanto ela está fora não há detecção. Um
    evento na fila não perde nada esperando: ele já aconteceu, o clipe já está em
    disco, e martelar uma API que devolve `5xx` só atrasa a recuperação dela.
    """

    base_s: float = 2.0
    cap_s: float = 300.0
    jitter_s: float = 2.0
    """Sem jitter, a fila represada de uma noite offline dispara inteira no mesmo
    instante em que o link volta — e derruba de novo o que acabou de voltar."""

    auth_floor_s: float = 60.0
    """Piso de espera para `401`/`403`. Credencial expirada só se resolve com
    intervenção humana ou rotação pela nuvem; tentar a cada 2 s não adianta e enche o
    log justamente quando alguém está depurando o problema."""

    max_clip_attempts: int = 5
    """Tentativas de upload do clipe antes de desistir e marcar `clip_failed`. O
    evento **não** tem teto de tentativas: ele só sai da fila por sucesso ou por TTL,
    porque perder o alerta é pior que insistir (§3.6)."""

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError(f"base_s deve ser positivo, recebi {self.base_s}")
        if self.cap_s < self.base_s:
            raise ValueError(f"cap_s ({self.cap_s}) não pode ser menor que base_s ({self.base_s})")
        if self.jitter_s < 0:
            raise ValueError(f"jitter_s não pode ser negativo: {self.jitter_s}")
        if self.max_clip_attempts <= 0:
            raise ValueError(
                f"max_clip_attempts deve ser positivo, recebi {self.max_clip_attempts}"
            )


@dataclass(frozen=True, slots=True)
class OutboxOptions:
    """Fila local e envio (§3.6, ADR-004).

    A fila é Redis, e a consequência está escrita: com `appendonly yes` e o
    `appendfsync everysec` padrão, uma queda de energia leva junto até ~1 s de fila.
    Para a loja isso é, no pior caso, o último evento antes do apagão — que é
    justamente quando ninguém está olhando o celular.
    """

    redis_url: str = "redis://localhost:6379/0"
    key_prefix: str = "lince"
    """Prefixo de todas as chaves, junto com o `store_id`. Nada global: o mesmo Redis
    pode servir a mais de um agente em desenvolvimento, e NFR-6 vale também aqui."""

    event_ttl_s: float = 24 * 3600
    """Alertar sobre um furto de ontem tem valor operacional baixo e ocupa a fila de
    triagem, que é o recurso escasso do lado humano (§3.6)."""

    clip_ttl_s: float = 6 * 3600
    """Menor que o do evento de propósito: clipes são descartados antes dos eventos
    (§3.6, R-13). O alerta sem vídeo ainda é triável; o vídeo sem alerta não existe."""

    lease_s: float = 120.0
    """Quanto um item fica reservado durante uma tentativa. Precisa cobrir o upload
    mais lento aceitável: um lease curto demais faria outra tentativa começar em cima
    de um `PUT` ainda em andamento."""

    dead_letter_max: int = 500
    """Teto da fila morta. Ela existe para diagnóstico, não para retenção."""

    idle_poll_s: float = 1.0
    """Espera do sender quando não há nada pronto. É `Event.wait`, então um item novo
    ou um `stop()` acordam antes do prazo."""

    socket_timeout_s: float = 2.0
    """Timeout do cliente Redis. Curto porque o enfileiramento acontece na thread do
    recorder: um Redis pendurado não pode segurar o corte do próximo clipe."""

    retry: RetryOptions = field(default_factory=RetryOptions)

    def __post_init__(self) -> None:
        if not self.redis_url:
            raise ValueError("redis_url é obrigatória")
        if not self.key_prefix:
            raise ValueError("key_prefix é obrigatório: chave sem prefixo colide entre agentes")
        if self.clip_ttl_s > self.event_ttl_s:
            raise ValueError(
                f"clip_ttl_s ({self.clip_ttl_s}s) não pode passar de event_ttl_s "
                f"({self.event_ttl_s}s): o §3.6 exige descartar clipe antes de evento"
            )
        if self.lease_s <= 0:
            raise ValueError(f"lease_s deve ser positivo, recebi {self.lease_s}")
        if self.dead_letter_max <= 0:
            raise ValueError(f"dead_letter_max deve ser positivo, recebi {self.dead_letter_max}")


@dataclass(frozen=True, slots=True)
class DetectionOptions:
    """Estágio 2 (§3.2). Um modelo e uma fila para o agente inteiro (ADR-007)."""

    enabled: bool = False
    """Desligado por padrão: sem modelo no disco, o agente ainda é um gravador de
    clipes útil, e o §3.9 exige que o mesmo artefato rode onde não há GPU."""

    model_path: Path | None = None
    """Arquivo `.onnx`. Hoje vem da configuração local; o `GET /v1/models/current` do
    §5.2 vai entregar este arquivo com checksum e versão (ADR-006)."""

    input_size: int = 640
    """Lado do quadrado que o modelo consome. É conferido contra a forma declarada no
    próprio `.onnx` na subida — divergir aqui decodifica caixas plausíveis e erradas."""

    providers: tuple[str, ...] = ("CUDAExecutionProvider", "CPUExecutionProvider")
    """Ordem de preferência do ONNX Runtime. É esta lista que implementa o fallback do
    §3.2 sem nenhum `if gpu`: num box sem placa, ou com o driver quebrado, o runtime
    simplesmente cai para o próximo da lista e o heartbeat reporta qual pegou (§10.10)."""

    score_threshold: float = 0.10
    """**Piso**, não o limiar de decisão. Abaixo disto a caixa não vale nada para
    ninguém; o corte que separa "é uma pessoa" de "talvez" mora no estágio 3
    (`TrackingOptions.high_threshold`).

    Este número já foi 0,35, e desceu quando o §3.3 entrou. Não é afrouxamento: o
    ByteTrack existe justamente para aproveitar as caixas fracas, oferecendo-as **só** a
    tracks que já existem, numa segunda passada de associação. É assim que a pessoa que
    passa atrás de uma gôndola continua sendo a mesma pessoa em vez de virar um track
    novo — e track que nasce do nada perto da saída é o falso positivo do R-2.

    Manter 0,35 aqui esconderia do tracker exatamente o dado de que ele mais precisa."""

    iou_threshold: float = 0.45
    classes: tuple[int, ...] = (0,)
    """Índices COCO de interesse; 0 é `person`. O MVP só precisa de pessoas — as outras
    79 classes seriam inferência paga e descartada três estágios depois."""

    queue_size: int = 8
    """Frames esperando inferência. Pequeno de propósito: o §3.2 manda descartar o mais
    antigo quando a GPU não acompanha, e uma fila grande troca descarte por latência —
    o frame inferido ficaria velho, e o §3.4 mede tempo em zona com ele."""

    fps_window: int = 32
    """Inferências consideradas no cálculo de `inference_fps`. Janela, e não média
    desde a subida, para uma degradação recente aparecer no heartbeat."""

    def __post_init__(self) -> None:
        if self.enabled and self.model_path is None:
            raise ValueError("detecção habilitada exige model_path")
        if self.input_size <= 0 or self.input_size % 32:
            raise ValueError(
                f"input_size deve ser múltiplo positivo de 32, recebi {self.input_size}: "
                "é o maior stride da grade de âncoras"
            )
        if not self.providers:
            raise ValueError("providers não pode ser vazio: o ONNX Runtime não teria onde rodar")
        for nome, valor in (
            ("score_threshold", self.score_threshold),
            ("iou_threshold", self.iou_threshold),
        ):
            if not 0.0 <= valor <= 1.0:
                raise ValueError(f"{nome} deve estar entre 0 e 1, recebi {valor}")
        if self.queue_size <= 0:
            raise ValueError(f"queue_size deve ser positivo, recebi {self.queue_size}")
        if self.fps_window < 2:
            raise ValueError(f"fps_window precisa de ao menos 2 amostras, recebi {self.fps_window}")


@dataclass(frozen=True, slots=True)
class TrackingOptions:
    """Estágio 3 (§3.3). Um tracker por câmera, IDs locais e efêmeros.

    Todas as janelas de tempo são **em segundos, não em frames**. As implementações de
    referência contam frames porque assumem 30 fps fixos; aqui a cadência é 3 fps e
    varia com o descarte do §3.2, então contar frames faria a tolerância a oclusão mudar
    sozinha conforme a carga do box.
    """

    enabled: bool = True

    high_threshold: float = 0.5
    """Fronteira entre detecção confiável e detecção fraca. Acima dela, uma caixa pode
    criar track; abaixo, ela só continua um track que já existia (§3.3, segunda passada
    de associação). É este limiar que preserva a precisão depois de o piso do detector
    ter descido para 0,10 — ver `DetectionOptions.score_threshold`."""

    iou_min: float = 0.20
    """Gate da primeira passada. Frouxo, e o motivo é aritmética de loja:

    uma pessoa a 1,4 m/s, com 1,70 m ocupando 200 px, percorre ~55 px entre dois frames
    a 3 fps. A caixa de alguém em pé tem cerca de 0,4 da altura, ou 80 px. Duas caixas de
    80 px deslocadas de 55 têm **IoU de 0,19** — e são a mesma pessoa.

    Os limiares de 0,3 a 0,5 que aparecem em toda implementação de referência assumem
    30 fps, onde o deslocamento entre frames é de poucos pixels. Copiá-los para cá faria
    o tracker perder justamente quem anda rápido — que numa loja é quem está saindo.

    Aqui o gate é frouxo porque a comparação não é contra a caixa antiga: é contra a
    previsão do Kalman, que já andou junto com a pessoa."""

    iou_min_baixa: float = 0.40
    """Gate da segunda passada, **mais apertado** que o da primeira. A detecção fraca já
    é duvidosa; exigir dela concordância geométrica melhor é o que impede uma sombra de
    sequestrar um track. Ainda assim abaixo dos 0,5 da referência, pela mesma aritmética
    de `iou_min`."""

    iou_min_novo: float = 0.10
    """Gate de um track ainda provisório, e o mais frouxo dos três **de propósito**.

    Um track recém-nascido não tem velocidade estimada, então a previsão do Kalman é a
    caixa parada no lugar de antes — sem nada para compensar os 55 px que a pessoa andou.
    É exatamente o número calculado em `iou_min`: com gate acima de 0,19, uma pessoa
    andando em ritmo normal nunca chega a formar um track, e o agente só rastrearia quem
    está parado."""

    min_hits: int = 2
    """Associações até um track valer como pessoa. Piso do tracker, não o filtro de
    verdade: o "tempo mínimo de vida do track" do §3.3 é limiar por câmera e mora no
    §3.4, que recebe `age_s` e `hits` e decide (ADR-002)."""

    max_perdido_s: float = 2.0
    """Quanto tempo um track sobrevive sem detecção antes de encerrar. A 3 fps são uns
    seis frames — o bastante para alguém passar atrás de uma gôndola e voltar sendo a
    mesma pessoa, e pouco o bastante para não colar o ID de quem saiu em quem entrou."""

    max_tracks: int = 64
    """Teto defensivo por câmera. Uma câmera apontada para a rua, ou o modelo alucinando
    numa cena difícil, não pode fazer o agente crescer sem limite."""

    def __post_init__(self) -> None:
        for nome, valor in (
            ("high_threshold", self.high_threshold),
            ("iou_min", self.iou_min),
            ("iou_min_baixa", self.iou_min_baixa),
            ("iou_min_novo", self.iou_min_novo),
        ):
            if not 0.0 <= valor <= 1.0:
                raise ValueError(f"{nome} deve estar entre 0 e 1, recebi {valor}")
        if self.min_hits < 1:
            raise ValueError(f"min_hits deve ser ao menos 1, recebi {self.min_hits}")
        if self.max_perdido_s < 0:
            raise ValueError(f"max_perdido_s não pode ser negativo, recebi {self.max_perdido_s}")
        if self.max_tracks < 1:
            raise ValueError(f"max_tracks deve ser positivo, recebi {self.max_tracks}")


@dataclass(frozen=True, slots=True)
class RuleOptions:
    """Estágio 4 (§3.4). Zonas e limiares **por câmera** — é aqui que o ADR-002 vira
    dado.

    Nada neste bloco é constante de código, e não pode ser: recalibrar uma loja tem que
    ser mudar configuração, não retreinar rede nem republicar imagem. Em produção isto
    desce da nuvem pelo `GET /v1/agents/config` (§5.2), versionado, e a `rule_version`
    viaja junto no evento para que uma taxa histórica de falso positivo signifique
    alguma coisa (§6).

    Os números são **hipóteses de projeto**, não medições — como todo limiar da
    arquitetura. `tempo_caixa_min_s` em particular é a pendência §10.5, e o
    `tempo_caixa_medio_s` do heartbeat existe para resolvê-la com dado da loja.
    """

    enabled: bool = False
    """Desligado por padrão: sem zonas desenhadas não há o que decidir, e o agente
    continua sendo ingestão, detecção, tracking e gatilho manual."""

    rule_id: str = "saida-sem-caixa"
    rule_version: int = 1
    """Sobe no evento. Formato restrito ao `identifier` do contrato (§5)."""

    linha_saida: LinhaOrientada | None = None
    zonas_caixa: tuple[Poligono, ...] = ()
    """Mais de uma porque mercado de bairro tem duas ou três posições de caixa, e
    obrigar a desenhar um polígono só engolindo o corredor entre elas transformaria o
    corredor em zona de caixa — quem passa direto sairia descartado."""

    tempo_caixa_min_s: float = 8.0
    """O **N** da regra principal: cruzou a saída sem ter ficado N segundos no caixa.
    Por layout de loja, a validar com dado real (§10.5)."""

    vida_min_s: float = 2.0
    """Tempo mínimo de vida do track antes de ele valer para regra (§3.3, §3.4).

    É a defesa contra o R-2 e o número mais delicado daqui. Baixo demais, um track que
    nasceu de troca de ID perto da porta vira alerta; alto demais, quem entra correndo
    e sai correndo nunca é avaliado. A 3 fps, 2 s são uns seis frames."""

    hits_min: int = 3
    """Associações mínimas, junto com `vida_min_s`. Os dois, e não só o tempo: um track
    perdido quase o tempo todo envelhece sem nunca ter sido visto de verdade."""

    intervalo_max_s: float = 1.0
    """Teto do intervalo creditado ao tempo de caixa entre duas amostras.

    Sem ele, uma oclusão longa dentro da zona credita todo o tempo ocluído como tempo
    de caixa, e a pessoa sai descartada por um crédito que ninguém observou. O teto
    empurra o erro para o lado seguro do produto: na dúvida, o tempo de caixa é
    subestimado e o evento **sobe** para triagem humana."""

    janela_tempo_caixa: int = 64
    """Travessias consideradas na média de `tempo_caixa_medio_s`. Janela, e não média
    desde a subida, pelo mesmo motivo do `inference_fps` do §3.2."""

    def __post_init__(self) -> None:
        if not self.enabled:
            return
        if self.linha_saida is None:
            raise ValueError(
                "regra habilitada exige linha_saida: sem a linha não existe travessia, "
                "e a regra principal do §3.4 é sobre a travessia"
            )
        if not self.zonas_caixa:
            # Recusar é o ponto. A regra compara tempo numa zona que não existe: todo
            # mundo teria zero, e toda saída da loja viraria alerta. Um agente que
            # subisse assim alertaria a loja inteira até alguém desligar a notificação,
            # que é exatamente como o R-1 mata o produto.
            raise ValueError(
                "regra habilitada exige ao menos uma zona de caixa: sem ela o tempo no "
                "caixa é sempre zero e toda saída da loja vira alerta (R-1)"
            )
        if self.tempo_caixa_min_s <= 0:
            raise ValueError(
                f"tempo_caixa_min_s deve ser positivo, recebi {self.tempo_caixa_min_s}: "
                "com zero, nenhuma travessia jamais dispara e a câmera fica muda"
            )
        if self.vida_min_s < 0:
            raise ValueError(f"vida_min_s não pode ser negativo, recebi {self.vida_min_s}")
        if self.hits_min < 1:
            raise ValueError(f"hits_min deve ser ao menos 1, recebi {self.hits_min}")
        if self.intervalo_max_s <= 0:
            raise ValueError(f"intervalo_max_s deve ser positivo, recebi {self.intervalo_max_s}")
        if self.janela_tempo_caixa < 1:
            raise ValueError(
                f"janela_tempo_caixa deve ser positiva, recebi {self.janela_tempo_caixa}"
            )

    @property
    def pontos(self) -> tuple[tuple[float, float], ...]:
        """Todo vértice e ponta de linha, para quem precisa conferir os limites."""
        pontos: list[tuple[float, float]] = []
        if self.linha_saida is not None:
            pontos.extend((self.linha_saida.origem, self.linha_saida.destino))
        for zona in self.zonas_caixa:
            pontos.extend(zona.vertices)
        return tuple(pontos)


@dataclass(frozen=True, slots=True)
class CameraConfig:
    camera_id: str
    url: str
    decode: DecodeOptions = field(default_factory=DecodeOptions)
    supervision: SupervisionOptions = field(default_factory=SupervisionOptions)
    clip: ClipOptions = field(default_factory=ClipOptions)
    rules: RuleOptions = field(default_factory=RuleOptions)
    """Zonas e limiares desta câmera (§3.4). Por câmera, não por loja: a linha de
    saída de uma é o corredor da outra."""

    detect: bool = True
    """Se esta câmera é elegível para IA. O R-3 prevê câmeras com ângulo, altura ou
    contraluz que as tornam inúteis para detecção — elas continuam alimentando o buffer
    do clipe e respondendo a gatilho manual, sem gastar inferência."""

    def __post_init__(self) -> None:
        if not self.camera_id:
            raise ValueError("camera_id é obrigatório")
        if not self.url:
            raise ValueError("url é obrigatória")
        self._valida_regras()

    def _valida_regras(self) -> None:
        """Recusa na subida a zona que nunca conteria ninguém.

        As zonas são desenhadas sobre um frame no dashboard (§4.5) e avaliadas contra
        as caixas que saem do §3.2 — que estão no espaço do frame **decodificado**.
        Quando os dois não são a mesma resolução, nada falha: o polígono cai fora do
        quadro, `contem` devolve `False` para todo mundo, e a câmera passa a alertar
        para a loja inteira porque ninguém nunca esteve no caixa. É o caminho mais
        curto do sistema até o R-1, e o único sintoma seria o volume de alertas.
        """
        if not self.rules.enabled:
            return
        if not self.detect:
            raise ValueError(
                f"câmera {self.camera_id} tem regra habilitada e detect=False: sem "
                "detecção não há track, e sem track a regra nunca avalia nada"
            )
        largura, altura = self.decode.width, self.decode.height
        fora = [
            ponto
            for ponto in self.rules.pontos
            if not (0 <= ponto[0] <= largura and 0 <= ponto[1] <= altura)
        ]
        if fora:
            raise ValueError(
                f"câmera {self.camera_id} decodifica em {largura}x{altura} e tem zonas "
                f"com pontos fora do quadro: {fora}. As zonas foram desenhadas sobre um "
                "frame de outra resolução? Uma zona fora do quadro não contém ninguém, "
                "e aí toda saída da loja vira alerta (R-1)"
            )


@dataclass(frozen=True, slots=True)
class CloudOptions:
    """Endereço e credencial do control plane (§5.1, §5.2)."""

    api_url: str = "http://localhost:3000"
    token: str | None = None
    """Bearer estático por enquanto. O provisionamento por token de bootstrap e a
    rotação da §5.1 dependem do `POST /v1/agents/register`, que ainda não existe."""

    timeout_s: float = 10.0
    upload_timeout_s: float = 120.0
    """Maior que o das chamadas JSON porque sobe megabytes por um link de loja. Ainda
    assim finito: sem timeout, um upload pendurado seguraria a fila inteira, já que a
    thread de envio é uma só."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """O agente inteiro: as câmeras da loja, onde ficam os clipes e para onde subir.

    Em produção isto vem da nuvem (§5.2). Hoje vem da linha de comando, e a fronteira
    é justamente esta classe — nada abaixo dela sabe de onde a configuração veio, que
    é o que o §3.9 exige para o mesmo artefato rodar na loja e na nuvem.
    """

    tenant_id: str
    store_id: str
    cameras: tuple[CameraConfig, ...]
    clips_dir: Path
    outbox: OutboxOptions = field(default_factory=OutboxOptions)
    cloud: CloudOptions = field(default_factory=CloudOptions)
    detection: DetectionOptions = field(default_factory=DetectionOptions)
    tracking: TrackingOptions = field(default_factory=TrackingOptions)
    ffmpeg_bin: str = "ffmpeg"

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.store_id:
            raise ValueError("tenant_id e store_id são obrigatórios (NFR-6)")
        if not self.cameras:
            raise ValueError("o agente precisa de ao menos uma câmera")

        ids = [camera.camera_id for camera in self.cameras]
        if len(set(ids)) != len(ids):
            raise ValueError(f"camera_id repetido em {ids}: os clipes iriam para o buffer errado")

        # O buffer circular é por câmera, mas o recorder e o diretório de clipes são do
        # agente inteiro. Divergir nestes campos não tem representação possível, e
        # falhar na subida é melhor que cortar silenciosamente com o pós-roll da câmera
        # errada — um clipe curto demais chega ao triador sem o momento do evento.
        comuns = {
            campo: {getattr(camera.clip, campo) for camera in self.cameras}
            for campo in ("pre_roll_s", "post_roll_s", "post_roll_grace_s", "remux_timeout_s")
        }
        divergentes = {campo: valores for campo, valores in comuns.items() if len(valores) > 1}
        if divergentes:
            raise ValueError(
                f"as câmeras divergem em {divergentes}, mas o corte é um só para o agente. "
                "Rolls por câmera exigem mudar o ClipRecorder (pendência registrada)"
            )

        self._valida_deteccao()

    def _valida_deteccao(self) -> None:
        """Recusa na subida o que o estágio 2 recusaria a cada frame.

        As duas exigências saem do `preprocess`: o maior lado do frame tem que bater com
        a entrada do modelo (nenhum redimensionamento acontece em Python) e o formato
        tem que ser BGR24. Descobrir isso em produção significaria um agente que ingere,
        grava clipe, e nunca detecta nada — falhando uma vez por frame, num log que
        ninguém lê. O conserto é no filtro do ffmpeg, e é de graça.
        """
        if not self.detection.enabled:
            return

        lado = self.detection.input_size
        for camera in self.cameras:
            if not camera.detect:
                continue
            maior = max(camera.decode.width, camera.decode.height)
            if maior != lado:
                raise ValueError(
                    f"câmera {camera.camera_id} decodifica em "
                    f"{camera.decode.width}x{camera.decode.height}, e o modelo consome {lado}: "
                    f"o maior lado precisa ser exatamente {lado}. Ajuste DecodeOptions.width/"
                    f"height — o ffmpeg já escala no filtro que existe (§3.1)"
                )
            if camera.decode.pixel_format is not PixelFormat.BGR24:
                raise ValueError(
                    f"câmera {camera.camera_id} entrega {camera.decode.pixel_format} e o "
                    f"detector consome {PixelFormat.BGR24}. Marque a câmera com detect=False "
                    "ou acrescente `format=bgr24` ao fim da cadeia de filtros"
                )

    @property
    def clip(self) -> ClipOptions:
        """As opções de corte válidas para o agente, já validadas como uniformes."""
        return self.cameras[0].clip
