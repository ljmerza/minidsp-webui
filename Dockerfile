# Build stage: resolve the locked dependencies into /app/.venv with uv.
FROM python:3.12-slim-bookworm AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Runtime stage: the virtualenv, the app (templates and vendored static files included) and the SQL migrations.
FROM python:3.12-slim-bookworm

ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.source="https://github.com/ljmerza/minidsp-webui" \
      org.opencontainers.image.description="Web UI for miniDSP devices on top of minidsp-rs" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# MALLOC_ARENA_MAX keeps glibc from growing one malloc arena per thread, which matters under a 256 MB memory limit.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY app ./app
COPY migrations ./migrations
RUN python -m compileall -q app \
    && useradd --system --uid 1000 webui \
    && mkdir -p /data \
    && chown webui /data

USER webui
EXPOSE 8480

# Liveness only: the app keeps serving (with a banner) while minidspd or the device is down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEBUI_PORT', '8480'), timeout=3)"]

CMD ["python", "-m", "app.main"]
