# minidsp-webui

A web UI for configuring miniDSP devices (built around the **2x4 HD**) on Linux, on top of
[minidsp-rs](https://github.com/mrene/minidsp-rs)'s `minidspd` daemon.

minidsp-rs gives you a CLI and an HTTP API but no graphical interface. This project adds one:
per-output gain, mute, delay, polarity, routing, PEQ and crossover editing with live frequency-response
graphs and level meters, plus Home Assistant entities via MQTT discovery.

> **Status: early development.** Only the service skeleton exists so far.

## How it works

- FastAPI backend that talks to `minidspd`'s HTTP API (default `http://127.0.0.1:5380`).
- The miniDSP can't report most DSP settings back, so the app keeps the desired configuration per preset
  in SQLite and syncs changes to the device.
- Server-rendered UI (Jinja + HTMX + Alpine.js + uPlot); no JavaScript build step.
- Ships as a multi-arch (amd64/arm64) container image, so it runs on a Raspberry Pi next to the daemon.

## Running

`minidspd` binds to loopback by default, so run the container with host networking on the machine that
runs the daemon:

```yaml
services:
  minidsp-webui:
    image: ghcr.io/ljmerza/minidsp-webui:main
    network_mode: host
    environment:
      MINIDSPD_URL: http://127.0.0.1:5380
      WEBUI_PORT: "8480"
    volumes:
      - ./data:/data
    restart: unless-stopped
```

Then open `http://<host>:8480`.

| Variable | Default | Description |
|---|---|---|
| `MINIDSPD_URL` | `http://127.0.0.1:5380` | minidspd HTTP API |
| `WEBUI_BIND` | `0.0.0.0` | Listen address |
| `WEBUI_PORT` | `8480` | Listen port |
| `DB_PATH` | `/data/minidsp-webui.sqlite3` | SQLite database |
| `LOG_LEVEL` | `info` | Log level |

## Development

```bash
uv sync --group dev
uv run ruff check . && uv run ruff format --check .
uv run pytest
uv run python -m app.main
```
