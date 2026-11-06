"""Decode SimCC, a saída do RTMPose, de volta a juntas no frame da câmera.

O ONNX do RTMPose não devolve coordenadas. Devolve dois vetores de
classificação por junta — um em x, um em y — no espaço do recorte, com o
eixo mais longo que a imagem pelo `simcc_split_ratio`. A config oficial do
RTMPose-s COCO-256×192 (MMPose, Apache-2.0,
`configs/body_2d_keypoint/rtmpose/coco/rtmpose-s_8xb256-420e_coco-256x192.py`)
fixa isso:

    codec = dict(type='SimCCLabel', input_size=(192, 256),
                 simcc_split_ratio=2.0, normalize=False, use_dark=False)
    head = dict(type='RTMCCHead', out_channels=17, ...)

São 17 juntas, a ordem COCO (nariz … tornozelos), não as 133 do WholeBody nem
as 136 do Halpe. `use_dark=False`: não há refinamento sub-bin. `normalize=False`
no codec e `apply_softmax` default False em `get_simcc_maximum`
(`mmpose/codecs/utils/post_processing.py`): a confiança é o menor pico cru dos
dois eixos, e um pico de 0.04 continua 0.04. Softmax transformaria junta fraca
em junta certa.

O pico dividido pelo split ratio está no recorte 192×256. Quem devolve a junta
ao frame é a inversa do `Crop` (`undo_crop`). Sem ela, o pulso fica na
coordenada do recorte — dezenas de pixels fora da pessoa — e o gesto de
ocultação lê o lugar errado sem erro nenhum.

A sessão abre em `OnnxPose`, no mesmo mecanismo do detector: `model_version`
(nome e o começo do sha256, R-7), a mesma lista de providers (CUDA e, se a
placa cair, CPU; §3.2, §10.10) e o import adiado do onnxruntime.
`recusa_modelo_fora_do_piloto` roda antes de abrir o arquivo — o par, para a
pose, de `recusa_pesos_agpl`. A forma é conferida na subida: entrada
1×3×256×192 e saídas SimCC de 17 juntas (384 bins em x, 512 em y). Divergir
aqui não estoura no primeiro frame; produz pulso no índice errado.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from lince_agent.detect.onnx import ModelError as DetectorModelError
from lince_agent.detect.onnx import _abre_sessao, recusa_pesos_agpl
from lince_agent.detect.onnx import model_version as _model_version_detector
from lince_agent.ffmpeg.rawframe import Frame
from lince_agent.pose.preprocess import (
    INPUT_ALTURA,
    INPUT_LARGURA,
    RTMPOSE,
    Crop,
    PreprocessError,
    PreprocessSpec,
    crop_bbox,
    frame_to_bgr,
    undo_crop,
)

log = logging.getLogger(__name__)

# A mesma ordem de `DetectionOptions.providers`. CUDA primeiro; CPU se a placa
# cair (§10.10). A lista mora aqui para a pose não nascer diferente do detector.
PROVIDERS: tuple[str, ...] = ("CUDAExecutionProvider", "CPUExecutionProvider")

NUM_JUNTAS = 17
"""`out_channels` da RTMCCHead na config do RTMPose-s COCO. Não é WholeBody."""

SPLIT_RATIO = 2.0
"""`simcc_split_ratio` da mesma config. O vetor x tem 384 bins; o y, 512."""

# Ordem COCO, que é a ordem dos canais do RTMPose-s. "Esquerdo" é o esquerdo
# da pessoa, não o da imagem. Trocar os dois coloca o gesto no pulso errado.
JUNTAS_COCO17: tuple[str, ...] = (
    "nariz",
    "olho_esquerdo",
    "olho_direito",
    "orelha_esquerda",
    "orelha_direita",
    "ombro_esquerdo",
    "ombro_direito",
    "cotovelo_esquerdo",
    "cotovelo_direito",
    "pulso_esquerdo",
    "pulso_direito",
    "quadril_esquerdo",
    "quadril_direito",
    "joelho_esquerdo",
    "joelho_direito",
    "tornozelo_esquerdo",
    "tornozelo_direito",
)

_FORA_DO_PILOTO = re.compile(r"whole[-_]?body|halpe", re.IGNORECASE)
"""WholeBody e Halpe no nome do arquivo. Olha só o nome, como
`recusa_pesos_agpl`: um diretório chamado `halpe-comparativo` não pode
impedir o box de subir."""


class ModelError(RuntimeError):
    """O arquivo não é o RTMPose-s COCO-17 do piloto."""


class DecodeError(ValueError):
    """SimCC que não dá para ler como 17 juntas. Sempre diz o que divergiu."""


@dataclass(frozen=True, slots=True)
class Junta:
    """Uma junta no frame da câmera, já com o recorte desfeito.

    Confiança ≤ 0 segue o `get_simcc_maximum`: os dois eixos viram o sentinela
    -1, dividido pelo split ratio e projetado. Não é o argmax desse vetor — o
    pico menos ruim de um mapa não positivo cairia noutro lugar, com cara de
    pulso. Confiança não finita fica sem coordenada: `nan <= limiar` é falso,
    e uma junta com x/y preenchido passaria no gesto.
    """

    indice: int
    x: float
    y: float
    confianca: float

    @property
    def nome(self) -> str:
        return JUNTAS_COCO17[self.indice]


def model_version(model_path: Path) -> str:
    """Identidade do arquivo: nome mais o começo do sha256.

    É `detect.onnx.model_version`, não uma segunda receita. Dois boxes com
    `rtmpose-s.onnx` podem ter arquivos diferentes depois de um rollout
    parcial (R-7); hashes diferentes no heartbeat esconderiam exatamente isso.
    """
    return _model_version_detector(model_path)


def recusa_modelo_fora_do_piloto(model_path: Path) -> None:
    """Recusa Ultralytics, COCO-WholeBody e Halpe pelo nome do arquivo.

    Não exige que o arquivo exista: a licença e a contagem de juntas se leem
    do nome, como no detector, e `OnnxPose` chama isto antes de abrir a sessão.
    Um ONNX de 133 juntas decodificado como 17 não estoura: o pulso sai no
    índice errado e o gesto de ocultação olha para outra parte do corpo.
    """
    try:
        recusa_pesos_agpl(model_path)
    except DetectorModelError as erro:
        raise ModelError(str(erro)) from erro
    marca = _FORA_DO_PILOTO.search(model_path.name)
    if marca is not None:
        raise ModelError(
            f"{model_path.name} não é o RTMPose-s COCO-17 do piloto "
            f"('{marca.group(0)}' no nome). WholeBody (133 juntas) e Halpe (136) "
            "têm outra contagem; decodificar isso como 17 canais coloca o pulso "
            "no índice errado, sem erro. O piloto usa o RTMPose-s COCO-17 "
            "exportado do MMPose (Apache-2.0)"
        )


def decode_simcc(
    simcc_x: np.ndarray,
    simcc_y: np.ndarray,
    crop: Crop,
    *,
    split_ratio: float = SPLIT_RATIO,
) -> tuple[Junta, ...]:
    """De `simcc_x` e `simcc_y` para 17 juntas no frame da câmera.

    Aceita o lote de uma pessoa `(1, 17, bins)` que o ONNX devolve, ou o
    `(17, bins)` sem o lote. `bins` tem que ser `lado_do_recorte * split_ratio`
    (384 e 512 com o default). Esquecer o 2.0 coloca a junta na metade do
    caminho entre o canto do recorte e o corpo, e o número continua plausível.
    """
    if not math.isfinite(split_ratio) or split_ratio <= 0:
        raise DecodeError(
            f"simcc_split_ratio deve ser positivo (a config do RTMPose-s usa "
            f"{SPLIT_RATIO}), recebi {split_ratio}"
        )
    eixo_x = _sem_lote(np.asarray(simcc_x), "simcc_x")
    eixo_y = _sem_lote(np.asarray(simcc_y), "simcc_y")
    if eixo_x.shape[0] != NUM_JUNTAS or eixo_y.shape[0] != NUM_JUNTAS:
        raise DecodeError(
            f"RTMPose-s COCO-17 tem {NUM_JUNTAS} juntas; recebi simcc_x {eixo_x.shape} "
            f"e simcc_y {eixo_y.shape}. WholeBody (133) e Halpe (136) não são este modelo"
        )
    bins_x = int(round(crop.input_width * split_ratio))
    bins_y = int(round(crop.input_height * split_ratio))
    if eixo_x.shape[1] != bins_x or eixo_y.shape[1] != bins_y:
        raise DecodeError(
            f"SimCC com split ratio {split_ratio} sobre recorte "
            f"{crop.input_width}×{crop.input_height} (largura×altura) tem vetores "
            f"({bins_x}, {bins_y}); recebi {eixo_x.shape[1]} e {eixo_y.shape[1]}. "
            f"Esquecer o split ratio de {SPLIT_RATIO} coloca a junta na metade do "
            "caminho, sem erro nenhum"
        )

    # Pico cru, como `get_simcc_maximum` com apply_softmax=False. A cópia é o
    # que impede esta leitura de escrever no buffer de quem chamou: um softmax
    # in-place mudaria a confiança e o tensor ainda pareceria o mesmo.
    eixo_x = np.array(eixo_x, dtype=np.float64, copy=True)
    eixo_y = np.array(eixo_y, dtype=np.float64, copy=True)
    bin_x = np.argmax(eixo_x, axis=1).astype(np.float64)
    bin_y = np.argmax(eixo_y, axis=1).astype(np.float64)
    confianca = np.minimum(np.max(eixo_x, axis=1), np.max(eixo_y, axis=1))
    # `locs[vals <= 0] = -1` no get_simcc_maximum, e o decode divide isso pelo
    # split ratio. O argmax de um vetor não positivo não entra.
    sem_pico = confianca <= 0
    bin_x[sem_pico] = -1.0
    bin_y[sem_pico] = -1.0
    no_recorte = np.stack((bin_x / split_ratio, bin_y / split_ratio), axis=1).astype(np.float32)
    no_frame = undo_crop(no_recorte, crop)

    juntas: list[Junta] = []
    for indice in range(NUM_JUNTAS):
        valor = float(confianca[indice])
        if not math.isfinite(valor):
            x = y = math.nan
        else:
            x = float(no_frame[indice, 0])
            y = float(no_frame[indice, 1])
        juntas.append(Junta(indice=indice, x=x, y=y, confianca=valor))
    return tuple(juntas)


BINS_X = int(INPUT_LARGURA * SPLIT_RATIO)
"""384. Largura do recorte vezes o `simcc_split_ratio` da config."""

BINS_Y = int(INPUT_ALTURA * SPLIT_RATIO)
"""512. Altura do recorte vezes o mesmo ratio."""

_NOME_EIXO_X = re.compile(r"simcc[_-]?x", re.IGNORECASE)
_NOME_EIXO_Y = re.compile(r"simcc[_-]?y", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PoseInfo:
    """Identidade da pose carregada. O heartbeat ainda não publica este campo;
    o `model_version` já nasce no formato do detector, para o dia em que publicar."""

    model_version: str
    provider: str
    input_width: int
    input_height: int
    lote_dinamico: bool


@dataclass(frozen=True, slots=True)
class PosePessoa:
    """As 17 juntas de uma caixa, na ordem COCO, já no frame da câmera.

    A confiança de cada junta está em `Junta.confianca` — o menor pico cru dos
    dois eixos, não um softmax. `confiancas` só projeta isso na mesma ordem.
    """

    juntas: tuple[Junta, ...]

    @property
    def confiancas(self) -> tuple[float, ...]:
        return tuple(junta.confianca for junta in self.juntas)


class OnnxPose:
    """RTMPose-s COCO-17 sobre uma sessão do ONNX Runtime.

    Recebe o frame BGR (o array ou o `Frame` do ffmpeg) e as caixas da pessoa,
    no mesmo espaço do detector depois do `undo_letterbox`. Devolve uma
    `PosePessoa` por caixa, na mesma ordem.

    O eixo de lote decide como as caixas andam. O export dinâmico do mmdeploy
    (`pose-detection_simcc_onnxruntime_dynamic.py`) deixa o batch livre: as
    caixas do frame entram juntas. Um ONNX com lote fixo 1 — o export estático,
    ou um arquivo antigo — não aceita N e não avisa com a junta errada: a sessão
    estoura ou, pior, some com as pessoas que sobraram. Aí o loop é uma caixa
    por chamada. Lote fixo diferente de 1 é recusado na subida: um frame com
    outra contagem de pessoas não teria o que fazer.
    """

    def __init__(
        self,
        model_path: Path,
        *,
        providers: tuple[str, ...] = PROVIDERS,
        spec: PreprocessSpec = RTMPOSE,
        session_factory: Callable[[Path, tuple[str, ...]], Any] | None = None,
    ) -> None:
        # Antes de conferir se o arquivo existe. Um caminho `wholebody` ou
        # `yolov8n-pose` tem que dizer o motivo, não "modelo não encontrado".
        recusa_modelo_fora_do_piloto(model_path)
        if not model_path.is_file():
            raise ModelError(f"modelo não encontrado em {model_path}")
        if not providers:
            raise ModelError("providers não pode ser vazio: o ONNX Runtime não teria onde rodar")

        self._spec = spec
        self._providers = providers
        self._session = (session_factory or _abre)(model_path, providers)
        contrato = _contrato_do_piloto(self._session, model_path)
        self._nome_entrada = contrato.nome_entrada
        self._nome_x = contrato.nome_simcc_x
        self._nome_y = contrato.nome_simcc_y
        self._lote_dinamico = contrato.lote_dinamico

        provider = self._session.get_providers()[0]
        self._info = PoseInfo(
            model_version=model_version(model_path),
            provider=provider,
            input_width=spec.input_width,
            input_height=spec.input_height,
            lote_dinamico=self._lote_dinamico,
        )
        # Pedido não é obtido: o box aceita CUDA na lista e roda em CPU sem
        # dizer nada. O aviso na subida é o que separa "a pose está lenta" de
        # "a pose caiu para CPU" (§10.10).
        if provider != providers[0]:
            log.warning(
                "provider %s indisponível; a pose vai rodar em %s",
                providers[0],
                provider,
            )
        log.info(
            "pose %s carregada em %s (entrada %d×%d, lote %s)",
            self._info.model_version,
            provider,
            spec.input_width,
            spec.input_height,
            "dinâmico" if self._lote_dinamico else "1",
        )

    @property
    def lote_dinamico(self) -> bool:
        return self._lote_dinamico

    def info(self) -> PoseInfo:
        return self._info

    def estimar(
        self,
        imagem: np.ndarray | Frame,
        caixas: Sequence[Any],
    ) -> tuple[PosePessoa, ...]:
        """Juntas e confianças de cada caixa, no frame da câmera.

        Caixa vazia não toca a sessão: um frame sem pessoa não pode pagar
        uma inferência para devolver nada.
        """
        if self._session is None:
            raise ModelError("a sessão da pose já foi fechada")
        if isinstance(imagem, Frame):
            imagem = frame_to_bgr(imagem)
        lista = _como_caixas(caixas)
        if not lista:
            return ()

        tensores: list[np.ndarray] = []
        crops: list[Crop] = []
        for caixa in lista:
            tensor, crop = crop_bbox(imagem, caixa, spec=self._spec)
            tensores.append(tensor)
            crops.append(crop)
        simcc_x, simcc_y = self._inferir_caixas(tensores)
        return self._decodificar(simcc_x, simcc_y, crops)

    def inferir(self, tensor: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Uma chamada da sessão. Devolve `simcc_x` e `simcc_y`, ainda no recorte.

        `tensor` é NCHW float32. Com lote dinâmico, N pode ser o frame inteiro;
        com lote fixo 1, N tem que ser 1 — quem junta as caixas é `estimar`.
        Sem o `decode_simcc`, este retorno é coordenada de bin, não de frame.
        """
        if self._session is None:
            raise ModelError("a sessão da pose já foi fechada")
        array = np.ascontiguousarray(tensor, dtype=np.float32)
        if array.ndim != 4:
            raise ModelError(
                f"a sessão espera NCHW (lote, 3, {INPUT_ALTURA}, {INPUT_LARGURA}), "
                f"recebi {array.shape}"
            )
        if not self._lote_dinamico and int(array.shape[0]) != 1:
            raise ModelError(
                f"este ONNX tem lote fixo 1 e recebeu {array.shape[0]}. "
                "Várias pessoas de uma vez só cabem no export dinâmico "
                "(pose-detection_simcc_onnxruntime_dynamic.py); no lote fixo, "
                "`estimar` chama a sessão uma vez por caixa"
            )
        saidas = self._session.run(
            [self._nome_x, self._nome_y],
            {self._nome_entrada: array},
        )
        return saidas[0], saidas[1]

    def close(self) -> None:
        self._session = None

    def _inferir_caixas(self, tensores: Sequence[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
        if self._lote_dinamico and len(tensores) > 1:
            return self.inferir(np.concatenate(tensores, axis=0))
        eixos_x: list[np.ndarray] = []
        eixos_y: list[np.ndarray] = []
        for tensor in tensores:
            simcc_x, simcc_y = self.inferir(tensor)
            eixos_x.append(simcc_x)
            eixos_y.append(simcc_y)
        return np.concatenate(eixos_x, axis=0), np.concatenate(eixos_y, axis=0)

    def _decodificar(
        self,
        simcc_x: np.ndarray,
        simcc_y: np.ndarray,
        crops: Sequence[Crop],
    ) -> tuple[PosePessoa, ...]:
        if simcc_x.ndim != 3 or simcc_y.ndim != 3:
            raise ModelError(
                f"simcc_x {getattr(simcc_x, 'shape', None)} e simcc_y "
                f"{getattr(simcc_y, 'shape', None)} não são (lote, 17, bins). "
                "Um ONNX que já devolve coordenada pronta não passa pelo decode SimCC"
            )
        if simcc_x.shape[0] != len(crops) or simcc_y.shape[0] != len(crops):
            raise ModelError(
                f"a sessão devolveu lote {simcc_x.shape[0]} em x e {simcc_y.shape[0]} em y "
                f"para {len(crops)} caixas. O pulso da pessoa seguinte sairia na caixa errada"
            )
        return tuple(
            PosePessoa(decode_simcc(simcc_x[i : i + 1], simcc_y[i : i + 1], crop))
            for i, crop in enumerate(crops)
        )


def _abre(model_path: Path, providers: tuple[str, ...]) -> Any:
    """A mesma abertura do detector, com o erro no tipo da pose.

    `_abre_sessao` filtra o provider que esta instalação realmente tem e recusa
    a lista vazia. Reimplementar isso aqui é a maneira de a pose cair para CPU
    num box em que o detector continua na GPU, ou o contrário (§10.10).
    """
    try:
        return _abre_sessao(model_path, providers)
    except DetectorModelError as erro:
        raise ModelError(str(erro)) from erro


@dataclass(frozen=True, slots=True)
class _Contrato:
    nome_entrada: str
    nome_simcc_x: str
    nome_simcc_y: str
    lote_dinamico: bool


def _contrato_do_piloto(sessao: Any, model_path: Path) -> _Contrato:
    """Confere a forma declarada contra o RTMPose-s 256×192 COCO-17.

    Faz na subida o que o primeiro frame faria em silêncio: entrada de outro
    tamanho, SimCC sem o split ratio 2.0, ou 133 canais de um WholeBody
    renomeado para passar na trava do arquivo. O nome já foi recusado; isto
    pega o arquivo que o nome não denuncia.
    """
    entradas = list(sessao.get_inputs())
    if len(entradas) != 1:
        raise ModelError(
            f"esperava um modelo de uma entrada só, {model_path.name} tem {len(entradas)}"
        )
    entrada = entradas[0]
    _exige_tipo_float(getattr(entrada, "type", ""), f"entrada {entrada.name}")
    forma = _forma(entrada, f"entrada {entrada.name}")
    if len(forma) != 4:
        raise ModelError(
            f"{model_path.name} declara entrada {forma}; o RTMPose-s do piloto é "
            f"1×3×{INPUT_ALTURA}×{INPUT_LARGURA} (lote, canal, altura, largura)"
        )
    lote_entrada = _classifica_lote(forma[0], f"entrada {entrada.name}")
    _exige_eixo(forma[1], 3, f"canais da entrada {entrada.name}", model_path)
    _exige_eixo(
        forma[2],
        INPUT_ALTURA,
        f"altura da entrada {entrada.name}",
        model_path,
    )
    _exige_eixo(
        forma[3],
        INPUT_LARGURA,
        f"largura da entrada {entrada.name}",
        model_path,
    )

    saidas = list(sessao.get_outputs())
    if len(saidas) != 2:
        raise ModelError(
            f"o RTMPose-s exportado com export_postprocess=False tem duas saídas, "
            f"simcc_x ({BINS_X} bins) e simcc_y ({BINS_Y}); {model_path.name} tem "
            f"{len(saidas)} ({[getattr(saida, 'name', '?') for saida in saidas]}). "
            "Um ONNX com a coordenada já decodificada não é este piloto"
        )

    por_eixo: dict[str, str] = {}
    lotes_saida: list[str] = []
    for saida in saidas:
        nome = str(getattr(saida, "name", ""))
        _exige_tipo_float(getattr(saida, "type", ""), f"saída {nome}")
        forma_saida = _forma(saida, f"saída {nome}")
        if len(forma_saida) != 3:
            raise ModelError(
                f"saída {nome} declara {forma_saida}; simcc é (lote, {NUM_JUNTAS}, bins)"
            )
        lotes_saida.append(_classifica_lote(forma_saida[0], f"saída {nome}"))
        _exige_eixo(forma_saida[1], NUM_JUNTAS, f"juntas da saída {nome}", model_path)
        bins = _eixo_fixo(forma_saida[2], f"bins da saída {nome}", model_path)
        eixo = _eixo_simcc(nome, bins, model_path)
        if eixo in por_eixo:
            raise ModelError(
                f"{model_path.name} tem duas saídas no eixo {eixo} "
                f"({por_eixo[eixo]} e {nome}). Faltou o outro eixo do SimCC"
            )
        por_eixo[eixo] = nome

    if set(por_eixo) != {"x", "y"}:
        raise ModelError(
            f"{model_path.name} não declara os dois eixos do SimCC "
            f"(simcc_x com {BINS_X} bins e simcc_y com {BINS_Y}); achei {por_eixo}"
        )

    # Lote dinâmico só vale se a entrada E as duas saídas aceitam N. Saída
    # presa em 1 com entrada livre estoura no frame com duas pessoas — ou,
    # se a sessão cortar, devolve junta só da primeira.
    lote_dinamico = lote_entrada == "dinamico" and all(lote == "dinamico" for lote in lotes_saida)
    if lote_entrada == "dinamico" and not lote_dinamico:
        log.warning(
            "%s tem entrada com lote dinâmico e saída com lote fixo; as caixas vão uma a uma",
            model_path.name,
        )
    return _Contrato(
        nome_entrada=str(entrada.name),
        nome_simcc_x=por_eixo["x"],
        nome_simcc_y=por_eixo["y"],
        lote_dinamico=lote_dinamico,
    )


def _eixo_simcc(nome: str, bins: int, model_path: Path) -> str:
    """O bin conta qual eixo é. O nome, se vier, tem que concordar.

    Confiar só na ordem das saídas troca x com y quando o export lista
    simcc_y primeiro, e o pulso sai na altura do ombro. Confiar só no nome
    aceita um arquivo que se chama simcc_x e tem 512 bins.
    """
    if bins == BINS_X:
        eixo = "x"
    elif bins == BINS_Y:
        eixo = "y"
    else:
        raise ModelError(
            f"saída {nome} de {model_path.name} tem {bins} bins. simcc_x do "
            f"RTMPose-s {INPUT_LARGURA}×{INPUT_ALTURA} tem {BINS_X} "
            f"({INPUT_LARGURA}×{SPLIT_RATIO}) e simcc_y tem {BINS_Y} "
            f"({INPUT_ALTURA}×{SPLIT_RATIO}). Esquecer o split ratio de {SPLIT_RATIO} "
            "na exportação deixa 192 e 256, e a junta cai na metade do caminho"
        )
    if _NOME_EIXO_X.search(nome) and eixo != "x":
        raise ModelError(
            f"saída {nome} tem {bins} bins, que é o eixo y ({BINS_Y}), mas o nome diz x. "
            "O export trocou os eixos; seguir em frente põe o pulso na altura errada"
        )
    if _NOME_EIXO_Y.search(nome) and eixo != "y":
        raise ModelError(
            f"saída {nome} tem {bins} bins, que é o eixo x ({BINS_X}), mas o nome diz y. "
            "O export trocou os eixos; seguir em frente põe o pulso na altura errada"
        )
    return eixo


def _forma(no: Any, o_que: str) -> list[Any]:
    forma = getattr(no, "shape", None)
    if not forma:
        raise ModelError(
            f"{o_que} não declara forma. Sem ela não dá para separar o RTMPose-s "
            f"{INPUT_LARGURA}×{INPUT_ALTURA} de outro modelo com o nome parecido"
        )
    return list(forma)


def _classifica_lote(valor: Any, contexto: str) -> str:
    if isinstance(valor, (bool, np.bool_)):
        raise ModelError(f"{contexto} tem eixo de lote ilegível: {valor!r}")
    if isinstance(valor, (int, np.integer)):
        if int(valor) == 1:
            return "fixo"
        raise ModelError(
            f"{contexto} declara lote {int(valor)}. O piloto aceita lote 1 "
            "(uma pessoa por chamada) ou o eixo dinâmico do export "
            "pose-detection_simcc_onnxruntime_dynamic.py. Um lote fixo diferente "
            "de 1 não serve para um frame com outra contagem de pessoas"
        )
    if valor is None or isinstance(valor, str):
        return "dinamico"
    raise ModelError(f"{contexto} tem eixo de lote ilegível: {valor!r}")


def _exige_eixo(valor: Any, esperado: int, contexto: str, model_path: Path) -> None:
    obtido = _eixo_fixo(valor, contexto, model_path)
    if obtido != esperado:
        raise ModelError(
            f"{contexto} de {model_path.name} é {obtido}, e o RTMPose-s COCO-17 "
            f"do piloto declara {esperado}. Entrada fora de "
            f"1×3×{INPUT_ALTURA}×{INPUT_LARGURA}, ou saída que não seja "
            f"{NUM_JUNTAS} juntas, coloca o pulso no lugar errado sem erro nenhum. "
            "WholeBody tem 133 juntas e Halpe 136 ou 26"
        )


def _eixo_fixo(valor: Any, contexto: str, model_path: Path) -> int:
    if isinstance(valor, (bool, np.bool_)) or not isinstance(valor, (int, np.integer)):
        raise ModelError(
            f"{contexto} de {model_path.name} é dinâmico ({valor!r}). "
            f"O piloto fixa o RTMPose-s em 1×3×{INPUT_ALTURA}×{INPUT_LARGURA}, "
            f"com {NUM_JUNTAS} juntas e bins {BINS_X}/{BINS_Y}. "
            "Eixo espacial dinâmico não dá para conferir na subida, e conferir "
            "no primeiro frame é tarde: a câmera já está no ar"
        )
    return int(valor)


def _exige_tipo_float(tipo: str, contexto: str) -> None:
    if not tipo:
        return
    if tipo in {"tensor(float)", "tensor(float32)"}:
        return
    raise ModelError(
        f"{contexto} é {tipo}; o recorte entrega float32. "
        "float16 ou uint8 aqui não estoura: a normalização do RTMPose é outra "
        "e a junta sai deslocada"
    )


def _como_caixas(caixas: Sequence[Any] | np.ndarray) -> list[Any]:
    # Uma caixa solta (4,) não é uma lista de quatro números. Sem isto, cada
    # coordenada vira uma "caixa" e o recorte estoura com uma mensagem de forma.
    if isinstance(caixas, np.ndarray) and caixas.shape == (4,):
        return [caixas]
    return [_xyxy(caixa) for caixa in caixas]


def _xyxy(caixa: Any) -> Any:
    if isinstance(caixa, np.ndarray):
        return caixa
    if isinstance(caixa, Sequence) and not isinstance(caixa, (str, bytes)):
        return caixa
    # A caixa do detector (`Detection`) não é uma sequência. Recusar aqui
    # obrigaria cada chamador a desmontar x1..y2, e a ordem é exatamente o
    # espaço que o recorte já assume.
    try:
        return (float(caixa.x1), float(caixa.y1), float(caixa.x2), float(caixa.y2))
    except AttributeError as erro:
        raise PreprocessError(
            "esperava uma caixa (x1, y1, x2, y2) ou uma detecção com esses campos"
        ) from erro


def _sem_lote(tensor: np.ndarray, nome: str) -> np.ndarray:
    if tensor.ndim == 3:
        if tensor.shape[0] != 1:
            raise DecodeError(
                f"{nome} tem lote {tensor.shape[0]}; o decode é de uma pessoa por recorte, "
                f"recebi {tensor.shape}"
            )
        tensor = tensor[0]
    if tensor.ndim != 2:
        raise DecodeError(
            f"{nome} deve ter forma (juntas, bins) ou (1, juntas, bins), recebi {tensor.shape}"
        )
    return tensor
