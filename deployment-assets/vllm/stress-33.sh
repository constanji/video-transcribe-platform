#!/usr/bin/env bash
# stress-33.sh - moss-vLLM 并发压测（只打 vLLM 端口 18110，不碰 moss 18102）。
#
# 前置: /tmp/ab_test.wav 已就位（354s 会议音频，16k mono wav）
#       plugin_parser-vllm 容器已启动并完成模型加载
#
# 用法: ./stress-33.sh smoke        单请求冒烟
#       ./stress-33.sh x3           3 路并发压测
#       ./stress-33.sh baseline     输出服务器基线（压测前后各跑一次对比）
set -euo pipefail

URL=http://127.0.0.1:18110/v1/audio/transcriptions
WAV=/tmp/ab_test.wav

baseline() {
  echo "=== $(date +%F' '%T) ==="
  nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader
  uptime
  docker ps --format '{{.Names}}\t{{.Status}}' | grep plugin_parser
}

smoke() {
  echo "--- smoke: 1 request (verbose_json) ---"
  curl -s -w '\nwall_time=%{time_total}s http=%{http_code}\n' \
    -X POST "$URL" \
    -F model=moss-transcribe \
    -F file=@"$WAV" \
    -F response_format=verbose_json \
    -F temperature=0 \
    -o /tmp/vllm_smoke.json
  python3 - <<'EOF'
import json
try:
    d = json.load(open('/tmp/vllm_smoke.json'))
    segs = d.get('segments') or []
    print('segments:', len(segs))
    print('text head:', (d.get('text') or '')[:200])
except Exception as e:
    print('parse fail:', e)
    print(open('/tmp/vllm_smoke.json').read()[:400])
EOF
}

x3() {
  echo "--- stress: 3 concurrent requests ---"
  for i in 1 2 3; do
    ( /usr/bin/time -f "req$i wall=%es" curl -s -X POST "$URL" \
        -F model=moss-transcribe -F file=@"$WAV" \
        -F response_format=json -F temperature=0 \
        -o /tmp/vllm_stress_$i.json ) 2>&1 &
  done
  # 压测期间采样 GPU3 利用率（只看 GPU3）
  for t in 1 2 3 4 5 6; do
    sleep 5
    nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader -i 3
  done
  wait
  echo "--- results ---"
  for i in 1 2 3; do
    echo "req$i: $(wc -c < /tmp/vllm_stress_$i.json) bytes; head: $(head -c 120 /tmp/vllm_stress_$i.json)"
  done
}

case "${1:-smoke}" in
  baseline) baseline ;;
  smoke)    smoke ;;
  x3)       x3 ;;
  *) echo "usage: $0 smoke|x3|baseline"; exit 1 ;;
esac
