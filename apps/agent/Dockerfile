# syntax=docker/dockerfile:1.7
#
# Imagem do agente da borda (§3, §3.8): o mesmo artefato no box da loja e em qualquer
# máquina com GPU NVIDIA (NFR-5). O contexto do build é a RAIZ do repositório, porque o
# estágio `teste` precisa de `packages/shared/schemas` — o contrato do §5 mora em um
# lugar só e o agente valida contra ele:
#
#   docker build -f apps/agent/Dockerfile -t lince-agent .                 # imagem do box
#   docker build -f apps/agent/Dockerfile --target teste .                 # roda a suíte
#
# Estágios:
#   base    → CUDA 13 + cuDNN 9 + ffmpeg + Python 3.12, sem nada do projeto
#   deps    → o venv com o grupo `gpu` (onnxruntime-gpu) e o agente instalado nele
#   teste   → deps + grupo `dev` + testes; o build falha se a suíte falhar
#   agente  → base + o venv, e só. É a imagem que vai para a loja
#
# O que esta imagem NÃO carrega: o modelo `.onnx` (ADR-006 — a nuvem distribui, com
# checksum; aqui ele entra por volume, ver infra/box/) e nenhuma credencial.

# CUDA 13 porque é contra ele que o `onnxruntime-gpu` >= 1.27 do PyPI é compilado
# (`libcublas.so.13`, `libcudart.so.13`). Imagem de CUDA 12 com ele instalado não dá
# erro nenhum: o provider de CUDA não carrega e a sessão roda em CPU, o que a §10.10
# mediu como ~2 fps para UMA câmera. O job `imagem` do CI confere com `ldd`.
#
# CUDA 13 exige driver NVIDIA >= 580 no HOST. A imagem não tem como verificar isso no
# build; docs/box.md põe a versão do driver no checklist de instalação.
FROM nvidia/cuda:13.4.3-cudnn-runtime-ubuntu24.04 AS base

# NVIDIA_DRIVER_CAPABILITIES é a armadilha mais silenciosa desta imagem. A imagem base
# declara `compute,utility`, e com isso o NVIDIA Container Toolkit monta o CUDA mas NÃO
# monta a `libnvcuvid.so`, que é o NVDEC. A inferência roda na GPU, o `-hwaccel cuda`
# do ffmpeg falha, o §3.1 cai para decode em CPU como foi desenhado para cair — e a
# loja passa a decodificar N streams na CPU sem erro em lugar nenhum (R-4). `video` é o
# que traz o NVDEC. Fica na imagem, e não só no compose, para valer em qualquer
# `docker run`.
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility,video \
    DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ffmpeg do Ubuntu 24.04 (6.1), não um estático baixado: é o mesmo que o CI instala com
# apt, então os testes de ingestão que passam no CI passam contra o MESMO binário que
# roda na loja. Ele é compilado com NVDEC (`-hwaccels` lista `cuda`), conferido no CI.
#
# python3.12 é o do sistema e não um baixado pelo uv: o Ubuntu 24.04 já o traz, e um
# segundo Python na imagem é um segundo Python para manter atualizado.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3.12 \
    && rm -rf /var/lib/apt/lists/*


FROM base AS deps

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

# O venv fica FORA do diretório do código, num caminho que o estágio final copia
# inteiro. `--no-editable` instala o agente como pacote dentro do venv: a imagem final
# não precisa de `src/` nenhum, só do venv.
ENV UV_PROJECT_ENVIRONMENT=/opt/lince/venv \
    UV_PYTHON=/usr/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /src/apps/agent

# Dependências antes do código: o `onnxruntime-gpu` tem ~250 MB, e com esta ordem uma
# mudança em `src/` não baixa ele de novo.
#
# `--no-default-groups --group gpu` e nunca o `uv sync` puro: o padrão inclui o grupo
# `cpu`, que conflita com `gpu` (pyproject.toml). `--frozen` porque a imagem da loja
# tem que ser exatamente o que o uv.lock diz — resolver de novo no build traria uma
# versão que nenhum teste viu.
COPY apps/agent/pyproject.toml apps/agent/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --group gpu --no-install-project

COPY apps/agent/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --group gpu --no-editable


# A suíte roda AQUI, e não só no runner do CI, por um motivo: neste estágio o ffmpeg é
# o da imagem e o ONNX Runtime é o `onnxruntime-gpu` — o mesmo pacote que vai para a
# loja, não o `onnxruntime` de CPU do ambiente de desenvolvimento. É o mesmo `uv run
# pytest` do CLAUDE.md, contra o que de fato roda no box.
FROM deps AS teste

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-default-groups --group gpu --group dev

COPY packages/shared/schemas /src/packages/shared/schemas
COPY infra/box/docker-compose.yml infra/box/docker-compose.dev.yml /src/infra/box/
COPY apps/agent/Dockerfile ./Dockerfile
COPY apps/agent/tests ./tests

# Pelo binário do venv e não por `uv run`: o `uv run` sincroniza com os grupos padrão
# antes de rodar, e o padrão inclui `cpu` — trocaria o pacote de GPU pelo de CPU
# justamente no estágio que existe para testar o de GPU.
RUN /opt/lince/venv/bin/pytest -q -p no:cacheprovider


FROM base AS agente

LABEL org.opencontainers.image.source="https://github.com/AlexandrePachecoo/lince" \
      org.opencontainers.image.description="lince — agente da borda (§3)"

COPY --from=deps /opt/lince/venv /opt/lince/venv
ENV PATH=/opt/lince/venv/bin:$PATH

# Onde o box guarda o que tem que sobreviver a um reinício: os clipes à espera de
# upload e o cache da última configuração válida (§5.4). O compose monta um volume
# aqui; sem volume, um `docker compose up` depois de uma atualização deixaria na fila
# do Redis eventos apontando para clipes que não existem mais.
WORKDIR /var/lib/lince

# Roda como root, de propósito e já assumido no CLAUDE.md (regra 7 dos testes): o
# acesso à GPU pelo NVIDIA Container Toolkit e a escrita no volume não dependem de uid,
# e um usuário próprio trocaria um problema que não existe hoje por permissão de volume
# para acertar em cada box.
ENTRYPOINT ["lince-agent"]
CMD ["--help"]
