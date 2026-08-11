# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE=python:3.11.15-slim-bookworm@sha256:d29f48a31a8b408ed19272ca1e7b10ebae13b240a27e862d3d4217c528e2e0c3

FROM ${PYTHON_IMAGE} AS novnc-assets

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends novnc \
    && rm -f /var/cache/apt/archives/*.deb

FROM ${PYTHON_IMAGE}

ARG PLAYWRIGHT_VERSION=1.61.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DEFAULT_TIMEOUT=100 \
    PIP_RETRIES=5 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.docker.lock ./
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --requirement requirements.docker.lock \
    && python -c "from importlib.metadata import version; assert version('playwright') == '${PLAYWRIGHT_VERSION}'"
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    python -m playwright install --with-deps chromium
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends x11vnc xauth \
    && rm -f /var/cache/apt/archives/*.deb
COPY --from=novnc-assets /usr/share/novnc /usr/share/novnc
COPY --from=novnc-assets /usr/share/doc/novnc /usr/share/doc/novnc
RUN chmod -R a+rX /ms-playwright
RUN browser_executable="$(find /ms-playwright -type f -path '*/chrome-linux64/chrome' -print -quit)" \
    && test -n "$browser_executable" \
    && ln -s "$browser_executable" /usr/local/bin/autosign-browser

COPY pyproject.toml ./

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
