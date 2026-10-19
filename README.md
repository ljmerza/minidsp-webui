# minidsp-webui

minidsp-webui is a self-hosted web UI for the miniDSP 2x4 HD that runs in one Docker container on Linux (a Raspberry Pi
is plenty) and talks to the device over USB through a bundled [minidsp-rs](https://github.com/mrene/minidsp-rs) daemon.
It explains in plain words what every output is doing and walks you through fixing problems, while still giving you
every DSP setting, live meters and response graphs, with no Windows plugin app needed.

<p align="center"><img src=".github/assets/dashboard.png" alt="minidsp-webui home screen" width="900"></p>

## Features

### New: guidance and problem-solving
- **A home screen that explains itself.** A health line says what's wrong in plain words ("Sub is silent: no input is
  routed to it") with a one-tap fix, and every output card says what it does ("Plays 20–80 Hz, fed by Receiver In,
  waits 9 ms").
- **Guided tasks:** Something wrong?, Make one louder, Fix a boom, Line up shakers, Set levels, Save or load a setup,
  and Set up this device. Each one previews the change, sends nothing until you press Apply, and offers Undo.
- **Signal-flow diagram** from inputs to outputs, with live levels and problems drawn where they are.
- **"?" help on every term** (PEQ, crossover, dBFS, routing…) plus a glossary page.

### Everything else
- Every DSP setting per channel: routing matrix, gain, mute, delay, polarity, 10-band PEQ, crossovers
  (Butterworth / Linkwitz-Riley / Bessel) with response graphs, and a compressor.
- Live level meters with clip lights; master volume, mute, source and presets.
- Keeps the app and device in sync, checks them against a readback, and adopts an existing setup.
- Profiles and history with undo; import/export of REW filters, `minidsp` CLI scripts and JSON backups.
- Home Assistant entities through MQTT discovery.
- One container with USB passthrough, easy on SD cards, and usable on a phone.

Works with minidspd 0.1.12, the current minidsp-rs release.

## Setup

### All-in-one install

The image bundles `minidspd` 0.1.12 from minidsp-rs and runs it next to the app, so the host needs only Docker: no
minidsp-rs package, no host networking. The container talks to the miniDSP over USB itself, and the daemon's own API
stays inside the container on `127.0.0.1:5380`. [`docker-compose.example.yml`](docker-compose.example.yml) is a complete
example. The essentials, for **rootful Docker** (the usual install):

```yaml
services:
  minidsp-webui:
    image: ghcr.io/ljmerza/minidsp-webui:main
    ports: ["8480:8480"]
    volumes:
      - ./data:/data
      - /dev/bus/usb:/dev/bus/usb        # the whole tree, so a re-plug (new device number) is still visible
    device_cgroup_rules: ["c 189:* rmw"] # allow USB device nodes (major 189)
    group_add: ["46"]                    # the gid that owns the device node on the host (see below)
    tmpfs: ["/run/minidsp:mode=1777,size=1m"] # the generated daemon config stays in RAM
    restart: unless-stopped
    mem_limit: 256m
```

Then open `http://<host>:8480`. Images are published for `linux/amd64` and `linux/arm64`.

Before the first start:

1. **Only one process can own the miniDSP.** If minidsp-rs is installed on the host, stop its daemon:
   `sudo systemctl disable --now minidsp`. The `minidsp` CLI is still available as `docker exec minidsp-webui minidsp`.
2. **Give the device node a group.** The container runs as uid 1000 and opens the device through `group_add`. The
   minidsp-rs package installs a udev rule that does this (`GROUP="plugdev"`, mode 0660). Without the package, add your
   own, e.g. `/etc/udev/rules.d/90-minidsp.rules`:

   ```
   SUBSYSTEM=="usb", ATTR{idVendor}=="2752", GROUP="plugdev", MODE="0660"
   ```

   Apply it with `sudo udevadm control --reload && sudo udevadm trigger --action=add --subsystem-match=usb
   --attr-match=idVendor=2752` (or re-plug the device). Put the group's numeric id in `group_add`:
   `getent group plugdev | cut -d: -f3` (46 on Debian and Raspberry Pi OS).
3. **The data directory must be writable by uid 1000:** `mkdir -p data && sudo chown 1000:1000 data`.

The alternative to `group_add` is running the container as root (`user: "0:0"`), still with the cgroup rule.

#### Rootless Docker

With rootless Docker, the host's root and `plugdev` aren't mapped into the container: the device node shows up as
`nobody:nogroup` and `group_add` can't help. Instead, give the device to the user that runs Docker and run the container
as root. Container root is that unprivileged host user, so this grants nothing beyond what your user already has.

1. udev rule, e.g. `/etc/udev/rules.d/90-minidsp.rules` (replace `youruser`):

   ```
   SUBSYSTEM=="usb", ATTR{idVendor}=="2752", OWNER="youruser", MODE="0660"
   ```

   Apply it with `sudo udevadm control --reload && sudo udevadm trigger --action=add --subsystem-match=usb
   --attr-match=idVendor=2752`, or re-plug the device. A plain `udevadm trigger` (a "change" event) doesn't re-apply
   `OWNER`, and that is also true when you remove the rule again: re-plug or use `--action=add`.
2. In the compose file: add `user: "0:0"`. Drop `group_add`. `device_cgroup_rules` has no effect under rootless Docker,
   so it can stay or go.
3. The data directory just needs to belong to your user (container root writes as your user), so no `chown` is needed.
4. `mem_limit: 256m` only takes effect if systemd delegates the cgroup v2 `memory` controller to your user. Many
   distributions (Raspberry Pi OS included) delegate only `cpu pids` by default, and then Docker silently ignores the
   limit: a runaway container grows until the host's OOM killer picks a victim, which may be another service or sshd.
   Check with `docker info`: it must not print `WARNING: No memory limit support`, and
   `cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers` must list `memory`.
   To enable it, create `/etc/systemd/system/user@.service.d/delegate.conf`:

   ```ini
   [Service]
   Delegate=cpu cpuset io memory pids
   ```

   then run `sudo systemctl daemon-reload` and reboot (or log out fully and back in), and recreate the container.
   `docker inspect minidsp-webui --format '{{.HostConfig.Memory}}'` should then print `268435456`.

#### Reaching the bundled daemon from the host

To keep using the `minidsp` CLI on the host with `--daemon-url`, set `MINIDSPD_HTTP_BIND: "0.0.0.0:5380"` and publish
the port on loopback only: `ports: ["8480:8480", "127.0.0.1:5380:5380"]`. The daemon's API has no authentication, so
never publish it on all interfaces. `docker exec minidsp-webui minidsp` works without any of this.

### Using a host minidspd instead (external mode)

To keep running `minidspd` on the host (installed from the minidsp-rs package), set `MINIDSPD_MODE=external`. The app
then behaves as before the bundled daemon existed. `minidspd` binds to loopback by default, so run the container with
host networking on the machine that runs the daemon:

```yaml
services:
  minidsp-webui:
    image: ghcr.io/ljmerza/minidsp-webui:main
    network_mode: host
    environment:
      MINIDSPD_MODE: external
      MINIDSPD_URL: http://127.0.0.1:5380
    volumes:
      - ./data:/data
    restart: unless-stopped
    mem_limit: 256m
```

The container needs no USB access in this mode. Setting `MINIDSPD_URL` without `MINIDSPD_MODE` also selects external
mode, so compose files written before the all-in-one image keep working.

### Demo mode (no hardware)

`MINIDSPD_MOCK=true` adds minidspd's built-in mock 2x4HD (serial 911111). No USB device or mount is needed:

```bash
docker run --rm -p 8480:8480 -e MINIDSPD_MOCK=true ghcr.io/ljmerza/minidsp-webui:main
```

The mock has no audio path and only roughly emulates the device. State goes to the container's `/data`, so it is lost
with the container unless you mount a volume.

### Security

The app has no login. Anyone who can reach the port can change the device, so keep it on a trusted network or put
it behind a reverse proxy that adds authentication. When proxying, turn buffering off for `/api/v1/events` (nginx:
`proxy_buffering off;`), or live updates stall.

Requests that a browser sends on behalf of another site are refused, but a malicious page can get around that with
DNS rebinding: it points its own host name at the app, and the browser then treats the app as that page's origin.
Such a page could read the database backup and change the device. So by default the app only answers requests
addressed to an IP address or `localhost`; any other host name gets a 400. If you reach the UI by a name
(`minidsp.example.lan`, or the name your reverse proxy forwards), list it in `ALLOWED_HOSTS`. `ALLOWED_HOSTS=*` turns
the check off, for example behind a reverse proxy that already only forwards its own host name.

### Health

- `GET /healthz` is the liveness probe the image's `HEALTHCHECK` uses. It answers 200 as long as the app is running;
  its `health` field carries the overall status below.
- `GET /api/v1/health` reports `status` (`ok`, `degraded` or `starting`) and a readable `problems` list: minidspd
  unreachable, device missing, live stream reconnecting, settings that failed to write, or state that isn't being
  saved. The app keeps retrying on its own in all of these cases, and the UI shows a banner meanwhile. Add
  `?strict=true` to get a 503 whenever the status isn't `ok` (for external monitoring).
- The bundled daemon shows up as `daemon_mode` (`embedded` or `external`), `daemon_process` (`running`, `restarting`,
  `failed`, `starting`, `stopped`, or `external` in external mode), `daemon_restarts`, `daemon_pid` and
  `daemon_last_exit`. USB is reported as `usb_device_visible`, `usb_access` (`ok`, `denied`, `not_mounted`, `absent`
  or `unknown`) and a readable `usb_detail`. The Device & sync page shows the same.
- If `minidspd` exits, the app restarts it after 1 s, backing off to 30 s while it keeps failing. Five restarts within
  five minutes log an error and turn `daemon_process` to `failed` until a run lasts.
- `daemon_process_error` is the current problem: why the daemon isn't running, or, while it runs, a failed HTTP
  listener. How an earlier run ended stays in `daemon_last_exit`.

### Troubleshooting

- **`usb_access: denied` with EPERM:** rootful Docker without `device_cgroup_rules: ["c 189:* rmw"]`.
- **`usb_access: denied` with EACCES:** the container user can't open the node. `usb_detail` shows the node's owner and
  mode and the container's uid and groups. Rootful: fix the udev rule's `GROUP` and the gid in `group_add`. Rootless:
  check the `OWNER` rule and `user: "0:0"`, and that the rule was applied (`--action=add` or a re-plug).
- **`usb_access: not_mounted`:** `/dev/bus/usb` isn't mounted into the container, or the device was plugged in after a
  `--device` mount of a single node. Mount the whole `/dev/bus/usb` tree.
- **`usb_access: absent`:** no miniDSP (USB vendor `2752`) is plugged into the host. Check `lsusb` on the host.
- **The device is visible but never shows up in the UI:** another `minidspd` (on the host, or in a second container)
  may still own it. Stop it, then restart the container. If the app logged "restarting embedded minidspd: a miniDSP
  appeared on USB …" and the device still doesn't appear, that is the likely cause.
- **After unplugging, re-plugging or power cycling the device:** it comes back under a new USB device number, and the
  bundled daemon can't see that by itself (USB hotplug events don't reach a container). The app watches for it and
  restarts the daemon, so the device reappears within about 20 s without a container restart. The reconnect then
  treats it as a possible power cycle: settings that can't be read back are marked unknown. `usb_device_visible` tells
  you whether the container can see the device at all.
- **`daemon_process: failed` with "cannot write the minidspd config":** `MINIDSPD_EXTRA_CONFIG` isn't valid TOML, sets
  the HTTP address (use `MINIDSPD_HTTP_BIND`) or has an `http_server` that isn't a table. This one isn't retried; fix
  the setting and restart.
- **`daemon_process: running` but the daemon is unreachable, with "HTTP/TCP listener error" in `daemon_process_error`:**
  the daemon couldn't open its HTTP port (usually another process already uses `MINIDSPD_HTTP_BIND`'s port). USB
  discovery keeps running, so the process stays up. `MINIDSPD_HTTP_BIND` must be an IP address and port; hostnames
  such as `localhost` are refused at startup.
- **A new tab never loads, or buttons stop responding, with many tabs open:** every open page keeps one live-update
  connection, and over plain HTTP browsers allow only about 6 connections per host. Close a few tabs of the app, or serve
  it through a reverse proxy that speaks HTTP/2.
- **Logs:** the daemon's warnings and errors appear in `docker logs` under the `minidspd` logger. A warning the daemon
  repeats (it retries a device it can't open every second) is logged once, then at most every 5 minutes with a repeat
  count. Its info output is dropped, so idle disk writes stay at zero. Set `RUST_LOG=info` (or `debug`) on the
  container to see more; its lines then appear at `DEBUG` under the `minidspd` logger, whatever `LOG_LEVEL` is.

### Upgrading

Back up first (Settings → Backup), then run `docker compose pull && docker compose up -d`. Database migrations run
automatically at startup.

### Configuration

Every setting is an environment variable. Values marked † can also be changed on the Settings page; values saved
there take precedence over the environment.

#### Server

| Variable | Default | Description |
|---|---|---|
| `WEBUI_BIND` | `0.0.0.0` | Listen address |
| `WEBUI_PORT` | `8480` | Listen port |
| `DB_PATH` | `/data/minidsp-webui.sqlite3` | SQLite database; nightly backups go to `backups/` next to it |
| `LOG_LEVEL` | `warning` | Log level. Per-request lines (the access log and every minidspd call) only appear at `debug` |
| `PUBLIC_URL` | _(empty)_ | The UI's external URL, used for Home Assistant's "visit device" link |
| `ALLOWED_HOSTS` | _(empty: IPs and localhost only)_ | Comma-separated host names the UI is reached by, e.g. `minidsp.example.lan`. IP addresses and `localhost` always work; requests for any other name get a 400. `*` disables the check. Blocks DNS-rebinding pages; see [Security](#security) |
| `DAEMON_TIMEOUT_S` | `5` | Timeout for minidspd HTTP calls (seconds) |
| `DISCOVERY_INTERVAL_S` | `30` | How often to rescan minidspd for devices (it retries faster while something is missing) |
| `MONITOR_BACKOFF_MIN_S` / `MONITOR_BACKOFF_MAX_S` | `0.5` / `10` | Reconnect backoff for a device's live status stream |
| `LEVELS_IDLE_GRACE_S` | `5` | How long the level meters stay on after the last viewer leaves (level polling uses USB bandwidth) |

#### Daemon

| Variable | Default | Description |
|---|---|---|
| `MINIDSPD_MODE` | `embedded` | `embedded` runs the bundled `minidspd`; `external` uses `MINIDSPD_URL`. Unset with `MINIDSPD_URL` set means `external` |
| `MINIDSPD_URL` | `http://127.0.0.1:5380` | External mode: the minidspd HTTP API. Ignored in embedded mode |
| `MINIDSPD_MOCK` | `false` | Embedded mode: add minidspd's mock 2x4HD (demo without hardware) |
| `MINIDSPD_MOCK_LEVELS` | `off` | With `MINIDSPD_MOCK`: replace the mock's flat −60 dBFS meters with a pattern that follows the routing (`signal`: In 1 around −20 dBFS; `hot`: around −6 dBFS, so loud outputs clip; `silent`: no signal). For tests and demos; never applied to a real device |
| `MINIDSPD_HTTP_BIND` | `127.0.0.1:5380` | Embedded mode: where the daemon's API listens inside the container. `0.0.0.0:5380` to publish it (loopback only!) |
| `MINIDSPD_DISCOVERY` | `false` | Embedded mode: pick up network devices (WI-DG) that advertise themselves |
| `MINIDSPD_EXTRA_CONFIG` | _(empty)_ | Embedded mode: raw TOML merged into the generated [daemon config](https://github.com/mrene/minidsp-rs/blob/master/docs/config.example.toml), e.g. a `[[static_device]]` |
| `MINIDSPD_CONFIG_DIR` | `/run/minidsp` | Embedded mode: where the generated config is written; mount a tmpfs there (falls back to `/dev/shm`) |
| `MINIDSPD_BINARY` | `minidspd` | Embedded mode: the daemon executable |

#### Sync

| Variable | Default | Description |
|---|---|---|
| `WRITE_COALESCE_MS` † | `50` | Gather window for edits before they are written to the device |
| `MUTE_WRAP_BULK_APPLY` † | `true` | Mute the affected outputs while a bulk apply (push, import, profile load) runs |
| `REAPPLY_ON_RECONNECT` † | `false` | Push the active preset again after the device reconnects (e.g. after a power cycle) |
| `PRESET_SWITCH_TIMEOUT_S` | `15` | How long to wait for the device to report a new preset |
| `VERIFY_NIGHTLY` | `false` | Verify the active preset of each device once a night; drift shows in the event log and the Home Assistant drift sensor |
| `VERIFY_AT` | `04:15` | Local time of the nightly verify (`HH:MM`) |

#### Safety

| Variable | Default | Description |
|---|---|---|
| `GAIN_MIN_DB` / `GAIN_MAX_DB` | `-72` / `12` | Allowed range for channel and routing gains (must stay within -100 to +24) |
| `GAIN_STEP_CONFIRM_DB` † | `6` | Raising a level by more than this in one step asks for confirmation (master volume, channel gains, a PEQ band's or crossover group's peak boost) |
| `VOLUME_DROP_CONFIRM_DB` † | `20` | In the UI, lowering the master volume by more than this in one action asks for confirmation. Home Assistant and the API are never blocked; their changes are logged |
| `PEQ_MAX_BOOST_DB` | `24` | Most a single PEQ band or crossover group may boost; larger boosts are refused |
| `FIR_EXPERIMENTAL` † | `false` | Show the FIR tab on models whose FIR support is unverified (the 2x4 HD). Loading taps mutes the device briefly |

#### Graphs

| Variable | Default | Description |
|---|---|---|
| `GRAPH_VIEW` † | `auto` | Frequency range graphs open in: `sub` (5–300 Hz), `full` (10 Hz–20 kHz) or `auto` (each page's own). A range picked on a page is remembered by the browser and wins |
| `GRAPH_DB_RANGE` † | `24` | Default magnitude scale, ± this many dB (3 to 60); the Fit button still auto-scales |

#### Guidance

Rules of thumb behind the plain-language summaries and health notes, not device facts.

| Variable | Default | Description |
|---|---|---|
| `GUIDANCE_HIGH_TOTAL_DB` † | `12` | A total level (input + route + output) at or above this gets a headroom caution |
| `GUIDANCE_NO_SIGNAL_DBFS` † | `-90` | Meters below this read "no signal" |
| `GUIDANCE_VOLUME_QUIET_DB` † | `-80` | Volume below this gets a "very quiet" note |
| `GUIDANCE_VOLUME_TYPICAL_MIN_DB` / `_MAX_DB` † | `-40` / `-10` | The usual listening range of Volume |
| `GUIDANCE_COMP_THRESHOLD_DBFS` / `GUIDANCE_COMP_RATIO` † | `-60` / `10` | A compressor at or below this threshold with at least this ratio gets a "may hold it almost silent" caution |

#### MQTT / Home Assistant

| Variable | Default | Description |
|---|---|---|
| `MQTT_ENABLED` | `false` | Publish Home Assistant entities over MQTT discovery |
| `MQTT_HOST` / `MQTT_PORT` | _(empty)_ / `1883` | MQTT broker, e.g. `mqtt.example.lan` |
| `MQTT_USERNAME` / `MQTT_PASSWORD` | _(empty)_ | Broker credentials (use an env file, not the compose file) |
| `MQTT_DISCOVERY_PREFIX` | `homeassistant` | Home Assistant discovery prefix |
| `MQTT_BASE_TOPIC` | `minidsp-webui` | Prefix for state, command and availability topics |
| `MQTT_VOLUME_FLOOR_DB` | `-127` | Lowest master volume the Home Assistant slider offers |
| `MQTT_LEVELS` | `false` | Feed the input/output level sensors (at most 1 Hz; keeps the device's level polling on) |

## Development

```bash
uv sync --group dev
make lint                # ruff check + format check
make test                # unit tests, no docker needed
make dev-up              # mock minidspd on 127.0.0.1:15380 + the UI with hot reload on 127.0.0.1:8480
make test-integration    # integration tests against the dev stack (starts and stops it as needed)
make dev-down
make dev-up-embedded     # the all-in-one variant: bundled minidspd + mock device, UI on 127.0.0.1:8481
make test-embedded       # build the image and test it in embedded + mock mode
make e2e-setup           # once: Chromium for Playwright + the minidspd release binary (to ~/.cache/minidsp-webui)
make test-e2e            # browser tests at 1440x900 and 390x844 (touch), with axe-core accessibility checks
```

The browser tests (`tests/e2e`, Playwright for Python) start the app itself in embedded mode with minidspd's mock
device, on random loopback ports and a throwaway database, one fresh app per test file. They ignore your environment
and `.env`, and refuse to run if minidspd sees any device besides the mock. `tests/e2e/conftest.py` describes the
fixtures; pytest-playwright's `--headed`, `--slowmo 200` and `--tracing retain-on-failure` help when one fails.

The dev stack (`docker-compose.dev.yml`) runs the real `minidspd` 0.1.12 against its built-in mock device, so no
hardware is needed. Its `webui` service runs in external mode against a separate mock daemon container; the
`embedded` profile runs the image the way production does, with the bundled daemon and its mock device.
`tests/integration/MOCK_NOTES.md` lists what the mock does and doesn't simulate. To run the app
outside Docker against it: `MINIDSPD_URL=http://127.0.0.1:15380 DB_PATH=.devdata/local.sqlite3 uv run python -m
app.main`.

The JSON API is under `/api/v1` (OpenAPI docs at `/docs`), and live updates are a Server-Sent Events stream at
`/api/v1/events`. The MQTT integration tests start a throwaway Mosquitto container on a random port; they never touch
a real broker.

### How it works

- A FastAPI backend talks to `minidspd`'s HTTP API (default `http://127.0.0.1:5380`) and keeps one live status
  stream open per device. In the all-in-one image the app also runs that `minidspd` as a child process: it writes the
  daemon's config to a tmpfs, restarts it if it exits and stops it on shutdown (`tini` is PID 1).
- Desired state, the last confirmed device state, history, profiles and the event log live in SQLite. Database
  writes are debounced and the event log is trimmed daily, to spare SD cards.
- One writer per device coalesces edits (a slider drag becomes a few writes, not hundreds), checks the active preset
  before every write, and retries failed writes with backoff, giving up on a setting after 5 attempts.
- The UI is server-rendered (Jinja, HTMX, Alpine.js, uPlot) with Server-Sent Events for live updates, so every open
  tab converges on the same state.

CI runs ruff and pytest on every pull request and builds multi-arch images to GHCR, using the reusable workflows in
[ljmerza/misc-actions](https://github.com/ljmerza/misc-actions). The browser tests run in their own workflow
(`.github/workflows/e2e.yml`) on every pull request and push to `main`.
