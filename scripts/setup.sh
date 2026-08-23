#!/usr/bin/env bash
#
# Bootstrap do ambiente de desenvolvimento do lince.
# Idempotente: pode rodar quantas vezes quiser. Roda automaticamente no
# postCreateCommand do devcontainer e pode ser chamado à mão a qualquer momento.
#
set -euo pipefail

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
skip() { printf '\033[1;32m  ok\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m  !!\033[0m %s\n' "$1"; }

cd "$(dirname "$0")/.."

# --- ffmpeg -----------------------------------------------------------------
# Estágio 1 (decode do substream RTSP) e estágio 5 (corte do clipe com -c copy).
if command -v ffmpeg >/dev/null 2>&1; then
  skip "ffmpeg já instalado ($(ffmpeg -version | head -1 | cut -d' ' -f3))"
else
  info "instalando ffmpeg"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq --no-install-recommends ffmpeg
  elif command -v brew >/dev/null 2>&1; then
    brew install ffmpeg
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y ffmpeg
  else
    warn "não sei instalar ffmpeg neste sistema. Instale à mão e rode de novo."
    warn "  macOS: brew install ffmpeg | Windows: use WSL2 (ver docs/setup.md)"
    exit 1
  fi
fi

# --- uv ---------------------------------------------------------------------
# Gerenciador de dependências e ambientes Python do apps/agent.
if command -v uv >/dev/null 2>&1; then
  skip "uv já instalado ($(uv --version))"
else
  info "instalando uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

# --- dependências do workspace ----------------------------------------------
# Só faz sentido depois que algum app tiver package.json.
if command -v pnpm >/dev/null 2>&1; then
  if compgen -G "apps/*/package.json" >/dev/null || compgen -G "packages/*/package.json" >/dev/null; then
    info "instalando dependências do workspace (pnpm)"
    pnpm install
  else
    skip "nenhum pacote no workspace ainda — pulando pnpm install"
  fi
else
  warn "pnpm não encontrado. Instale com: corepack enable pnpm"
fi

# --- .env -------------------------------------------------------------------
if [ ! -f infra/.env ] && [ -f infra/.env.example ]; then
  info "criando infra/.env a partir do exemplo"
  cp infra/.env.example infra/.env
  warn "troque POSTGRES_PASSWORD e MINIO_ROOT_PASSWORD em infra/.env antes de subir a infra"
else
  skip "infra/.env já existe"
fi

echo
info "pronto. próximo passo:  pnpm infra:up"
