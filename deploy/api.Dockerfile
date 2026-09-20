FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .
COPY apps/__init__.py /app/apps/__init__.py
COPY apps/api /app/apps/api
CMD ["uvicorn","apps.api.main:app","--host","0.0.0.0","--port","8000"]
