# minidsp-webui

A web UI for configuring miniDSP devices (built around the **2x4 HD**) on Linux, on top of
[minidsp-rs](https://github.com/mrene/minidsp-rs)'s `minidspd` daemon.

minidsp-rs gives you a CLI and an HTTP API but no graphical interface. This project adds one:
per-output gain, mute, delay, polarity, routing, PEQ and crossover editing with live frequency-response
graphs and level meters, plus Home Assistant entities via MQTT discovery.

> **Status: early development.** Working so far: the live header with master volume, mute, source and preset
> controls, a dashboard with live level meters, routing, input/output gain, mute, polarity and delay editing with
> per-setting sync status, staged edits for inactive presets, an adopt wizard (trust or push the device's current
> config, optionally from a `minidsp` CLI script), CLI-script import/export, and Home Assistant master controls over
> MQTT. PEQ, crossover and response graphs come next.

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
| `GAIN_STEP_CONFIRM_DB` | `6` | Raising a level by more than this in one step asks for confirmation (also a PEQ band's or crossover group's peak boost) |
| `PEQ_MAX_BOOST_DB` | `24` | Most a single PEQ band or crossover group may boost; larger boosts are refused |
| `DISCOVERY_INTERVAL_S` | `30` | How often to rescan minidspd for devices |
| `WRITE_COALESCE_MS` | `50` | Gather window for edits before they are written to the device |
| `MUTE_WRAP_BULK_APPLY` | `true` | Mute the affected outputs while a bulk apply (push, import, profile load) runs |
| `REAPPLY_ON_RECONNECT` | `false` | Push the active preset again after the device reconnects (e.g. after a power cycle) |
| `PUBLIC_URL` | _(empty)_ | The UI's external URL, used for Home Assistant's "visit device" link |
| `MQTT_ENABLED` | `false` | Publish Home Assistant entities over MQTT discovery |
| `MQTT_HOST` / `MQTT_PORT` | _(empty)_ / `1883` | MQTT broker, e.g. `mqtt.example.lan` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | _(empty)_ | Broker credentials |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant discovery prefix |
| `MQTT_BASE_TOPIC` | `minidsp-webui` | Prefix for state, command and availability topics |
| `MQTT_VOLUME_FLOOR_DB` | `-127` | Lowest master volume the Home Assistant slider offers |

Gain-step confirmation, the gather window, mute-wrap and reapply-on-reconnect can also be changed on the Settings
page; values saved there take precedence over the environment.

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
