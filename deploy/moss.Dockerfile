FROM python:3.11-slim
WORKDIR /app
ENV MOSS_PRELOAD=1
RUN pip install --no-cache-dir fastapi uvicorn pydantic
COPY services/moss-transcriber/main.py /app/main.py
CMD ["uvicorn","main:app","--host","0.0.0.0","--port","9000"]
