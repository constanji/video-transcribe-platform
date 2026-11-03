#!/usr/bin/env bash
# debug-run.sh - 前台 DEBUG 跑 vLLM 抓引擎子进程真实报错（保留容器以取退出码）
set -u
docker rm -f vllm-debug >/dev/null 2>&1
docker run --name vllm-debug \
  --gpus '"device=3"' \
  --cpus 8 --memory 16g --shm-size 4g \
  -e VLLM_LOGGING_LEVEL=DEBUG \
  -e VLLM_USE_V2_MODEL_RUNNER=0 \
  -v /home/goodeep/jzj/models/moss:/models/moss:ro \
  moss-vllm:cu129-nightly \
  /models/moss/OpenMOSS-Team/MOSS-Transcribe-Diarize \
    --trust-remote-code \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization 0.85 \
    --max-model-len 32768 \
    --max-num-seqs 8 \
    --enforce-eager
