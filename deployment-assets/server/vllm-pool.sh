#!/usr/bin/env bash
# vllm-pool.sh - vLLM 转录池（MOSS-Transcribe on GPU）统一管理入口，跑在部署机上。
#
#   bash vllm-pool.sh status     # 容器状态 + 端口 + 引擎就绪探测
#   bash vllm-pool.sh restart    # 按当前 deploy.env 重建容器
#   bash vllm-pool.sh logs       # 跟随容器日志
#
# 参数来源：同目录 deploy.env（与主栈共用），可选项：
#   VLLM_IMAGE=moss-vllm:stable032          镜像 tag
#   VLLM_GPU_DEVICE=3                       CUDA 设备号
#   VLLM_HTTP_PORT=18110                    宿主机发布端口
#   VLLM_MODEL_DIR=/home/.../MOSS-Transcribe-Diarize
#   VLLM_SERVED_MODEL_NAME=moss-transcribe
#   VLLM_MAX_MODEL_LEN=32768
#   VLLM_GPU_MEM_UTIL=0.85
#   VLLM_MAX_NUM_SEQS=8
#   VLLM_MAX_AUDIO_CLIP_FILESIZE_MB=512
#   VLLM_MAX_AUDIO_DECODE_DURATION_S=14400
#
# 为什么不进 docker-compose：主栈 `docker compose up -d` 会连带重启 GPU 引擎
# （加载 1-2 分钟）拖慢发布验证，且误重启会打断在途转录任务。池的生命周期
# 独立由本脚本管理。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HERE/deploy.env"
NAME="plugin_parser-vllm"

if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

IMAGE="${VLLM_IMAGE:-moss-vllm:stable032}"
GPU="${VLLM_GPU_DEVICE:-3}"
PORT="${VLLM_HTTP_PORT:-18110}"
MODEL_DIR="${VLLM_MODEL_DIR:-/home/goodeep/jzj/models/moss/OpenMOSS-Team/MOSS-Transcribe-Diarize}"
MODELS_ROOT="${VLLM_MODELS_ROOT:-/home/goodeep/jzj/models/moss}"
SERVED_NAME="${VLLM_SERVED_MODEL_NAME:-moss-transcribe}"
MAX_MODEL_LEN="${VLLM_MAX_MODEL_LEN:-32768}"
GPU_MEM_UTIL="${VLLM_GPU_MEM_UTIL:-0.85}"
MAX_NUM_SEQS="${VLLM_MAX_NUM_SEQS:-8}"
MAX_FILE_MB="${VLLM_MAX_AUDIO_CLIP_FILESIZE_MB:-512}"
MAX_DURATION_S="${VLLM_MAX_AUDIO_DECODE_DURATION_S:-14400}"

status() {
  docker inspect "$NAME" --format 'container: {{.State.Status}}  image: {{.Config.Image}}  started: {{.State.StartedAt}}' 2>/dev/null \
    || { echo "container: not found"; exit 1; }
  docker inspect "$NAME" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -E 'VLLM_MAX|FLASHINFER' || true
  if curl -sf -m 5 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
    echo "engine: READY on 127.0.0.1:${PORT} (${SERVED_NAME})"
  else
    echo "engine: NOT READY (warming up or down)"
  fi
}

restart() {
  docker rm -f "$NAME" 2>/dev/null || true
  # 容器内模型路径 = MODELS_ROOT 之后的相对部分挂到 /models/moss 下（与 run-33.sh 一致）
  inner="${MODEL_DIR#"$MODELS_ROOT"}"
  # 基础镜像 ENTRYPOINT 已是 [vllm serve]，只传模型路径与参数。
  # label 必须带 com.docker.compose.project=plugin_parser：宿主机 gpu-guard 仅放行
  # 白名单 compose 项目，裸容器会在建 CUDA 上下文时被 SIGTERM 击杀。
  docker run -d --name "$NAME" \
    --restart unless-stopped \
    --label com.docker.compose.project=plugin_parser \
    --gpus "\"device=${GPU}\"" \
    --cpus 8 --memory 16g --shm-size 4g \
    -p "${PORT}:8000" \
    -e FLASHINFER_DISABLE_VERSION_CHECK=1 \
    -e "VLLM_MAX_AUDIO_CLIP_FILESIZE_MB=${MAX_FILE_MB}" \
    -e "VLLM_MAX_AUDIO_DECODE_DURATION_S=${MAX_DURATION_S}" \
    -v "${MODELS_ROOT}:/models/moss:ro" \
    "$IMAGE" \
    "/models/moss${inner}" \
    --trust-remote-code \
    --served-model-name "$SERVED_NAME" \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization "$GPU_MEM_UTIL" \
    --max-model-len "$MAX_MODEL_LEN" \
    --max-num-seqs "$MAX_NUM_SEQS"
  echo "started $NAME (image=$IMAGE gpu=$GPU port=$PORT); waiting for engine..."
  for _ in $(seq 1 30); do
    if curl -sf -m 3 "http://127.0.0.1:${PORT}/v1/models" >/dev/null 2>&1; then
      echo "engine READY"; return 0
    fi
    sleep 10
  done
  echo "engine not ready after 300s; check: docker logs $NAME" >&2
  exit 1
}

case "${1:-status}" in
  status)  status ;;
  restart) restart ;;
  logs)    docker logs -f --tail 100 "$NAME" ;;
  *)       echo "usage: $0 [status|restart|logs]"; exit 1 ;;
esac
