FROM python:3.11-slim AS novnc-assets

RUN apt-get update \
    && apt-get install -y --no-install-recommends novnc \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=5 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN pip install --no-cache-dir "playwright>=1.55,<2"
RUN python -m playwright install --with-deps chromium
RUN apt-get update \
    && apt-get install -y --no-install-recommends x11vnc xauth \
    && rm -rf /var/lib/apt/lists/*
COPY --from=novnc-assets /usr/share/novnc /usr/share/novnc
COPY --from=novnc-assets /usr/share/doc/novnc /usr/share/doc/novnc
RUN chmod -R a+rX /ms-playwright

COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
      "hatchling>=1.27" \
      "alembic>=1.16,<2" \
      "cryptography>=45,<47" \
      "fastapi>=0.116,<1" \
      "pydantic-settings>=2.10,<3" \
      "sqlalchemy>=2.0,<3" \
      "tzdata>=2025.2" \
      "uvicorn>=0.35,<1" \
      "websockets>=15,<17"

COPY README.md ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/autosign-entrypoint

RUN pip install --no-build-isolation --no-deps .
RUN chmod +x /usr/local/bin/autosign-entrypoint

RUN mkdir -p /data \
    && mkdir -p /tmp/.X11-unix \
    && chmod 1777 /tmp/.X11-unix \
    && useradd --create-home --uid 10001 autosign \
    && chown -R autosign:autosign /app /data

USER autosign

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["autosign-entrypoint"]
