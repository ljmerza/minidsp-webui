"""FIR filters per output: WAV upload -> stored blob -> preview -> load to the device, plus the "experimental FIR"
capability toggle.

- An upload is parsed in memory (`app.formats.wav`), checked against the profile (mono, internal rate, at most
  ``fir_max_taps``) and stored content-addressed in ``fir_blob``. Uploading doesn't touch the desired state.
- Loading points ``outputs.N.fir.blob_id`` at a blob. The writer sends the taps alone, and minidspd mutes the device
  while it loads them (docs 03 §4), so a load or clear always needs ``confirm``. An un-bypass waits for the taps to be
  confirmed on the device (payload builder), and enabling a FIR that has no taps is refused: a cleared FIR passes no
  audio.
- FIR on the 2x4HD is unverified (spike 0.3 skipped), so the stock profile has ``fir_supported=False`` and every FIR
  action is refused until the experimental toggle is on. The toggle swaps each session's profile for a copy with
  ``fir_supported=True``; FIR paths then show up as unknown and are written only when touched.
"""

import logging
import math
from collections.abc import Iterable
from typing import Any

import numpy as np

from app.config import Settings
from app.dsp import response as resp
from app.formats.wav import WavError, read_fir_taps
from app.model.device_profile import PROFILES, DeviceProfile
from app.state import Repo
from app.state.repo import FirBlob
from app.sync.session import (
    FIR_CLEAR_WARNING,
    FIR_LOAD_WARNING,
    ConfirmationRequired,
    DesiredValidationError,
    DeviceSession,
    EditResult,
    StateUnavailable,
)

log = logging.getLogger(__name__)

FIR_SETTING_KEY = "fir.experimental"
MAX_UPLOAD_BYTES = 1 << 20  # 4096 taps of 32-bit float are 16 KiB; anything near this is not an impulse for us
PREVIEW_POINTS = 256
PREVIEW_F_MIN = 5.0
PREVIEW_F_MAX = 24_000.0
SYMMETRY_TOLERANCE = 1e-6  # relative to the largest tap
LOAD_WARNING = FIR_LOAD_WARNING.format(gain="")
CLEAR_WARNING = FIR_CLEAR_WARNING


class FirError(DesiredValidationError):
    """A FIR request was refused (bad file, unknown blob, nothing loaded to enable)."""


class FirDisabled(FirError):
    """The device profile has no FIR support (the experimental toggle is off)."""


class FirBlobNotFound(FirError):
    """No stored FIR blob with that id."""


# --- addressing ---------------------------------------------------------------------------------------------------


def require_fir(profile: DeviceProfile) -> None:
    if not profile.fir_supported:
        raise FirDisabled(
            "FIR is off for this device: it is unverified on this model. Turn on experimental FIR in Settings first"
        )


def check_output(profile: DeviceProfile, channel: int) -> str:
    """``outputs.N.fir``; raises `FirError` for an output outside the profile."""
    if not 0 <= channel < profile.n_outputs:
        raise FirError(f"output {channel} outside 0..{profile.n_outputs - 1}")
    return f"outputs.{channel}.fir"


def _repo(session: DeviceSession) -> Repo:
    if session.repo is None:
        raise StateUnavailable("FIR filters need the state database, which isn't available")
    return session.repo


# --- analysis -----------------------------------------------------------------------------------------------------


def _symmetry(taps: np.ndarray) -> str | None:
    tol = SYMMETRY_TOLERANCE * float(np.max(np.abs(taps)))
    if len(taps) > 1 and np.allclose(taps, taps[::-1], rtol=0.0, atol=tol):
        return "symmetric"
    if len(taps) > 1 and np.allclose(taps, -taps[::-1], rtol=0.0, atol=tol):
        return "antisymmetric"
    return None


def _db(value: float) -> float:
    return 20 * math.log10(value) if value > 0 else resp.DB_FLOOR


def analyze(taps: np.ndarray | list[float], fs: float = resp.FS) -> dict[str, Any]:
    """Tap count, length, peak position, latency estimate and gains of an impulse.

    The latency of a linear-phase (symmetric) FIR is exactly ``(N - 1) / 2`` samples; for anything else the position
    of the largest tap is the usual estimate (it's what a measurement would align to).
    """
    h = np.asarray(taps, dtype=np.float64)
    n = len(h)
    peak_index = int(np.argmax(np.abs(h)))
    symmetry = _symmetry(h)
    linear = symmetry is not None
    latency_samples = (n - 1) / 2 if linear else float(peak_index)
    return {
        "n_taps": n,
        "length_ms": round(n / fs * 1000.0, 4),
        "peak_index": peak_index,
        "peak_ms": round(peak_index / fs * 1000.0, 4),
        "symmetry": symmetry,
        "linear_phase": linear,
        "latency_samples": latency_samples,
        "latency_ms": round(latency_samples / fs * 1000.0, 3),
        "latency_basis": "linear-phase" if linear else "peak",
        "dc_gain_db": round(_db(abs(float(np.sum(h)))), 3),
        "peak_gain_db": round(resp.fir_peak_gain_db(h, fs), 3),
        "max_abs_tap": float(np.max(np.abs(h))),
    }


def _sig(values: np.ndarray, digits: int = 7) -> list[float]:
    return [float(f"{v:.{digits}g}") for v in values]


def preview(taps: np.ndarray | list[float], fs: float = resp.FS, *, n_points: int = PREVIEW_POINTS) -> dict[str, Any]:
    """Analysis plus the impulse and its frequency response (float32 taps, as the device holds them).

    ``excess_phase_deg`` is the phase with the latency estimate removed: flat for a linear-phase FIR, where the raw
    phase just wraps around.
    """
    h = np.asarray(taps, dtype=np.float64)
    info = analyze(h, fs)
    freqs = resp.freq_grid(PREVIEW_F_MIN, PREVIEW_F_MAX, n_points)
    tf = resp.fir_tf(h, freqs, fs)
    r = resp.Response.from_tf(tf, freqs, fs)
    with np.errstate(invalid="ignore"):
        excess = np.angle(tf.h * np.exp(1j * 2 * np.pi * freqs / fs * info["latency_samples"]))
    excess = np.where(np.abs(tf.h) == 0, 0.0, excess)
    return {
        **info,
        "sample_rate": fs,
        "impulse": {
            "t_ms": [round(k / fs * 1000.0, 6) for k in range(len(h))],
            "values": _sig(h.astype(np.float32).astype(np.float64)),
        },
        "response": {
            "freqs": [float(f"{f:.6g}") for f in freqs],
            "magnitude_db": [round(float(v), 3) for v in r.magnitude_db],
            "phase_deg": [round(float(v), 2) for v in r.phase_deg],
            "excess_phase_deg": [round(float(v), 2) for v in np.degrees(excess)],
        },
    }


def blob_json(blob: FirBlob) -> dict[str, Any]:
    return {
        "id": blob.id,
        "name": blob.name,
        "n_taps": blob.n_taps,
        "sha256": blob.sha256,
        "created_at": blob.created_at,
    }


# --- blobs --------------------------------------------------------------------------------------------------------


def parse_upload(data: bytes, profile: DeviceProfile, *, name: str | None = None) -> np.ndarray:
    """Taps from an uploaded WAV, checked against the profile; `FirError` with the reason otherwise."""
    label = name or "upload"
    if len(data) > MAX_UPLOAD_BYTES:
        raise FirError("invalid WAV file", [f"{label}: {len(data)} bytes is too large for a FIR impulse"])
    try:
        return read_fir_taps(data, sample_rate=profile.sample_rate, max_taps=profile.fir_max_taps)
    except WavError as exc:
        raise FirError("invalid WAV file", [f"{label}: {exc}"]) from None


async def upload(session: DeviceSession, data: bytes, *, name: str | None = None) -> tuple[FirBlob, np.ndarray]:
    """Validate and store an uploaded impulse. The desired state is not changed."""
    require_fir(session.profile)
    repo = _repo(session)
    taps = parse_upload(data, session.profile, name=name)
    blob = await repo.put_fir_blob(taps, name=name)
    return blob, taps


async def get_blob(session: DeviceSession, blob_id: str) -> FirBlob:
    blob = await _repo(session).get_fir_blob(blob_id)
    if blob is None or blob.taps is None:
        raise FirBlobNotFound(f"no stored FIR filter {blob_id!r}")
    return blob


async def list_blobs(session: DeviceSession) -> list[dict[str, Any]]:
    return [blob_json(b) for b in await _repo(session).list_fir_blobs()]


# --- state and edits ----------------------------------------------------------------------------------------------


async def fir_state(session: DeviceSession, preset: int, channel: int, *, with_preview: bool = True) -> dict[str, Any]:
    """Desired FIR of one output, its stored blob (with a preview) and the sync status of its paths."""
    path = check_output(session.profile, channel)
    doc = await session.get_desired(preset)
    fir = doc.outputs[channel].fir
    blob_info: dict[str, Any] | None = None
    blob_preview: dict[str, Any] | None = None
    missing = False
    if fir.blob_id is not None and session.repo is not None:
        blob = await session.repo.get_fir_blob(fir.blob_id)
        if blob is None or blob.taps is None:
            missing = True
        else:
            blob_info = blob_json(blob)
            if with_preview:
                blob_preview = preview(blob.taps, session.profile.sample_rate)
    status = session.sync_status(preset)
    return {
        "serial": session.serial,
        "preset": preset,
        "channel": channel,
        "path": path,
        "supported": session.profile.fir_supported,
        "max_taps": session.profile.fir_max_taps,
        "sample_rate": session.profile.sample_rate,
        "bypass": fir.bypass,
        "blob_id": fir.blob_id,
        "blob": blob_info,
        "blob_missing": missing,
        "preview": blob_preview,
        "sync_status": {p: s for p, s in status.items() if p.startswith(f"{path}.")},
    }


async def _edit(session: DeviceSession, preset: int, channel: int, fields: dict[str, Any], **kw: Any) -> EditResult:
    return await session.edit(preset, {"outputs": {str(channel): {"fir": fields}}}, **kw)


async def load(
    session: DeviceSession,
    preset: int,
    channel: int,
    blob_id: str,
    *,
    confirm: bool = False,
    enable: bool | None = None,
    **edit_kwargs: Any,
) -> EditResult:
    """Point the output's FIR at a stored blob (and optionally enable/bypass it). A new blob needs ``confirm``: the
    device mutes while it loads the taps. The session checks the confirmation and the FIR's peak gain against the
    boost limit, as for any other edit that touches the FIR."""
    require_fir(session.profile)
    check_output(session.profile, channel)
    blob = await get_blob(session, blob_id)
    if blob.n_taps > session.profile.fir_max_taps:
        raise FirError(f"{blob_id} has {blob.n_taps} taps; the device takes at most {session.profile.fir_max_taps}")
    fields: dict[str, Any] = {"blob_id": blob.id}
    if enable is not None:
        fields["bypass"] = not enable
    return await _edit(session, preset, channel, fields, confirm=confirm, **edit_kwargs)


async def clear(
    session: DeviceSession, preset: int, channel: int, *, confirm: bool = False, **edit_kwargs: Any
) -> EditResult:
    """Bypass the FIR and clear its taps (``coefficients: []``). Needs ``confirm`` when there are taps to clear."""
    require_fir(session.profile)
    check_output(session.profile, channel)
    current = (await session.get_desired(preset)).outputs[channel].fir
    if current.blob_id is not None and not confirm:
        raise ConfirmationRequired(CLEAR_WARNING)
    return await _edit(session, preset, channel, {"blob_id": None, "bypass": True}, confirm=confirm, **edit_kwargs)


async def set_bypass(session: DeviceSession, preset: int, channel: int, bypass: bool, **edit_kwargs: Any) -> EditResult:
    """Enable or bypass the FIR. Enabling one without taps is refused."""
    require_fir(session.profile)
    path = check_output(session.profile, channel)
    current = (await session.get_desired(preset)).outputs[channel].fir
    if not bypass and current.blob_id is None:
        raise FirError("nothing to enable", [f"{path}: load a FIR filter first; an empty FIR passes no audio"])
    return await _edit(session, preset, channel, {"bypass": bypass}, **edit_kwargs)


# --- capability toggle --------------------------------------------------------------------------------------------


def fir_experimental(settings: Settings) -> bool:
    return bool(getattr(settings, "fir_experimental", False))


def _set_flag(settings: Settings, enabled: bool) -> None:
    if "fir_experimental" in type(settings).model_fields:
        settings.fir_experimental = enabled  # type: ignore[attr-defined]


def apply_fir_capability(sessions: Iterable[DeviceSession], enabled: bool) -> list[int]:
    """Give each session a profile with FIR support on (or back to the model's own). Returns the changed serials."""
    changed = []
    for session in sessions:
        base = PROFILES.get(session.profile.product_name)
        target = enabled or bool(base and base.fir_supported)
        if session.profile.fir_supported == target:
            continue
        profile = session.profile.model_copy(update={"fir_supported": target})
        session.profile = profile
        session.writer.set_profile(profile)
        session.log_event("info", f"experimental FIR {'enabled' if target else 'disabled'}", kind="settings")
        changed.append(session.serial)
    return changed


async def restore_fir_capability(repo: Repo | None, settings: Settings, sessions: Iterable[DeviceSession] = ()) -> bool:
    """At startup: the stored toggle (else ``settings.fir_experimental``) onto the settings and any sessions."""
    stored = await repo.get_setting(FIR_SETTING_KEY) if repo is not None else None
    enabled = bool(stored) if stored is not None else fir_experimental(settings)
    _set_flag(settings, enabled)
    apply_fir_capability(sessions, enabled)
    return enabled


async def set_fir_capability(
    enabled: bool, *, settings: Settings, sessions: Iterable[DeviceSession], repo: Repo | None
) -> list[int]:
    """Persist and apply the experimental FIR toggle. Returns the serials whose profile changed."""
    if repo is not None:
        await repo.set_setting(FIR_SETTING_KEY, enabled)
    _set_flag(settings, enabled)
    return apply_fir_capability(sessions, enabled)
