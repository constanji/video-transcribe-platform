#!/usr/bin/env bash
# Build linux/amd64 images LOCALLY and export them as tar.gz into images/.
# Requires a local machine that can reach the package registries.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT="${ROOT}/deployment-assets/images"
TAG="${IMAGE_TAG:-latest}"
PLATFORM="${BUILD_PLATFORM:-linux/amd64}"
WEB_API_URL="${WEB_API_URL:-http://localhost:8000}"
APT_MIRROR="${APT_MIRROR:-deb.debian.org}"
PIP_INDEX="${PIP_INDEX:-https://pypi.org/simple/}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmjs.org}"
TORCH_WHEEL_URL="${TORCH_WHEEL_URL:-}"
# TORCH_VARIANT=cpu (default) | cu121 — use cu121 when the target host has an
# NVIDIA GPU (see deployment-assets/server/docker-compose.gpu.yml).
TORCH_VARIANT="${TORCH_VARIANT:-cpu}"
LLAMA_REPO="${LLAMA_REPO:-https://github.com/ggml-org/llama.cpp.git}"
LLAMA_TAG="${LLAMA_TAG:-}"

mkdir -p "$OUT"
command -v docker >/dev/null || { echo 'docker is required'; exit 1; }
docker buildx inspect >/dev/null 2>&1 || { echo 'docker buildx is required'; exit 1; }

build_one() {
  local name="$1" dockerfile="$2" context="$3" archive="$4"; shift 4
  echo "==> Building ${name}:${TAG} for ${PLATFORM}"
  docker buildx build --platform "$PLATFORM" --load -t "${name}:${TAG}" -f "$ROOT/$dockerfile" "$@" "$ROOT/$context"
  echo "==> Exporting $archive"
  docker save "${name}:${TAG}" | gzip -c > "$OUT/$archive"
}

build_one video-platform-api deploy/api.Dockerfile . api.tar.gz \
  --build-arg "APT_MIRROR=${APT_MIRROR}" --build-arg "PIP_INDEX=${PIP_INDEX}"
build_one video-platform-web deploy/web.Dockerfile apps/web web.tar.gz \
  --build-arg "NEXT_PUBLIC_API_URL=${WEB_API_URL}" --build-arg "NPM_REGISTRY=${NPM_REGISTRY}"
build_one video-platform-moss deploy/moss.Dockerfile . moss.tar.gz \
  --build-arg "APT_MIRROR=${APT_MIRROR}" --build-arg "PIP_INDEX=${PIP_INDEX}" \
  --build-arg "TORCH_WHEEL_URL=${TORCH_WHEEL_URL}" \
  --build-arg "TORCH_VARIANT=${TORCH_VARIANT}" \
  --build-arg "LLAMA_REPO=${LLAMA_REPO}" --build-arg "LLAMA_TAG=${LLAMA_TAG}"

echo "Images written to $OUT"
ls -lh "$OUT"/*.tar.gz
