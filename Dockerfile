FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STORAGE_DIR=/data \
    MAX_FILE_AGE_SECONDS=1800 \
    MAX_CONCURRENT_DOWNLOADS=4

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .

RUN python -m pip install --upgrade pip \
    && python -m pip install --upgrade yt-dlp \
    && python -m pip install -r requirements.txt

COPY main.py .
COPY templates ./templates

RUN mkdir -p /data \
    && useradd \
        --create-home \
        --uid 10001 \
        appuser \
    && chown -R appuser:appuser /app /data

USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-${SERVER_PORT:-8000}}"]
