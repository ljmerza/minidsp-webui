# Mock minidspd: fidelity notes

What the dev mock (`dev/minidspd-mock`) does and doesn't do. Each claim here is covered by a test in this directory,
except where marked *(source only)*, which comes from reading the minidsp-rs source.

## Build

- `minidsp_0.1.12-1_amd64.deb` from the `v0.1.12` GitHub release of mrene/minidsp-rs (latest release as of 2026-10-15).
  The Dockerfile pins its SHA-256.
- The release also ships arm64/armhf `.deb`s and plain tarballs. The package depends on libusb/libudev, and its postinst
  only touches systemd (it's a no-op in a container).
- Config: one `[[static_device]] url = "mock:?serial=911111"`, `ignore_advertisements = true`, HTTP on `0.0.0.0:5380`
  inside the container. It's published on `127.0.0.1:15380`.
- The mock defaults to `hw_id=10, dsp_version=100, fw 1.13`, which probes as a **2x4HD**. Each device command has a 50 ms
  response delay (default `response_delay`), so a status read takes roughly 100-300 ms.

## The 0.1.12 API surface (this applies to real hardware too)

| Finding | Consequence for the app |
|---|---|
| **No `POST /devices/{i}/volume/{up,down,toggle_mute}`, `/source/{s}` or `/preset/{p}`.** They return `404 Not Found` (plain text). They were added upstream after 0.1.12 (commit d548db7). The live OpenAPI captured from production 0.1.12 lists only `/devices`, `/devices/{i}` and `/devices/{i}/config`. | `DaemonClient.volume_up/volume_down/toggle_mute/set_source/set_preset` don't work against 0.1.12. Use `POST /devices/{i}` with a `MasterStatus` body instead. Strict-xfail tests track this in `test_daemon_client.py` and `test_app_against_mock.py`. |
| Every error is HTTP 500. Typed errors have a JSON `FormattedError` body with `error.type`. **`InternalError` bodies aren't JSON**: the daemon fails to serialize them and returns `the error: 'FormattedError { ... }' couldn't be serialized as json: ...`. Out-of-range channel/PEQ/crossover indexes, a missing `index`, and delay over the limit all land here. | Callers can't rely on `error.type`. `DaemonClient` falls back to `DaemonRejected(error_type=None, body=<text>)`. |
| A routing entry with an **out-of-range output index panics the request handler**: the connection closes with no response. The daemon survives. Fixed upstream after 0.1.12. | `DaemonClient` raises `DaemonUnavailable`, so the outcome is unknown. Validate indexes before sending. |
| `/devices/{x}` matches `x` against **serials first**, then treats it as an index. | Harmless. The app resolves serial → index itself. |
| Config writes return `200` with an empty body. There is no read-back of DSP parameters. | As designed (shadow state). |
| A config call is applied **in order and stops at the first error**. Earlier parts stay applied. | A failed write means "every path in that call is unknown" (docs/03 §4). |
| Unknown JSON fields are ignored. Wrong types are a `ParseError`. | |

## Master status

- `POST /devices/{i}` returns the master status read back, **including `"dirac": null`**. `GET` omits `dirac` on the 2x4HD.
- Volume is **truncated toward zero to 0.5 dB** (-20.3 → -20.0, -20.7 → -20.5).
- Volume is **not range-checked**. `+5` is stored as `-5.0` (the sign is lost), and `-200` becomes `-127.5`. The app must
  clamp to [-127, 0] itself.
- Source names are **case-sensitive** in JSON bodies (`"Toslink"`). `"toslink"` is a `ParseError`.
- Presets: **the mock switches instantly and accepts any number** (7 is stored and reported back). Real hardware does a
  partial reset that takes seconds. Preset timing and validation can only be tested on hardware.
- `config.master_status` is applied before inputs/outputs and works the same as `POST /devices/{i}`.

## Config writes

What the mock stores. Scalar writes go to its DSP memory, and the tests read them back with
`docker exec <mock> minidsp debug dump-float <hex start> <hex end>`.

| Write | Mock behaviour | Read back? |
|---|---|---|
| input/output `gain`, `mute` | stored. Mute words: 1 = muted, 2 = unmuted | yes |
| output `gain` > +12 dB (e.g. +50) | **accepted and stored**, with no clamp | yes |
| output `invert` | stored | yes (value differs between true and false) |
| output `delay` `{secs, nanos}` | stored as whole samples at 96 kHz | yes |
| routing `mute` | stored as the **inverted enable**: `mute: true` → enable word = 1 (off) | yes |
| routing `gain` | stored | yes |
| compressor threshold/ratio/attack/release | stored | yes (`bypass` is accepted but not checked) |
| PEQ `coeff`, PEQ `bypass`, crossover `coeff`, crossover `bypass` | **acknowledged but not stored** (the mock acks unknown command types) | **no** |
| FIR | not exercised. The mock answers `FirLoadStart` with the max tap count *(source only)* | no |

Delay details:

- The daemon converts the `Duration` with `as_micros()`, which truncates to whole µs, then rounds to samples. With the
  docs/06 mapping `nanos = round(samples / 96000 * 1e9)`, the sample count always survives. Tested for 1, 7, 96, 4801,
  7999 and 8000 samples.
- The limit is **`samples > 8000` → error**, i.e. 83.3 ms, even though the message says "within [0, 80] ms". 8000 is
  accepted and 8001 is rejected.
- A bare number (`"delay": 1.5`) is a `ParseError`.

Index validation (each is a 500 with a plain-text body): input ≥ 2, output ≥ 4, PEQ ≥ 10, crossover group ≥ 2, crossover
biquad ≥ 4, a crossover biquad without `index` ("biquad index not specified"), and an output entry without `index`.

## Levels and the status websocket

- The mock's meters are a constant **-60 dBFS** on every channel. Fine for plumbing, useless for audio claims.
- `ws://…/devices/{i}`: the first frame is the full status (with levels). After that:
  - With no query: **nothing**. Master changes made through the HTTP API are **not pushed** (the mock sends no
    unsolicited notifications, so this is unverified on hardware).
  - `?levels=true`: a frame every 250 ms, `{"master": {}, "input_levels": [...], "output_levels": [...]}`. Note the
    **empty `master` object**.
  - `?poll=true`: a master-only frame (no levels keys) **only when the master status changed**, checked every 2 s.
  - `?levels=true&poll=true` works on a single socket: both streams interleave. This answers spike 0.4 for the mock.
- **minidspd never reads from the websocket.** It only forwards frames into it, so it never answers a client close frame.
  `websockets`' `close()` then waits its full `close_timeout` (10 s by default). On a poll-only socket nothing else
  arrives in that time. Measured with the current monitor:
  - Subscribing to levels takes **~10 s** before the first level event (strict xfail `test_app_levels_start_promptly`).
    Leaving levels mode takes the same 10 s.
  - App shutdown takes ~10 s. `docker stop` of the dev webui took 11 s and ended in **SIGKILL (exit 137)**, and every
    `--reload` restart waits the same.
  - Passing `close_timeout=1` to the monitor's `connect()` brought both down to ~1 s in a scratch run. Aborting the
    transport on kick would also work.
- An external master change (made behind the app's back) reaches the app's cached status within about 2 s through
  `poll=true`. This is Phase 1 acceptance, tested in `test_app_sees_external_volume_change`.

## Not covered by the mock

- Audio, real meter movement, preset-switch latency, and USB re-enumeration or power cycles (index changes).
- Whether biquad coefficients and bypass flags really land. Use the hardware acceptance tests on the Pi.
- Device-side clamping of channel gains. The mock stores anything.
