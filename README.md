# minidsp-webui

A web UI for configuring miniDSP devices (built around the **2x4 HD**) on Linux, on top of
[minidsp-rs](https://github.com/mrene/minidsp-rs)'s `minidspd` daemon.

minidsp-rs gives you a CLI and an HTTP API but no graphical interface. This project adds one:
per-output gain, mute, delay, polarity, routing, PEQ and crossover editing with live frequency-response
graphs and level meters, plus Home Assistant entities via MQTT discovery.

> **Status: early development.** Working so far: the live header with master volume, mute, source and preset
> controls, a dashboard with live level meters, and a device info page. Per-channel editing comes next.

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

Then open `http://<host>:8480`. Every JavaScript and CSS library is vendored under `app/static/vendor`
(versions in `VERSIONS.md` there), so the UI works without internet access.

Works with minidspd 0.1.12, the current minidsp-rs release.

| Variable | Default | Description |
|---|---|---|
| `MINIDSPD_URL` | `http://127.0.0.1:5380` | minidspd HTTP API |
| `WEBUI_BIND` | `0.0.0.0` | Listen address |
| `WEBUI_PORT` | `8480` | Listen port |
| `DB_PATH` | `/data/minidsp-webui.sqlite3` | SQLite database |
| `LOG_LEVEL` | `info` | Log level |
| `DAEMON_TIMEOUT_S` | `5` | Timeout for minidspd HTTP calls (seconds) |
| `PRESET_SWITCH_TIMEOUT_S` | `15` | How long to wait for the device to report a new preset |
| `GAIN_MIN_DB` / `GAIN_MAX_DB` | `-72` / `12` | Allowed range for channel and routing gains |
| `GAIN_STEP_CONFIRM_DB` | `6` | Raising a level by more than this in one step asks for confirmation |
| `DISCOVERY_INTERVAL_S` | `30` | How often to rescan minidspd for devices |

## Development

```bash
uv sync --group dev
make lint                # ruff check + format check
make test                # unit tests, no docker needed
make dev-up              # mock minidspd on 127.0.0.1:15380 + the UI with hot reload on 127.0.0.1:8480
make test-integration    # integration tests against the dev stack (starts and stops it as needed)
make dev-down
```

The dev stack runs the real `minidspd` 0.1.12 against its built-in mock device, so no hardware is needed.
`tests/integration/MOCK_NOTES.md` lists what the mock does and doesn't simulate. The JSON API is under `/api/v1`
(OpenAPI docs at `/docs`), and live updates are a Server-Sent Events stream at `/api/v1/events`.
