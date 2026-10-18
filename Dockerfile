# Build stage: resolve the locked dependencies into /app/.venv with uv.
FROM python:3.12-slim-bookworm AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# minidsp-rs release binaries (minidspd + the minidsp CLI) for the target architecture, sha256-pinned. Runs on the
# build platform, so a cross-arch build needs no emulation for this stage.
FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS minidsp

ARG TARGETARCH
ARG MINIDSP_VERSION=0.1.12
ARG MINIDSP_SHA256_AMD64=37f9d1be8e41fd506fca7ce006d80a73bd83b954082ac6642162e2ba67e38fb2
ARG MINIDSP_SHA256_ARM64=e4a67f70fddf620dd85d369fe5a5a777896180ed84e002bab63488d9a7f16458

RUN set -eu; \
    case "${TARGETARCH:-amd64}" in \
        amd64) triple=x86_64-unknown-linux-gnu; sha="${MINIDSP_SHA256_AMD64}" ;; \
        arm64) triple=aarch64-unknown-linux-gnu; sha="${MINIDSP_SHA256_ARM64}" ;; \
        *) echo "unsupported TARGETARCH ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/mrene/minidsp-rs/releases/download/v${MINIDSP_VERSION}/minidsp.${triple}.tar.gz"; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/minidsp.tar.gz')" "$url"; \
    echo "${sha}  /tmp/minidsp.tar.gz" | sha256sum -c -; \
    mkdir -p /out; \
    tar -xzf /tmp/minidsp.tar.gz -C /out minidsp minidspd; \
    chmod 0755 /out/minidsp /out/minidspd

# Runtime stage: the virtualenv, the app (templates and vendored static files included), the SQL migrations and the
# bundled minidspd, which the app runs as a child process unless MINIDSPD_MODE=external (docs: README "All-in-one").
FROM python:3.12-slim-bookworm

ARG GIT_COMMIT=unknown
ARG BUILD_DATE=unknown
LABEL org.opencontainers.image.source="https://github.com/ljmerza/minidsp-webui" \
      org.opencontainers.image.description="Web UI for miniDSP devices, with minidsp-rs's minidspd bundled" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_DATE}"

# libusb for minidspd; tini as PID 1 forwards signals to the app and reaps the daemon if it ever outlives the app.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libusb-1.0-0 tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=minidsp /out/minidspd /out/minidsp /usr/local/bin/

# MALLOC_ARENA_MAX keeps glibc from growing one malloc arena per thread, which matters under a 256 MB memory limit.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY app ./app
COPY migrations ./migrations
# /run/minidsp holds the generated daemon config; mount a tmpfs there so it never touches the SD card.
RUN python -m compileall -q app \
    && useradd --system --uid 1000 webui \
    && mkdir -p /data /run/minidsp \
    && chown webui /data /run/minidsp

# Non-root by default. USB access then comes from the device's group (rootful Docker: group_add + a device cgroup
# rule) or, with rootless Docker, run as `user: "0:0"` (container root is your unprivileged host user there).
USER webui
EXPOSE 8480

# Liveness only: the app keeps serving (with a banner) while minidspd or the device is down.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('WEBUI_PORT', '8480'), timeout=3)"]

# -e 143: a SIGTERM-ed app (docker stop) counts as a clean exit.
ENTRYPOINT ["/usr/bin/tini", "-e", "143", "--"]
CMD ["python", "-m", "app.main"]
