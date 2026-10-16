# minidspd from the upstream minidsp-rs release, serving a mock 2x4HD (see config.toml).
# Dev/test only: the mock has no audio path and only roughly emulates the device.
FROM debian:bookworm-slim

ARG MINIDSP_VERSION=0.1.12
ARG MINIDSP_DEB=minidsp_${MINIDSP_VERSION}-1_amd64.deb
ARG MINIDSP_DEB_SHA256=713828f8dbc2ca515ecec7c32e9e9440c9fa693e467f4e75cac43832e720b7b7

# curl stays installed for the healthcheck.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSLo "/tmp/${MINIDSP_DEB}" \
        "https://github.com/mrene/minidsp-rs/releases/download/v${MINIDSP_VERSION}/${MINIDSP_DEB}" \
    && echo "${MINIDSP_DEB_SHA256}  /tmp/${MINIDSP_DEB}" | sha256sum -c - \
    && apt-get install -y --no-install-recommends "/tmp/${MINIDSP_DEB}" \
    && rm -rf "/tmp/${MINIDSP_DEB}" /var/lib/apt/lists/*

COPY config.toml /etc/minidsp/config.toml

RUN useradd --system --uid 10001 minidsp
USER minidsp

EXPOSE 5380
# /devices answers 500 (ApplicationStillInitializing) until the device manager is up, so -f doubles as a readiness check.
HEALTHCHECK --interval=2s --timeout=3s --start-period=5s --retries=15 \
    CMD ["curl", "-fsS", "-o", "/dev/null", "http://127.0.0.1:5380/devices"]

CMD ["minidspd", "--config", "/etc/minidsp/config.toml"]
