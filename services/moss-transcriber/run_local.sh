#!/bin/zsh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="${MEETILY_FUNASR_VENV:-$HOME/Desktop/Meetily/frontend/funasr-sidecar/.venv}"
export MEETILY_FUNASR_MODEL_DIR="${MEETILY_FUNASR_MODEL_DIR:-$HOME/Library/Application Support/com.meetily.ai/funasr-models}"
export MOSS_MODEL_PATH="${MOSS_MODEL_PATH:-$MEETILY_FUNASR_MODEL_DIR/OpenMOSS-Team/MOSS-Transcribe-Diarize}"
export MOSS_HOST="${MOSS_HOST:-127.0.0.1}"
export MOSS_PORT="${MOSS_PORT:-9000}"
export MOSS_PRELOAD="${MOSS_PRELOAD:-1}"
export SUMMARY_MODEL_PATH="${SUMMARY_MODEL_PATH:-$HOME/Library/Application Support/com.meetily.ai/models/summary/Qwen3.5-4B-Q4_K_M.gguf}"
export LLAMA_CLI="${LLAMA_CLI:-llama-cli}"
cd "$ROOT/services/moss-transcriber"
echo "MOSS 监听 $MOSS_HOST:$MOSS_PORT ，启动后会在后台预热模型"
exec "$VENV/bin/python" -m uvicorn main:app --host "$MOSS_HOST" --port "$MOSS_PORT"
