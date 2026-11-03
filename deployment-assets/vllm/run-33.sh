#!/usr/bin/env bash
# run-33.sh - 在 33 的 GPU3 上启动 moss-vLLM 推理池（批量转录道）。
#
# 隔离原则（压测/运行都不得影响服务器其他服务）：
#   - GPU: CUDA device 3 硬绑（同事的 GPU0/GPU2、moss 的 GPU1 不可见）
#   - 网络: 默认 bridge，不加入 plugin_parser_net
#   - 资源: --cpus 8 / --memory 16g / 显存由 --gpu-memory-utilization 限死
#   - 文件: 模型目录只读挂载
#   - 端口: 18110（宿主） -> 8000（容器）
#
# 用法: ./run-33.sh <image-tag>   默认 moss-vllm:cu129-nightly
set -euo pipefail

TAG="${1:-moss-vllm:cu129-nightly}"
NAME=plugin_parser-vllm

docker rm -f "$NAME" 2>/dev/null || true

# 基础镜像 ENTRYPOINT 已是 [vllm serve]，下面只传模型路径与参数（再写 serve 会报 unrecognized arguments）
# flashinfer 主包（0.7.0.post1，随 vLLM 0.32.0 安装）与基础镜像遗留的 flashinfer-cubin
# （0.7.2.post1）版本号不一致会直接 RuntimeError；flashinfer 官方提供该开关绕过检查。
# label 必须带 com.docker.compose.project=plugin_parser：宿主机 model-platform-gpu-guard
# 每 2s 扫 GPU 进程，仅放行 local-model-workers 与 plugin_parser 两个 compose 项目，
# 裸 docker run 的容器会在引擎建 CUDA 上下文时被 SIGTERM 击杀。
docker run -d --name "$NAME" \
  --restart unless-stopped \
  --label com.docker.compose.project=plugin_parser \
  --gpus '"device=3"' \
  --cpus 8 --memory 16g --shm-size 4g \
  -p 18110:8000 \
  -e FLASHINFER_DISABLE_VERSION_CHECK=1 \
  -e VLLM_MAX_AUDIO_CLIP_FILESIZE_MB=512 \
  -e VLLM_MAX_AUDIO_DECODE_DURATION_S=14400 \
  -v /home/goodeep/jzj/models/moss:/models/moss:ro \
  "$TAG" \
  /models/moss/OpenMOSS-Team/MOSS-Transcribe-Diarize \
    --trust-remote-code \
    --served-model-name moss-transcribe \
    --host 0.0.0.0 --port 8000 \
    --gpu-memory-utilization 0.90 \
    --max-model-len 65536 \
    --max-num-seqs 8

echo "started $NAME on GPU3; logs: docker logs -f $NAME"
