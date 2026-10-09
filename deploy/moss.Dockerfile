# ---- stage 1: build llama.cpp (llama-cli) from source ----
# LLAMA_REPO may point at a mirror (e.g. gitcode) when github is unreachable.
# Leave LLAMA_TAG empty to build the default branch, or set it to a tag (bXXXX).
FROM python:3.11-slim AS llama-build
ARG APT_MIRROR=deb.debian.org
ARG LLAMA_REPO=https://github.com/ggml-org/llama.cpp.git
ARG LLAMA_TAG=
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends git cmake build-essential \
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

# ---- stage 2: runtime ----
FROM python:3.11-slim
WORKDIR /app
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX=https://pypi.org/simple/
# CPU-only torch wheel. When set, it is fetched with curl (much faster than pip
# for some mirrors); leave empty to install "torch" from PIP_INDEX.
ARG TORCH_WHEEL_URL=
ENV MOSS_PRELOAD=1
ENV LLAMA_CLI=/usr/local/bin/llama-cli
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgomp1 libstdc++6 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=llama-build /llama-cli /usr/local/bin/llama-cli
RUN if [ -n "${TORCH_WHEEL_URL}" ]; then \
        f="/tmp/$(basename "${TORCH_WHEEL_URL}")" \
        && curl -fL --retry 3 --retry-delay 3 -o "$f" "${TORCH_WHEEL_URL}" \
        && pip install --no-cache-dir "$f" \
        && rm -f "$f"; \
    else \
        pip install --no-cache-dir -i "${PIP_INDEX}" torch; \
    fi
COPY deploy/moss-requirements.txt /tmp/moss-requirements.txt
RUN pip install --no-cache-dir -i "${PIP_INDEX}" -r /tmp/moss-requirements.txt
COPY services/moss-transcriber/main.py /app/main.py
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","9000"]
