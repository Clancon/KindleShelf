FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QT_QPA_PLATFORM=offscreen \
    KINDLE_SHELF_DATA=/app/data \
    KINDLE_SHELF_PORT=8090

WORKDIR /app

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        calibre fonts-noto-cjk ca-certificates tzdata tini \
    && ebook-convert --version \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app.py ./
COPY kindle_shelf ./kindle_shelf
COPY scripts/healthcheck.py ./scripts/healthcheck.py

RUN groupadd --gid 10001 shelf \
    && useradd --uid 10001 --gid shelf --create-home shelf \
    && mkdir -p /app/data \
    && chown shelf:shelf /app/data

USER 10001:10001

EXPOSE 8090
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "scripts/healthcheck.py"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "app.py", "--host", "0.0.0.0", "--no-browser"]
