#!/usr/bin/env bash
# Start the local Ollama server in the background if it's installed and not already running.
# Runs automatically each time the Codespace starts (see .devcontainer/devcontainer.json).
command -v ollama >/dev/null 2>&1 || exit 0
curl -s localhost:11434/api/tags >/dev/null 2>&1 && exit 0
# 8k context so the coach's instructions, tools and your data all fit.
OLLAMA_CONTEXT_LENGTH=8192 nohup ollama serve > /tmp/ollama.log 2>&1 &
for _ in $(seq 1 30); do
  curl -s localhost:11434/api/tags >/dev/null 2>&1 && exit 0
  sleep 1
done
echo "Ollama didn't start; see /tmp/ollama.log" >&2
exit 1
