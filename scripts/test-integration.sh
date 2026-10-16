#!/usr/bin/env bash
# Run the integration tests against docker-compose.dev.yml (mock minidspd + webui).
# Starts the stack if it isn't running and stops it again afterwards; a stack that was already up is left alone.
# Extra arguments go to pytest, e.g. scripts/test-integration.sh -k routing -x
set -euo pipefail

cd "$(dirname "$0")/.."

compose=(docker compose -f docker-compose.dev.yml)
export MOCK_PORT="${MOCK_PORT:-15380}"
export WEBUI_DEV_PORT="${WEBUI_DEV_PORT:-8480}"
# Not MINIDSPD_URL: that's the app's own setting and may point at real hardware.
export MINIDSPD_MOCK_URL="${MINIDSPD_MOCK_URL:-http://127.0.0.1:${MOCK_PORT}}"
export WEBUI_URL="${WEBUI_URL:-http://127.0.0.1:${WEBUI_DEV_PORT}}"

started=0
if [[ -z "$("${compose[@]}" ps --status running --quiet minidspd-mock 2>/dev/null)" ]]; then
    mkdir -p .devdata
    "${compose[@]}" up -d --build --wait
    started=1
fi

cleanup() {
    if [[ $started == 1 && -z "${KEEP_STACK:-}" ]]; then
        "${compose[@]}" down --remove-orphans
    fi
}
trap cleanup EXIT

MINIDSPD_MOCK_CONTAINER="${MINIDSPD_MOCK_CONTAINER:-$("${compose[@]}" ps --quiet minidspd-mock | head -n1)}"
export MINIDSPD_MOCK_CONTAINER

uv run pytest -m integration tests/integration -rfxX "$@"
