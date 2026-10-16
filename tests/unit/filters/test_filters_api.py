"""PUT/GET PEQ bands and crossover groups through the router, on a real session and the stateful fake daemon."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import filters, presets
from app.formats.cli_script import parse_script
from app.model.config_doc import default_config_doc
from app.model.device_profile import M2X4HD
from app.sync.filters import recognize_doc
from app.sync.session import AdoptMode, SessionManager
from tests.unit.dsp.vectors_gen import GOLDEN_HP_20, GOLDEN_LP_80

from ..conftest import SERIAL
from ..session.conftest import settle

PI_NOTES = (Path(__file__).parents[2] / "fixtures" / "pi_notes_commands.txt").read_text()
BASE = f"/api/v1/devices/{SERIAL}/presets"
LR4_LP_80 = {"kind": "lp", "family": "lr", "order": 4, "freq": 80}
LR4_HP_20 = {"kind": "hp", "family": "lr", "order": 4, "freq": 20}
KEYS = ("b0", "b1", "b2", "a1", "a2")
LP = dict(zip(KEYS, GOLDEN_LP_80, strict=True))
HP = dict(zip(KEYS, GOLDEN_HP_20, strict=True))
FLAT = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}


@pytest.fixture
async def api(session, client, bus, settings):
    app = FastAPI()
    manager = SessionManager(client, bus, settings)
    manager.sessions[SERIAL] = session
    app.state.manager = manager
    for module in (presets, filters):
        app.include_router(module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


def crossover_writes(fake) -> list[dict]:
    return [o for o in fake.outputs_written() if "crossover" in o]


async def test_put_crossover_design_writes_coefficients_and_bypass(api, session, fake):
    response = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"design": LR4_LP_80, "bypass": False})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["group"]["label"] == "LR4 LP 80 Hz" and data["group"]["biquads_used"] == 2
    assert data["group"]["compiled"] == [LP, LP, FLAT, FLAT]
    assert "outputs.0.crossover.0.design" in data["changed"]
    assert set(data["device_paths"]) == {
        "outputs.0.crossover.0.compiled.0",
        "outputs.0.crossover.0.compiled.1",
        "outputs.0.crossover.0.bypass",
    }
    await settle(session)
    # Coefficients and the un-bypass share one call (the daemon applies crossover coefficients first). Slots 2-3 are
    # pass-through, which the shadow doesn't know yet, so they are pulled in before the un-bypass too.
    [write] = crossover_writes(fake)
    [group] = write["crossover"]
    assert group["index"] == 0 and group["bypass"] is False
    coeffs = {c["index"]: {k: c[k] for k in KEYS} for c in group["coeff"]}
    assert coeffs == {0: LP, 1: LP, 2: FLAT, 3: FLAT}

    sent = len(fake.configs)
    again = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"design": LR4_LP_80})
    assert again.status_code == 200
    assert again.json()["changed"] == [] and again.json()["device_paths"] == []
    await settle(session)
    assert len(fake.configs) == sent


async def test_redesign_after_adopting_shaker_preset_writes_nothing(api, session, fake):
    doc = parse_script(PI_NOTES, M2X4HD).apply(default_config_doc(M2X4HD))
    await session.adopt(AdoptMode.TRUST, preset=0, doc=recognize_doc(doc))
    shown = (await api.get(f"{BASE}/0/outputs/0/crossover/0")).json()
    assert shown["label"] == "LR4 LP 80 Hz" and shown["bypass"] is False
    assert set(shown["sync_status"].values()) == {"synced"}
    assert (await api.get(f"{BASE}/0/outputs/0/crossover/1")).json()["label"] == "LR4 HP 20 Hz"
    assert (await api.get(f"{BASE}/0/outputs/1/crossover/0")).json()["label"] == "Custom"

    for g, design in ((0, LR4_LP_80), (1, LR4_HP_20)):
        response = await api.put(f"{BASE}/0/outputs/0/crossover/{g}", json={"design": design})
        assert response.status_code == 200, response.text
        assert response.json()["changed"] == [] and response.json()["device_paths"] == []
    await settle(session)
    assert fake.configs == []

    # Moving the low-pass to 70 Hz rewrites just its two sections; the group stays enabled.
    response = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"design": {**LR4_LP_80, "freq": 70}})
    assert response.json()["device_paths"] == ["outputs.0.crossover.0.compiled.0", "outputs.0.crossover.0.compiled.1"]
    await settle(session)
    [write] = crossover_writes(fake)
    assert [c["index"] for c in write["crossover"][0]["coeff"]] == [0, 1] and "bypass" not in write["crossover"][0]


async def test_adopt_recognizes_shaker_crossover_and_redesign_is_a_noop(api, session, fake):
    await session.adopt(AdoptMode.TRUST, preset=0, script=PI_NOTES)
    group = (await api.get(f"{BASE}/0/outputs/0/crossover/1")).json()
    assert group["label"] == "LR4 HP 20 Hz"
    response = await api.put(f"{BASE}/0/outputs/0/crossover/1", json={"design": LR4_HP_20})
    data = response.json()
    assert data["changed"] == [] and data["device_paths"] == []
    await settle(session)
    assert fake.configs == []


async def test_put_raw_biquads_are_recognized(api, session, fake):
    response = await api.put(f"{BASE}/0/outputs/2/crossover/1", json={"biquads": [HP, list(HP.values())]})
    assert response.status_code == 200, response.text
    group = response.json()["group"]
    assert group["design"] == {**LR4_HP_20, "freq": 20.0, "freq_hi": None, "bessel_norm": "phase"}
    assert group["label"] == "LR4 HP 20 Hz" and group["compiled"] == [HP, HP, FLAT, FLAT]
    assert group["bypass"] is True  # not enabled unless asked

    custom = await api.put(f"{BASE}/0/outputs/2/crossover/1", json={"biquads": [[0.9, 0.1, 0.05, 0.3, -0.2]]})
    assert custom.json()["group"]["label"] == "Custom" and custom.json()["group"]["design"] is None
    flat = await api.put(f"{BASE}/0/outputs/2/crossover/1", json={"biquads": []})
    assert flat.json()["group"]["compiled"] == [FLAT] * 4


async def test_crossover_rejections(api, fake):
    unstable = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"biquads": [LP, [1, 0, 0, 1.5, -0.2]]})
    assert unstable.status_code == 422
    body = unstable.json()
    assert body["error"] == "invalid" and body["problems"][0].startswith("outputs.0.crossover.0.compiled.1: unstable")

    too_big = {"kind": "bp", "family": "lr", "order": 8, "freq": 20, "freq_hi": 80}
    response = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"design": too_big})
    assert response.status_code == 422 and "needs 8 biquads, the group has 4" in response.json()["problems"][0]
    response = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"biquads": [FLAT] * 5})
    assert response.status_code == 422 and "5 given, the group holds 4" in response.json()["problems"][0]

    nyquist = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"design": {**LR4_LP_80, "freq": 50_000}})
    assert nyquist.status_code == 422 and "Nyquist" in nyquist.json()["problems"][0]

    odd_lr = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"design": {**LR4_LP_80, "order": 3}})
    assert odd_lr.status_code == 422  # model validation

    assert (await api.put(f"{BASE}/0/outputs/0/crossover/2", json={"bypass": False})).status_code == 422
    assert (await api.put(f"{BASE}/0/outputs/4/crossover/0", json={"bypass": False})).status_code == 422
    assert (await api.put(f"{BASE}/0/outputs/0/crossover/0", json={})).status_code == 422
    assert (await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"bypas": True})).status_code == 422
    assert (await api.put(f"{BASE}/9/outputs/0/crossover/0", json={"bypass": True})).status_code == 404
    assert (await api.get(f"{BASE}/0/outputs/0/crossover/5")).status_code == 422
    assert fake.configs == []


async def test_put_peq_band_splits_unbypass(api, session, fake):
    design = {"type": "peaking", "freq": 42, "q": 4, "gain_db": -3.5}
    response = await api.put(f"{BASE}/0/outputs/1/peq/3", json={"design": design, "bypass": False})
    assert response.status_code == 200, response.text
    band = response.json()["band"]
    assert band["label"] == "PEQ 42 Hz Q 4 -3.5 dB" and band["bypass"] is False
    await settle(session)
    peq_writes = [o["peq"] for o in fake.outputs_written() if "peq" in o]
    # Coefficients first, the un-bypass in a later call (the daemon applies a PEQ's bypass before its coefficients).
    assert len(peq_writes) == 2
    assert peq_writes[0] == [{"index": 3, "coeff": band["compiled"]}]
    assert peq_writes[1] == [{"index": 3, "bypass": False}]

    sent = len(fake.configs)
    again = await api.put(f"{BASE}/0/outputs/1/peq/3", json={"design": design})
    assert again.json()["changed"] == []
    off = await api.put(f"{BASE}/0/outputs/1/peq/3", json={"bypass": True})
    assert off.json()["device_paths"] == ["outputs.1.peq.3.bypass"]
    await settle(session)
    assert len(fake.configs) == sent + 1

    got = (await api.get(f"{BASE}/0/outputs/1/peq/3")).json()
    assert got["path"] == "outputs.1.peq.3" and got["bypass"] is True and got["design"]["type"] == "peaking"
    assert got["sync_status"] == {"outputs.1.peq.3.bypass": "synced", "outputs.1.peq.3.compiled": "synced"}


async def test_put_input_peq_raw(api, session, fake):
    raw = [0.9, 0.1, 0.05, 0.3, -0.2]
    response = await api.put(f"{BASE}/0/inputs/1/peq/0", json={"raw": raw})
    assert response.status_code == 200, response.text
    band = response.json()["band"]
    assert band["design"]["type"] == "raw" and list(band["compiled"].values()) == raw and band["label"] == "Raw"
    await settle(session)
    assert fake.configs[-1] == {
        "inputs": [{"index": 1, "peq": [{"index": 0, "coeff": dict(zip(KEYS, raw, strict=True))}]}]
    }


async def test_peq_rejections(api, fake):
    response = await api.put(f"{BASE}/0/outputs/0/peq/0", json={"raw": [1, 0, 0, 0, 1]})
    assert response.status_code == 422 and "unstable" in response.json()["problems"][0]
    response = await api.put(f"{BASE}/0/outputs/0/peq/0", json={"raw": [1, 0, 0]})
    assert response.status_code == 422
    response = await api.put(f"{BASE}/0/outputs/0/peq/0", json={"design": {"type": "peaking", "freq": 100}})
    assert response.status_code == 422  # q and gain_db missing
    response = await api.put(
        f"{BASE}/0/outputs/0/peq/0", json={"design": {"type": "passthrough"}, "raw": [1, 0, 0, 0, 0]}
    )
    assert response.status_code == 422 and "not both" in response.json()["detail"]
    assert (await api.put(f"{BASE}/0/outputs/0/peq/10", json={"bypass": False})).status_code == 422
    assert (await api.put(f"{BASE}/0/inputs/2/peq/0", json={"bypass": False})).status_code == 422
    assert (await api.put(f"{BASE}/0/sideways/0/peq/0", json={"bypass": False})).status_code == 422
    assert fake.configs == []


async def test_external_band_is_read_only(api, session):
    await session.edit(0, {"outputs": {"0": {"peq": {"2": {"external": True}}}}})
    response = await api.put(f"{BASE}/0/outputs/0/peq/2", json={"bypass": False})
    assert response.status_code == 422 and "read-only" in response.json()["detail"]


async def test_if_match_and_commit(api, session):
    response = await api.put(f"{BASE}/0/outputs/0/crossover/0", params={"commit": "true"}, json={"design": LR4_LP_80})
    assert response.json()["revision"] == 1
    stale = await api.put(f"{BASE}/0/outputs/0/crossover/0", headers={"If-Match": '"0"'}, json={"bypass": False})
    assert stale.status_code == 412 and stale.json()["error"] == "revision_conflict"
    ok = await api.put(f"{BASE}/0/outputs/0/crossover/0", headers={"If-Match": '"1"'}, json={"bypass": False})
    assert ok.status_code == 200


async def test_inactive_preset_is_staged(api, session, fake):
    response = await api.put(f"{BASE}/2/outputs/0/crossover/1", json={"design": LR4_HP_20, "bypass": False})
    assert response.status_code == 200 and response.json()["summary"]["active"] is False
    await settle(session, 2)
    assert fake.configs == []
    assert session.sync_status(2)["outputs.0.crossover.1.bypass"] == "pending"


# --- boost limit and big-step guard (docs 03 §5) -----------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"design": {"type": "gain", "gain_db": 40}, "bypass": False},
        {"design": {"type": "peaking", "freq": 40, "q": 1, "gain_db": 60}, "bypass": False},
        {"design": {"type": "lt", "lt": {"f0": 60, "q0": 0.7, "fp": 5, "qp": 0.5}}, "bypass": False},
        {"raw": [1000, 0, 0, 0, 0], "bypass": False},
    ],
)
async def test_peq_boost_above_the_limit_is_refused(api, session, fake, body):
    response = await api.put(f"{BASE}/0/outputs/0/peq/0?confirm=true", json=body)
    assert response.status_code == 422, response.text
    assert "more than the 24 dB" in response.json()["problems"][0]
    await settle(session)
    assert fake.configs == []


@pytest.mark.parametrize("gain_db", [120, 1e5, 1e6, -1e6])
async def test_absurd_gains_are_a_422_not_a_500(api, gain_db):
    for design in ({"type": "gain", "gain_db": gain_db}, {"type": "peaking", "freq": 1000, "q": 1, "gain_db": gain_db}):
        response = await api.put(f"{BASE}/0/outputs/0/peq/0", json={"design": design, "bypass": False})
        assert response.status_code == 422, response.text


async def test_big_peq_boost_needs_confirmation(api, session, fake):
    boost = {"design": {"type": "peaking", "freq": 40, "q": 1, "gain_db": 10}, "bypass": False}
    response = await api.put(f"{BASE}/0/outputs/0/peq/0", json=boost)
    assert response.status_code == 409 and "outputs.0.peq.0 peak" in response.json()["detail"]
    assert (await api.get(f"{BASE}/0/outputs/0/peq/0")).json()["bypass"] is True
    assert (await api.put(f"{BASE}/0/outputs/0/peq/0?confirm=true", json=boost)).status_code == 200

    # Small steps and cuts pass; so does bypassing. Un-bypassing a stored boost is a raise again.
    small = {"design": {"type": "peaking", "freq": 40, "q": 1, "gain_db": 14}}
    assert (await api.put(f"{BASE}/0/outputs/0/peq/0", json=small)).status_code == 200
    assert (await api.put(f"{BASE}/0/outputs/0/peq/0", json={"bypass": True})).status_code == 200
    assert (await api.put(f"{BASE}/0/outputs/0/peq/0", json={"bypass": False})).status_code == 409
    cut = {"design": {"type": "peaking", "freq": 40, "q": 1, "gain_db": -20}, "bypass": False}
    assert (await api.put(f"{BASE}/0/outputs/0/peq/1", json=cut)).status_code == 200


async def test_raw_crossover_boost_is_guarded(api):
    boost = [[4.0, 0, 0, 0, 0]]  # +12 dB
    response = await api.put(f"{BASE}/0/outputs/0/crossover/0", json={"biquads": boost, "bypass": False})
    assert response.status_code == 409
    huge = [[100.0, 0, 0, 0, 0]]  # +40 dB
    response = await api.put(f"{BASE}/0/outputs/0/crossover/0?confirm=true", json={"biquads": huge, "bypass": False})
    assert response.status_code == 422


async def test_generic_patch_of_compiled_coefficients_is_guarded(api):
    patch = {"outputs": {"0": {"peq": {"0": {"compiled": {"b0": 100.0}, "bypass": False}}}}}
    response = await api.patch(f"{BASE}/0?confirm=true", json=patch)
    assert response.status_code == 422, response.text


# --- concurrent edits ----------------------------------------------------------------------------------------------


async def test_design_edit_does_not_revert_a_concurrent_bypass(api, session, monkeypatch):
    from app.sync import filters as svc

    on = {"design": {"type": "peaking", "freq": 42, "q": 4, "gain_db": -3}, "bypass": False}
    assert (await api.put(f"{BASE}/0/outputs/1/peq/3", json=on)).status_code == 200
    stale = await svc.get_band(session, 0, "outputs", 1, 3)
    # Another client bypasses the band after this request read it but before its edit runs.
    assert (await api.put(f"{BASE}/0/outputs/1/peq/3", json={"bypass": True})).status_code == 200

    async def read_stale(*_args):
        return stale

    monkeypatch.setattr(svc, "get_band", read_stale)
    await svc.set_peq_band(session, 0, "outputs", 1, 3, design=svc.FilterDesign(type="notch", freq=50, q=2))
    band = session.desired(0).outputs[1].peq[3]
    assert band.bypass is True and band.design.type == "notch"


async def test_bypass_toggle_does_not_revert_a_concurrent_design(api, session, monkeypatch):
    from app.sync import filters as svc

    stale = await svc.get_group(session, 0, 2, 0)
    assert (await api.put(f"{BASE}/0/outputs/2/crossover/0", json={"design": LR4_LP_80})).status_code == 200

    async def read_stale(*_args):
        return stale

    monkeypatch.setattr(svc, "get_group", read_stale)
    await svc.set_crossover_group(session, 0, 2, 0, bypass=False)
    group = session.desired(0).outputs[2].crossover[0]
    assert group.bypass is False and group.compiled == [svc.Biquad(**LP)] * 2 + [svc.PASSTHROUGH] * 2
