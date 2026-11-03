# moss-vllm: MOSS-Transcribe-Diarize 的 vLLM 推理池镜像（批量转录道）。
#
# 官方 README 要求 pinned nightly 构建（含 MOSS 模型注册）：
#   https://wheels.vllm.ai/68b4a1d582818e67adc903bf1b8fc5a5447da2fa/cu129  (CUDA 12)
# 基于官方 vllm/vllm-openai 基础镜像，仅替换 vLLM 为该 pinned 版本。
#
# 构建（在 33 上）：
#   docker build -t moss-vllm:cu129-nightly-<date> .
# 运行（GPU3，独立 bridge 网络，不进 plugin_parser_net）：
#   见 run-33.sh
FROM vllm/vllm-openai:latest

RUN (command -v uv >/dev/null 2>&1 || pip install --no-cache-dir -q uv) \
 && uv pip install -U --system vllm \
      --torch-backend=auto \
      --extra-index-url https://wheels.vllm.ai/68b4a1d582818e67adc903bf1b8fc5a5447da2fa/cu129

# 回退稳定版 0.32.0：pinned nightly（0.33.0-dev）的进程管理有 bug——
# EngineCore 启动中 consistently 收到神秘 SIGTERM 后被杀（Engine core initialization failed），
# enforce-eager / V2 runner 开关均无效。稳定版 0.32.0 已内置 MOSS 架构注册
# （registry.py: MossTranscribeDiarizeForConditionalGeneration），README 的「必须 nightly」已过时。
# --torch-backend=cu130 与基础镜像原生 torch 2.11.0+cu130 组合对齐，torchaudio 一并匹配。
RUN uv pip install --system vllm==0.32.0 torchaudio==2.11.0+cu130 \
      --torch-backend=cu130 \
      --extra-index-url https://wheels.vllm.ai/0.32.0/cu130
