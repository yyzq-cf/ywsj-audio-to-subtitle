FROM python:3.12-slim

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
ENV DATA_DIR=/data

LABEL org.opencontainers.image.title="ywsj-audio-to-subtitle"
LABEL org.opencontainers.image.description="Free audio to subtitle tool using JianYing and BCut ASR"
LABEL org.opencontainers.image.source="https://github.com/yyzq-cf/ywsj-audio-to-subtitle"

RUN groupadd -r appgroup && useradd -r -g appgroup appuser \
    && mkdir -p /data && chown appuser:appgroup /data

RUN apt-get update && apt-get install -y --no-install-recommends gosu ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appgroup . .

RUN chmod +x /app/entrypoint.sh

USER root

EXPOSE 5200

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5200/health')" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "-w", "1", "-b", "0.0.0.0:5200", "--timeout", "600", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
