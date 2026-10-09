FROM python:3.11-slim
ARG APT_MIRROR=deb.debian.org
ARG PIP_INDEX=https://pypi.org/simple/
WORKDIR /app
RUN sed -i "s|deb.debian.org|${APT_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
RUN pip install --no-cache-dir -i "${PIP_INDEX}" .
COPY apps/__init__.py /app/apps/__init__.py
COPY apps/api /app/apps/api
CMD ["uvicorn","apps.api.main:app","--host","0.0.0.0","--port","8000"]
