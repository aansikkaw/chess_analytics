#!/usr/bin/env bash
# One-time setup for the free local AI coach: installs Ollama, starts it, downloads a model.
# Usage:  bash scripts/setup_ollama.sh            (default model: qwen2.5:3b, ~2 GB)
#         OLLAMA_MODEL=llama3.2:3b bash scripts/setup_ollama.sh
set -euo pipefail
MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v ollama >/dev/null 2>&1; then
  echo "==> Installing Ollama"
  sudo apt-get update -qq && sudo apt-get install -y -qq zstd curl >/dev/null
  curl -fsSL https://ollama.com/install.sh | sh
fi

echo "==> Starting Ollama"
bash "$HERE/start_ollama.sh"

echo "==> Downloading $MODEL (first time only; a few minutes)"
ollama pull "$MODEL"

echo "==> Test run"
ollama run "$MODEL" "Reply with the single word: ready"

echo
echo "Done. Restart the app (Ctrl+C in its terminal, then run uvicorn again)."
echo "The header should say: Coach Ollama (local) · $MODEL"
