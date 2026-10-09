# Local-dev helper around deploy/moss.Dockerfile.
#
# The committed moss.Dockerfile needs `python:3.11-slim` for its builder stage.
# Docker Hub is unreachable from this machine and the mirrors available here
# fail on the blob fetch, so a full build cannot even start. This file adds the
# same llama-cli on top of an already-built moss image instead, reusing the
# torch / funasr layers that are already local.
#
# Usage, from the repo root:
#   docker build -f deploy/moss-add-llama-cli.Dockerfile -t deploy-moss:latest \
#     --build-arg APT_MIRROR=mirrors.aliyun.com \
#     --build-arg LLAMA_REPO=https://gitcode.com/gh_mirrors/ll/llama.cpp.git .
#
# On a host that can reach docker.io, build deploy/moss.Dockerfile as usual; the
# committed file stays the source of truth for the server deployment.

ARG MOSS_BASE=deploy-moss:latest

# Bookworm on purpose: the runtime image is bookworm, so the compiled binary
# links a compatible glibc / libstdc++.
FROM python:3.10-slim-bookworm AS llama-build
ARG APT_MIRROR=deb.debian.org
ARG LLAMA_REPO=https://github.com/ggml-org/llama.cpp.git
ARG LLAMA_TAG=
RUN for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do \
        if [ -f "$f" ]; then sed -i "s|deb.debian.org|${APT_MIRROR}|g" "$f"; fi; \
    done \
    && apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN if [ -n "$LLAMA_TAG" ]; then \
        git clone --depth 1 --branch "$LLAMA_TAG" "$LLAMA_REPO" /src/llama; \
    else \
        git clone --depth 1 "$LLAMA_REPO" /src/llama; \
    fi \
    && cd /src/llama \
    && cmake -B build -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DGGML_NATIVE=OFF \
    && cmake --build build --config Release -j"$(nproc)" --target llama-cli \
    && cp build/bin/llama-cli /llama-cli \
    && /llama-cli --version

FROM ${MOSS_BASE}
COPY --from=llama-build /llama-cli /usr/local/bin/llama-cli
ENV LLAMA_CLI=/usr/local/bin/llama-cli
# Keep the local image in step with the working tree. The committed moss.Dockerfile
# bakes the same file in as its last layer, but its runtime stage is built FROM
# python:3.11-slim, which cannot be pulled from here.
COPY services/moss-transcriber/main.py /app/main.py
