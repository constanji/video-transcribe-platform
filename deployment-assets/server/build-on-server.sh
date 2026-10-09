#!/usr/bin/env bash
# Build the three images ON the server. Used when the local machine has no
# reliable access to PyPI / GitHub.
#
# All registries are overridable; the defaults below point at mainland mirrors
# because this deployment host reaches those fastest.
set -euo pipefail

SRC="${SRC:-/opt/plugin_parser/src}"
TAG="${IMAGE_TAG:-latest}"
API_URL="${NEXT_PUBLIC_API_URL:?set NEXT_PUBLIC_API_URL}"
ONLY="${1:-}"

APT_MIRROR="${APT_MIRROR:-mirrors.aliyun.com}"
PIP_INDEX="${PIP_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"
TORCH_WHEEL_URL="${TORCH_WHEEL_URL:-https://mirrors.aliyun.com/pytorch-wheels/cpu/torch-2.14.0+cpu-cp311-cp311-manylinux_2_28_x86_64.whl}"
LLAMA_REPO="${LLAMA_REPO:-https://gitcode.com/gh_mirrors/ll/llama.cpp.git}"
LLAMA_TAG="${LLAMA_TAG:-}"

[ -f "$SRC/deploy/api.Dockerfile" ] || { echo "source not found at $SRC"; exit 1; }

build_api() {
  echo "==> Building api:${TAG}"
  docker build -t "video-platform-api:${TAG}" \
    --build-arg "APT_MIRROR=${APT_MIRROR}" \
    --build-arg "PIP_INDEX=${PIP_INDEX}" \
    -f "$SRC/deploy/api.Dockerfile" "$SRC"
}

build_web() {
  echo "==> Building web:${TAG} (API=${API_URL})"
  docker build -t "video-platform-web:${TAG}" \
    --build-arg "NEXT_PUBLIC_API_URL=${API_URL}" \
    --build-arg "NPM_REGISTRY=${NPM_REGISTRY}" \
    -f "$SRC/deploy/web.Dockerfile" "$SRC/apps/web"
}

build_moss() {
  echo "==> Building moss:${TAG} (llama.cpp from ${LLAMA_REPO} tag='${LLAMA_TAG}')"
  docker build -t "video-platform-moss:${TAG}" \
    --build-arg "APT_MIRROR=${APT_MIRROR}" \
    --build-arg "PIP_INDEX=${PIP_INDEX}" \
    --build-arg "TORCH_WHEEL_URL=${TORCH_WHEEL_URL}" \
    --build-arg "LLAMA_REPO=${LLAMA_REPO}" \
    --build-arg "LLAMA_TAG=${LLAMA_TAG}" \
    -f "$SRC/deploy/moss.Dockerfile" "$SRC"
}

case "$ONLY" in
  api) build_api ;;
  web) build_web ;;
  moss) build_moss ;;
  "") build_api; build_web; build_moss ;;
  *) echo "usage: $0 [api|web|moss]"; exit 1 ;;
esac

echo "==> Built images:"
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | grep video-platform
