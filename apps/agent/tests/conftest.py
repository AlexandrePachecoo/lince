from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


def _ffmpeg() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        pytest.skip("ffmpeg não encontrado no PATH")
    return binary


@pytest.fixture(scope="session")
def fmp4_stream() -> bytes:
    """Bytes fMP4 reais, produzidos com as mesmas flags da ingestão.

    Testar o parser contra bytes escritos à mão testaria a nossa leitura da
    especificação; testar contra a saída do ffmpeg testa o que vai chegar no pipe.
    `-g 10` em 50 frames dá 5 fragmentos.
    """
    result = subprocess.run(  # noqa: S603
        [
            *(_ffmpeg(), "-hide_banner", "-loglevel", "error"),
            *("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10"),
            *("-frames:v", "50", "-c:v", "libx264", "-preset", "ultrafast"),
            *("-g", "10", "-pix_fmt", "yuv420p", "-an"),
            *("-movflags", "+frag_keyframe+empty_moov+default_base_moof"),
            *("-f", "mp4", "pipe:1"),
        ],
        capture_output=True,
        timeout=60,
        check=True,
    )
    assert result.stdout, "ffmpeg não produziu bytes"
    return result.stdout


@pytest.fixture(scope="session")
def fmp4_sessao_longa() -> bytes:
    """40 s de fMP4 real, um fragmento por segundo.

    O `fmp4_stream` tem 5 s — não cabe uma janela de 30 s com pré e pós-roll. Aqui
    `-g 10` a 10 fps dá 40 fragmentos de 1 s, que é resolução suficiente para
    exercitar poda, seleção de janela e teto de bytes com bytes de verdade em vez de
    fragmento inventado. A 64x64 o ffmpeg gera isto em menos de um segundo.
    """
    result = subprocess.run(  # noqa: S603
        [
            *(_ffmpeg(), "-hide_banner", "-loglevel", "error"),
            *("-f", "lavfi", "-i", "testsrc2=size=64x64:rate=10"),
            *("-frames:v", "400", "-c:v", "libx264", "-preset", "ultrafast"),
            *("-tune", "zerolatency", "-g", "10", "-pix_fmt", "yuv420p", "-an"),
            *("-movflags", "+frag_keyframe+empty_moov+default_base_moof"),
            *("-f", "mp4", "pipe:1"),
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    assert result.stdout, "ffmpeg não produziu bytes"
    return result.stdout


@pytest.fixture(scope="session")
def fmp4_outra_resolucao() -> bytes:
    """Uma segunda execução do ffmpeg, com `moov` incompatível com o das outras.

    A resolução diferente é o que torna a incompatibilidade observável: concatenar
    este init com fragmentos de 64x64 (ou o inverso) produz mídia que o decoder não
    consegue interpretar. Serve para medir o que acontece de fato quando duas
    sessões se misturam — que é o desastre que o `session_id` existe para evitar.
    """
    result = subprocess.run(  # noqa: S603
        [
            *(_ffmpeg(), "-hide_banner", "-loglevel", "error"),
            *("-f", "lavfi", "-i", "testsrc2=size=96x96:rate=10"),
            *("-frames:v", "100", "-c:v", "libx264", "-preset", "ultrafast"),
            *("-tune", "zerolatency", "-g", "10", "-pix_fmt", "yuv420p", "-an"),
            *("-movflags", "+frag_keyframe+empty_moov+default_base_moof"),
            *("-f", "mp4", "pipe:1"),
        ],
        capture_output=True,
        timeout=120,
        check=True,
    )
    assert result.stdout, "ffmpeg não produziu bytes"
    return result.stdout


@pytest.fixture(scope="session")
def redis_url() -> str:
    """URL de um Redis de verdade, ou `skip`.

    Aponta para o banco 15 do Redis de desenvolvimento por padrão — separado do 0, que
    é onde o control plane guarda as filas do BullMQ. Os testes ainda assim usam
    prefixo próprio e nunca `FLUSHDB`: apagar o banco inteiro de quem está com a
    stack de dev no ar é o tipo de gentileza que ninguém esquece.
    """
    url = os.environ.get("LINCE_REDIS_URL", "redis://localhost:6379/15")
    redis = pytest.importorskip("redis", reason="pacote redis não instalado")
    cliente = redis.Redis.from_url(url, socket_timeout=2.0, socket_connect_timeout=2.0)
    try:
        cliente.ping()
    except redis.exceptions.RedisError as erro:
        pytest.skip(f"sem Redis em {url} ({erro}) — rode `pnpm infra:up`")
    finally:
        cliente.close()
    return url


@pytest.fixture(scope="session")
def model_path() -> Path:
    """Caminho do `.onnx` de verdade, ou `skip`.

    O modelo é a única coisa do estágio 2 que não cabe numa máquina limpa: são dezenas
    de megabytes que não vão para o git. Todo o resto do estágio — letterbox,
    decodificação, NMS, fila, thread e contagem — roda contra dublês e sem marcador.
    """
    caminho = Path(os.environ.get("LINCE_MODEL_PATH", "models/yolox_s.onnx"))
    if not caminho.is_file():
        pytest.skip(f"sem modelo em {caminho} — rode `bash scripts/modelo.sh`")
    return caminho


@pytest.fixture(scope="session")
def rtsp_url() -> str:
    url = os.environ.get("RTSP_TEST_URL", "rtsp://localhost:8554/cam1")
    probe = shutil.which("ffprobe")
    if probe is None:
        pytest.skip("ffprobe não encontrado no PATH")
    check = subprocess.run(  # noqa: S603
        [probe, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-i", url],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if check.returncode != 0:
        pytest.skip(f"sem servidor RTSP em {url} — rode `pnpm rtsp:up`")
    return url


SCHEMAS = Path(__file__).resolve().parents[3] / "packages" / "shared" / "schemas"
"""`packages/shared/schemas`, a fonte única do contrato do §5.

O agente lê os schemas do repositório, e não uma cópia dentro de `apps/agent`. Uma
cópia é exatamente a duplicação que o contrato existe para evitar: ela fica correta
até alguém alterar o original, e o sintoma aparece numa loja, como evento recusado.
"""


@pytest.fixture(scope="session")
def registry():
    """Os schemas do repositório num registry, para os `$ref` entre eles resolverem
    sem rede. Sem isto, validar `event.v1.json` tentaria buscar `common.v1.json` em
    `https://schemas.lince.dev` — que não existe, e o teste passaria a depender de DNS.
    """
    import json

    from referencing import Registry, Resource

    recursos = []
    for arquivo in sorted(SCHEMAS.glob("*.json")):
        conteudo = json.loads(arquivo.read_text(encoding="utf-8"))
        recursos.append((conteudo["$id"], Resource.from_contents(conteudo)))
    assert recursos, f"nenhum schema em {SCHEMAS}"
    return Registry().with_resources(recursos)


@pytest.fixture(scope="session")
def validador(registry):
    """Fábrica: `validador("event.v1.json")` devolve o validador daquele schema."""
    import json

    from jsonschema import Draft202012Validator

    def cria(nome: str) -> Draft202012Validator:
        schema = json.loads((SCHEMAS / nome).read_text(encoding="utf-8"))
        return Draft202012Validator(schema, registry=registry)

    return cria
