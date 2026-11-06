"""O box da loja (§3.8): as invariantes do compose e do Dockerfile que falham em silêncio.

Nada aqui sobe container — isto roda com `uv run pytest` numa máquina sem Docker, como
o resto da suíte. O que se testa é o que está ESCRITO em `infra/box/` e no Dockerfile,
porque cada uma destas linhas, se sumir, não dá erro: o box sobe, o heartbeat chega
verde, e a loja passa a operar degradada até alguém olhar a métrica certa.

O que estes testes não cobrem, e quem cobre: que a imagem constrói, que o
`onnxruntime-gpu` carrega o provider de CUDA dentro dela e que o ffmpeg dela lista
`cuda` em `-hwaccels`. Isso exige construir a imagem, e é o job `imagem` do CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from lince_agent.config import ClipOptions, SupervisionOptions

RAIZ = Path(__file__).resolve().parents[3]
BOX = RAIZ / "infra" / "box"
DOCKERFILE = RAIZ / "apps" / "agent" / "Dockerfile"


def _carrega(nome: str) -> dict:
    return yaml.safe_load((BOX / nome).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def box() -> dict:
    return _carrega("docker-compose.yml")


@pytest.fixture(scope="module")
def dev() -> dict:
    return _carrega("docker-compose.dev.yml")


@pytest.fixture(scope="module")
def dockerfile() -> str:
    # Junta as continuações de linha: um `ENV A=1 \` seguido de `    B=2` é uma
    # instrução só, e é assim que o Docker a lê.
    return DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")


def _instrucoes(dockerfile: str, nome: str) -> list[str]:
    return [
        linha.strip()[len(nome) :].strip()
        for linha in dockerfile.splitlines()
        if linha.strip().upper().startswith(nome + " ")
    ]


def _montagens(servico: dict) -> list[tuple[str, str, str]]:
    """`origem:destino[:modo]`, com a origem podendo ser `${VAR:-padrão}` — cujo `:`
    interno quebraria um `split(":")` ingênuo, e o teste passaria a ler a montagem
    errada sem falhar."""
    montagens = []
    for texto in servico["volumes"]:
        sem_variaveis = re.sub(r"\$\{[^}]*\}", "VAR", texto)
        origem, destino, *modo = sem_variaveis.split(":")
        montagens.append((origem, destino, modo[0] if modo else "rw"))
    return montagens


def _segundos(duracao: str) -> float:
    """`30s`, `1m30s` — o formato de duração do compose, só no que o box usa."""
    partes = re.fullmatch(r"(?:(\d+)m)?(?:(\d+)s)?", duracao)
    assert partes and any(partes.groups()), f"duração fora do formato esperado: {duracao}"
    minutos, segundos = (int(p or 0) for p in partes.groups())
    return minutos * 60 + segundos


# --- o agente -----------------------------------------------------------------------


def test_agente_usa_a_fila_duravel_e_a_configuracao_da_nuvem(box):
    """`--outbox memory` é o padrão do agente e é andaime. No box, um reinício — e toda
    atualização de imagem é um reinício — jogaria fora cada evento que esperava a
    internet voltar (NFR-8). E sem `--config-nuvem` a loja seria configurada por um
    arquivo que o dashboard não enxerga e o poll de 30 s nunca atualiza (§5.2)."""
    comando = box["services"]["agente"]["command"]

    assert "--config-nuvem" in comando
    assert comando[comando.index("--outbox") + 1] == "redis"
    assert "--config" not in comando and "--camera" not in comando


def test_clipes_ficam_num_volume_que_sobrevive_ao_reinicio(box):
    """A fila do Redis guarda o caminho do clipe que ainda não subiu. Se os clipes
    morassem no sistema de arquivos do container, a primeira atualização deixaria na
    fila eventos apontando para arquivos que não existem: o evento sobe, o upload
    falha para sempre, e a triagem fica esperando um vídeo que já era (§3.6)."""
    agente = box["services"]["agente"]
    comando = agente["command"]
    clipes = comando[comando.index("--clips-dir") + 1]

    volumes_nomeados = set(box["volumes"])
    destinos_persistentes = [d for o, d, _ in _montagens(agente) if o in volumes_nomeados]

    assert any(clipes == d or clipes.startswith(d + "/") for d in destinos_persistentes), (
        f"--clips-dir {clipes} não está dentro de nenhum volume nomeado"
    )


def test_modelo_vem_de_uma_montagem_so_leitura_no_caminho_que_o_agente_le(box):
    """`LINCE_MODEL_PATH` apontando para fora da montagem não para o box: o agente sobe
    sem modelo, ingere e corta clipe, e não detecta ninguém — uma loja inteira sem
    alerta nenhum e com heartbeat verde. E a montagem é `:ro` porque o modelo é o
    único arquivo que o box não consegue refazer sem internet (ADR-006)."""
    agente = box["services"]["agente"]
    modelo = agente["environment"]["LINCE_MODEL_PATH"]

    montagens_ro = [destino for _, destino, modo in _montagens(agente) if modo == "ro"]
    assert any(modelo.startswith(destino + "/") for destino in montagens_ro), (
        f"LINCE_MODEL_PATH={modelo} não está dentro de uma montagem só leitura"
    )


def test_gpu_reservada_com_a_capacidade_de_video(box):
    """`video` é o que faz o NVIDIA Container Toolkit montar a `libnvcuvid.so` — o
    NVDEC. Sem ela a inferência continua na GPU, o `-hwaccel cuda` falha, e o §3.1
    cai para decode em CPU como foi desenhado para cair: N streams decodificados na
    CPU, sem um erro em lugar nenhum (R-4)."""
    dispositivos = box["services"]["agente"]["deploy"]["resources"]["reservations"]["devices"]
    nvidia = [d for d in dispositivos if d.get("driver") == "nvidia"]

    assert nvidia, "o agente não reserva GPU NVIDIA nenhuma"
    assert {"gpu", "video"} <= set(nvidia[0]["capabilities"])


def test_imagem_do_agente_tem_versao_fixa_e_obrigatoria(box):
    """`latest` faria cada loja rodar a versão do dia em que reiniciou. O R-7 pede o
    contrário: saber exatamente o que roda em cada box e subir uma loja piloto antes
    das outras. A versão vem do `.env` do box e é obrigatória (`:?`)."""
    imagem = box["services"]["agente"]["image"]

    assert "latest" not in imagem
    assert re.search(r":\$\{LINCE_VERSAO:\?[^}]*\}$", imagem), (
        f"a tag da imagem não é a versão obrigatória do .env: {imagem}"
    )


CAMERAS_POR_LOJA_MAX = 20
"""O teto do requisito não funcional de escala: 10 a 20 câmeras por cliente."""


def test_init_recolhe_os_ffmpeg(box):
    """O agente abre um ffmpeg por câmera e reabre a cada reconexão (§3.1). Como PID 1
    sem `init`, os órfãos viram zumbi, e uma câmera instável acumula centenas por dia
    até a tabela de processos do box acabar."""
    assert box["services"]["agente"]["init"] is True


def test_encerramento_cabe_no_pior_caso_de_uma_loja_cheia(box):
    """`docker compose stop` (e toda atualização) manda SIGTERM e, vencida a folga,
    SIGKILL. O agente para as câmeras uma de cada vez, até `stop_grace_s` cada, e
    depois espera o pós-roll do clipe em corte. Se a folga for menor que isso, o SIGKILL
    chega no meio e o clipe do evento que acabou de acontecer se perde (§3.5) — numa
    loja cheia, que é onde há mais eventos."""
    pior_caso = (
        SupervisionOptions().stop_grace_s * CAMERAS_POR_LOJA_MAX
        + ClipOptions().post_roll_s
        + ClipOptions().post_roll_grace_s
    )

    assert _segundos(box["services"]["agente"]["stop_grace_period"]) >= pior_caso


# --- a fila -------------------------------------------------------------------------


def test_redis_do_box_persiste_a_fila_com_aof(box):
    """ADR-004: a fila sobrevive a reinício porque o Redis grava AOF com fsync a cada
    segundo. Sem `--appendonly yes`, o Redis padrão guarda a fila só em memória e um
    reinício do box apaga todo evento que esperava a internet voltar."""
    comando = box["services"]["redis"]["command"]

    assert comando[comando.index("--appendonly") + 1] == "yes"
    assert comando[comando.index("--appendfsync") + 1] == "everysec"
    assert any(v.endswith(":/data") for v in box["services"]["redis"]["volumes"])


def test_redis_do_box_nao_e_publicado_na_rede_da_loja(box):
    """A fila guarda o payload dos eventos (§3.6), e a rede da loja é a mesma do Wi-Fi
    dos clientes e do DVR com senha padrão. Publicar a porta expõe a fila para todos
    eles; o agente a alcança pela rede interna do compose."""
    assert "ports" not in box["services"]["redis"]


def test_override_de_dev_prende_o_redis_no_loopback_fora_da_porta_de_desenvolvimento(dev):
    """Com o agente na rede do host, o Redis do box precisa ser publicado — mas só em
    127.0.0.1, e não na 6379, onde mora o Redis do control plane. Na mesma porta, os
    testes `-m redis` do agente (que limpam o que criam) mexeriam na fila do box."""
    portas = dev["services"]["redis"]["ports"]

    assert portas, "sem porta publicada o agente em rede do host não alcança a fila"
    for porta in portas:
        host, porta_host, _ = porta.split(":")
        assert host == "127.0.0.1"
        assert porta_host != "6379"


def test_override_de_dev_aponta_o_agente_para_a_porta_que_publicou(dev):
    """As duas metades da decisão anterior têm que andar juntas: mudar a porta publicada
    sem mudar `LINCE_REDIS_URL` deixa o agente batendo no Redis de desenvolvimento —
    que responde, aceita a fila, e mistura as duas sem erro nenhum."""
    _, porta_host, _ = dev["services"]["redis"]["ports"][0].split(":")
    url = dev["services"]["agente"]["environment"]["LINCE_REDIS_URL"]

    assert url.startswith(f"redis://127.0.0.1:{porta_host}/")


# --- a imagem -----------------------------------------------------------------------


def test_imagem_declara_a_capacidade_de_video_por_conta_propria(dockerfile):
    """A imagem base declara `compute,utility`, sem `video`. A trava do compose não
    vale num `docker run --gpus all` de diagnóstico, que é justamente o comando de quem
    está investigando por que o decode está em CPU (R-4). Por isso a imagem declara
    sozinha."""
    envs = " ".join(_instrucoes(dockerfile, "ENV"))
    valor = re.search(r"NVIDIA_DRIVER_CAPABILITIES=(\S+)", envs)

    assert valor, "NVIDIA_DRIVER_CAPABILITIES não está declarado na imagem"
    assert {"compute", "utility", "video"} <= set(valor.group(1).split(","))


def test_toda_sincronizacao_da_imagem_usa_o_grupo_gpu_e_nunca_o_padrao(dockerfile):
    """O grupo padrão do uv é `cpu` (pyproject.toml). Um `uv sync` sem
    `--no-default-groups` instalaria `onnxruntime` e `onnxruntime-gpu` juntos — os dois
    escrevem o mesmo módulo, o segundo sobrescreve o primeiro, e o que sobra importa
    sem erro. Um `uv run` faz a mesma coisa, porque sincroniza antes de rodar."""
    comandos = _instrucoes(dockerfile, "RUN")
    sincronizacoes = [c for c in comandos if "uv sync" in c]

    assert sincronizacoes, "a imagem não instala dependência nenhuma pelo uv"
    for comando in sincronizacoes:
        assert "--no-default-groups" in comando, comando
        assert "--group gpu" in comando, comando
        assert "--frozen" in comando, comando
    assert not [c for c in comandos if "uv run" in c]
