"""FIR analysis/preview and the experimental capability toggle."""

import numpy as np
import pytest

from app.model.device_profile import M2X4HD
from app.sync import fir

FS = 96_000


def delayed_impulse(k: int, n: int) -> np.ndarray:
    h = np.zeros(n)
    h[k] = 1.0
    return h


def test_linear_phase_latency_is_half_the_length():
    # Docs 05 §4: 2048 taps -> latency ~10.7 ms (linear-phase assumption).
    taps = np.hanning(2048)
    taps /= taps.sum()
    info = fir.analyze(taps, FS)
    assert info["symmetry"] == "symmetric" and info["linear_phase"]
    assert info["latency_samples"] == 1023.5
    assert info["latency_ms"] == pytest.approx(10.661, abs=1e-3)
    assert info["latency_basis"] == "linear-phase"
    assert info["dc_gain_db"] == pytest.approx(0.0, abs=1e-6)
    assert info["length_ms"] == pytest.approx(2048 / 96, abs=1e-3)


def test_antisymmetric_counts_as_linear_phase():
    info = fir.analyze([1.0, 0.0, -1.0], FS)
    assert info["symmetry"] == "antisymmetric" and info["latency_samples"] == 1.0
    assert info["dc_gain_db"] == fir.resp.DB_FLOOR


def test_minimum_phase_latency_is_the_peak():
    taps = delayed_impulse(96, 512) + 0.3 * delayed_impulse(100, 512)
    info = fir.analyze(taps, FS)
    assert info["symmetry"] is None and info["latency_basis"] == "peak"
    assert info["peak_index"] == 96 and info["latency_ms"] == 1.0
    assert info["peak_gain_db"] == pytest.approx(20 * np.log10(1.3), abs=0.01)


def test_preview_shapes_and_excess_phase():
    taps = np.hanning(255)
    p = fir.preview(taps, FS, n_points=64)
    assert len(p["impulse"]["t_ms"]) == len(p["impulse"]["values"]) == 255
    assert p["impulse"]["t_ms"][96] == pytest.approx(1.0)
    r = p["response"]
    assert len(r["freqs"]) == len(r["magnitude_db"]) == len(r["phase_deg"]) == len(r["excess_phase_deg"]) == 64
    assert r["freqs"][0] == 5.0 and r["freqs"][-1] == 24_000.0
    # A symmetric FIR has no phase left once its (N-1)/2 delay is removed: 0, or 180 where its zero-phase amplitude
    # is negative (side lobes).
    excess = r["excess_phase_deg"]
    assert all(min(abs(e), abs(abs(e) - 180)) < 0.01 for e in excess)
    passband = [e for e, f in zip(excess, r["freqs"], strict=True) if f < 300]
    assert passband and max(abs(e) for e in passband) < 0.01
    # A pure impulse at 0 is flat at 0 dB.
    flat = fir.preview([1.0], FS, n_points=16)["response"]
    assert set(flat["magnitude_db"]) == {0.0} and set(flat["phase_deg"]) == {0.0}


def test_parse_upload_maps_wav_errors():
    from app.formats.wav import write_wav

    with pytest.raises(fir.FirError) as info:
        fir.parse_upload(write_wav([1.0], sample_rate=44_100), M2X4HD, name="x.wav")
    assert info.value.problems[0].startswith("x.wav: a FIR impulse must be 96 kHz")
    with pytest.raises(fir.FirError, match="invalid WAV"):
        fir.parse_upload(b"\x00" * (fir.MAX_UPLOAD_BYTES + 1), M2X4HD)


def test_require_fir_and_check_output():
    with pytest.raises(fir.FirDisabled):
        fir.require_fir(M2X4HD)
    fir.require_fir(M2X4HD.model_copy(update={"fir_supported": True}))
    assert fir.check_output(M2X4HD, 3) == "outputs.3.fir"
    with pytest.raises(fir.FirError):
        fir.check_output(M2X4HD, 4)


async def test_capability_toggle_swaps_profiles_and_sync_paths(session):
    assert not session.profile.fir_supported
    assert not any(".fir." in p for p in session.sync_status(0))
    assert fir.apply_fir_capability([session], True) == [session.serial]
    assert session.profile.fir_supported and session.writer.profile is session.profile
    statuses = session.sync_status(0)
    assert statuses["outputs.0.fir.bypass"] == "unknown" and statuses["outputs.0.fir.blob_id"] == "unknown"
    assert fir.apply_fir_capability([session], True) == []  # already on
    assert fir.apply_fir_capability([session], False) == [session.serial]
    assert not any(".fir." in p for p in session.sync_status(0))


async def test_enabling_writes_nothing(session, fake):
    fir.apply_fir_capability([session], True)
    await session.writer.wait_idle(0, timeout=0.5)
    assert fake.configs == []


async def test_restore_and_set_persist(session, repo, settings):
    assert await fir.restore_fir_capability(repo, settings, [session]) is False
    changed = await fir.set_fir_capability(True, settings=settings, sessions=[session], repo=repo)
    assert changed == [session.serial]
    assert await repo.get_setting(fir.FIR_SETTING_KEY) is True
    fir.apply_fir_capability([session], False)
    assert await fir.restore_fir_capability(repo, settings, [session]) is True
    assert session.profile.fir_supported
    assert settings.fir_experimental  # set_fir_capability also updates the live settings
    assert await fir.restore_fir_capability(None, settings, []) is True  # no DB: the settings flag
    settings.fir_experimental = False
    assert await fir.restore_fir_capability(None, settings, []) is False
