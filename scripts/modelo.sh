#!/usr/bin/env bash
#
# Baixa os ativos do estágio 2 (detecção, §3.2): o modelo e o vídeo de teste.
#
# Nenhum dos dois entra no git. O modelo porque a nuvem é quem o distribui, com
# checksum e rollback (ADR-006) — um .onnx versionado aqui seria uma segunda fonte da
# verdade. O vídeo porque o .gitignore recusa mídia por princípio (R-9), e o hábito de
# abrir exceção "só para o teste" é exatamente o que não se quer num projeto que lida
# com imagem de pessoa.
#
# Idempotente: com o arquivo já no lugar e o checksum batendo, não baixa de novo.
#
#   bash scripts/modelo.sh
#
set -euo pipefail

info() { printf '\033[1;34m==>\033[0m %s\n' "$1"; }
skip() { printf '\033[1;32m  ok\033[0m %s\n' "$1"; }
fail() { printf '\033[1;31m  !!\033[0m %s\n' "$1" >&2; exit 1; }

cd "$(dirname "$0")/.."

# YOLOX-s, exportado para ONNX pelos próprios autores. Apache-2.0 — e a licença é
# metade do motivo da escolha: a linhagem Ultralytics é AGPL-3.0, o que para um SaaS
# comercial exigiria licença enterprise ou abrir o código (ADR-009). A licença do
# runtime não é a licença dos pesos, e trocar o modelo por um AGPL desfaz o ponto.
MODELO_URL="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx"
MODELO_SHA="c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MODELO_DEST="apps/agent/models/yolox_s.onnx"

# As duas variantes pequenas, para o box sem placa de vídeo (ADR-012, §3.2). Mesma
# família, mesma licença e mesma convenção de entrada do YOLOX-s; o que muda é a
# entrada de 416 — por isso o agente sobe com `--input-size 416` para elas.
#
# Medido no vídeo de teste a 3 fps, numa CPU Xeon de 4 núcleos, contra o YOLOX-s:
#   tiny  ~27 ms/frame, acha 93% das pessoas que o s acha
#   nano  ~11 ms/frame, acha 78%
#   s     ~71 ms/frame
# O tiny é o padrão do box em CPU; o nano é a reserva para máquina muito fraca.
TINY_URL="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx"
TINY_SHA="427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7"
TINY_DEST="apps/agent/models/yolox_tiny.onnx"
NANO_URL="https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx"
NANO_SHA="c789161ed43c8269fcd4e67c67eeeb4e80c622da2eb296a20bc6007bd18a0b7d"
NANO_DEST="apps/agent/models/yolox_nano.onnx"

# Vídeo com pessoas andando e cruzando o quadro. `testsrc2` das cam1 e cam2 é padrão
# de barras: o detector não tem o que achar ali, e um teste de detecção contra barras
# de cor não significa nada. É este vídeo que a cam3 publica, e é ele que o tracking
# (§3.3) e o motor de regras (§3.4) vão precisar depois — os dois querem movimento.
# Pelo host de conteúdo, e não por `github.com/.../raw/...`: o segundo é só um
# redirecionamento para o primeiro, e há proxy de rede que recusa o redirecionamento.
VIDEO_URL="https://raw.githubusercontent.com/intel-iot-devkit/sample-videos/master/people-detection.mp4"
VIDEO_SHA="18ffe8672d741e3e29c9d891d22c59d453720b086c25b35c88b393d55f92f693"
VIDEO_DEST="infra/videos/pessoas.mp4"

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1   # macOS
  fi
}

baixa() {
  local url="$1" destino="$2" esperado="$3" nome="$4"

  if [ -f "$destino" ] && [ "$(sha256 "$destino")" = "$esperado" ]; then
    skip "$nome já está em $destino"
    return
  fi

  info "baixando $nome"
  mkdir -p "$(dirname "$destino")"
  # Para um arquivo temporário: interromper o download no meio deixaria um destino
  # truncado que o próximo `is_file()` aceitaria como válido.
  local parcial="$destino.parcial"
  curl -fL --progress-bar -o "$parcial" "$url" || fail "download de $nome falhou"

  local obtido
  obtido="$(sha256 "$parcial")"
  if [ "$obtido" != "$esperado" ]; then
    rm -f "$parcial"
    fail "checksum de $nome não bate (esperava $esperado, veio $obtido)"
  fi
  mv "$parcial" "$destino"
  skip "$nome em $destino"
}

baixa "$MODELO_URL" "$MODELO_DEST" "$MODELO_SHA" "modelo YOLOX-s"
baixa "$TINY_URL" "$TINY_DEST" "$TINY_SHA" "modelo YOLOX-tiny"
baixa "$NANO_URL" "$NANO_DEST" "$NANO_SHA" "modelo YOLOX-nano"
baixa "$VIDEO_URL" "$VIDEO_DEST" "$VIDEO_SHA" "vídeo de teste"

cat <<'FIM'

Pronto. Agora dá para:

  cd apps/agent && uv run pytest -m modelo          # o modelo de verdade
  pnpm rtsp:up && uv run pytest -m "rtsp and modelo"  # ponta a ponta, com pessoas

  uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 \
    --model models/yolox_s.onnx --stats

  # o box sem placa: modelo pequeno, entrada de 416
  uv run python -m lince_agent --camera rtsp://localhost:8554/cam3 \
    --model models/yolox_tiny.onnx --input-size 416 --stats
FIM
