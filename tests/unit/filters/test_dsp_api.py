"""POST /dsp/design, POST /dsp/recognize and the preset response endpoints."""

import math

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import dsp, filters
from app.dsp import design as d
from app.sync.session import DeviceSession, SessionManager
from tests.unit.dsp.vectors_gen import GOLDEN_HP_20, GOLDEN_LP_80

from ..conftest import SERIAL

BASE = f"/api/v1/devices/{SERIAL}/presets"
KEYS = ("b0", "b1", "b2", "a1", "a2")
LP = dict(zip(KEYS, GOLDEN_LP_80, strict=True))
HP = dict(zip(KEYS, GOLDEN_HP_20, strict=True))
FLAT = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}
LR4_LP_80 = {"kind": "lp", "family": "lr", "order": 4, "freq": 80}
LR4_HP_20 = {"kind": "hp", "family": "lr", "order": 4, "freq": 20}
# docs 04 §8.1: LR4 LP 80 + LR4 HP 20, magnitude in dB.
SHAKER_DB = {10: -24.6, 20: -6.1, 40: -1.1, 60: -2.5, 80: -6.1, 120: -15.7, 200: -32.1}


@pytest.fixture
async def stateless():
    app = FastAPI()
    app.include_router(dsp.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


@pytest.fixture
async def api(session, client, bus, settings):
    app = FastAPI()
    manager = SessionManager(client, bus, settings)
    manager.sessions[SERIAL] = session
    app.state.manager = manager
    for module in (dsp, filters):
        app.include_router(module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


# --- POST /dsp/design ---------------------------------------------------------------------------------------------


async def test_design_peaking(stateless):
    body = {"filter": {"type": "peaking", "freq": 1000, "q": 1, "gain_db": 6}, "f_min": 1000, "f_max": 2000}
    response = await stateless.post("/api/v1/dsp/design", json=body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["biquads"] == [d.peaking(1000, 1, 6).model_dump()]
    assert data["label"] == "PEQ 1 kHz Q 1 +6.0 dB" and data["biquads_used"] == 1
    curve = data["response"]
    assert len(curve["freqs"]) == 512 and curve["freqs"][0] == pytest.approx(1000)
    assert curve["magnitude_db"][0] == pytest.approx(6.0, abs=0.01)
    assert set(curve) >= {"phase_deg", "phase_unwrapped_deg", "group_delay_ms", "warnings", "float32_deviation_db"}


async def test_design_crossover_matches_golden(stateless):
    response = await stateless.post("/api/v1/dsp/design", json={"crossover": LR4_LP_80, "view": "sub"})
    data = response.json()
    assert data["biquads"] == [LP, LP, FLAT, FLAT]
    assert data["label"] == "LR4 LP 80 Hz" and data["biquads_used"] == 2
    assert data["response"]["freqs"][0] == pytest.approx(5) and data["response"]["freqs"][-1] == pytest.approx(300)

    three = await stateless.post("/api/v1/dsp/design", json={"crossover": LR4_LP_80, "n_biquads": 3, "points": 16})
    assert len(three.json()["biquads"]) == 3 and len(three.json()["response"]["freqs"]) == 16


@pytest.mark.parametrize("gain_db", [1e5, 1e6, -1e6])
async def test_design_with_absurd_gain_is_a_422(stateless, gain_db):
    for spec in ({"type": "gain", "gain_db": gain_db}, {"type": "peaking", "freq": 1000, "q": 1, "gain_db": gain_db}):
        response = await stateless.post("/api/v1/dsp/design", json={"filter": spec})
        assert response.status_code == 422, response.text


async def test_design_raw_biquads(stateless):
    body = {"biquads": [LP, LP, list(HP.values()), HP], "f_min": 80, "f_max": 160, "points": 2}
    data = (await stateless.post("/api/v1/dsp/design", json=body)).json()
    assert data["label"] is None and data["biquads_used"] == 4
    assert data["response"]["magnitude_db"][0] == pytest.approx(SHAKER_DB[80], abs=0.1)
    assert data["response"]["warnings"] == []  # float32 rounding matters only near 10 Hz
    full = (await stateless.post("/api/v1/dsp/design", json={"biquads": body["biquads"]})).json()
    assert "float32" in full["response"]["warnings"][0]


async def test_design_float32_flag(stateless):
    body = {"biquads": [LP, LP, HP, HP], "f_min": 10, "f_max": 11, "points": 2}
    f32 = (await stateless.post("/api/v1/dsp/design", json=body)).json()["response"]["magnitude_db"][0]
    f64 = (await stateless.post("/api/v1/dsp/design", json={**body, "float32": False})).json()
    assert f64["response"]["magnitude_db"][0] == pytest.approx(SHAKER_DB[10], abs=0.1)
    assert abs(f32 - f64["response"]["magnitude_db"][0]) > 0.05


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"crossover": {"kind": "bp", "family": "lr", "order": 8, "freq": 20, "freq_hi": 80}}, "needs 8 biquads"),
        ({"crossover": LR4_LP_80, "n_biquads": 1}, "needs 2 biquads"),
        ({"filter": {"type": "lp2", "freq": 48_000, "q": 0.7}}, "Nyquist"),
        ({"biquads": [[1, 0, 0, 1.5, -0.2]]}, "biquads.0: unstable"),
        ({"filter": {"type": "peaking", "freq": 100, "q": 1, "gain_db": 1}, "f_min": 300, "f_max": 200}, "range"),
        ({"filter": {"type": "peaking", "freq": 100, "q": 1, "gain_db": 1}, "view": "sub", "f_min": 5}, "not both"),
        ({"filter": {"type": "passthrough"}, "f_max": 60_000}, "range"),
    ],
)
async def test_design_rejections(stateless, body, message):
    response = await stateless.post("/api/v1/dsp/design", json=body)
    assert response.status_code == 422
    assert message in response.json()["detail"] and response.json()["error"] == "invalid"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"filter": {"type": "passthrough"}, "crossover": LR4_LP_80},
        {"filter": {"type": "peaking", "freq": 100}},
        {"biquads": [[1, 0, 0]]},
        {"filter": {"type": "passthrough"}, "points": 1},
        {"filter": {"type": "passthrough"}, "extra": 1},
        {"biquads": [{"b0": "nan", "b1": 0, "b2": 0, "a1": 0, "a2": 0}]},
    ],
)
async def test_design_request_validation(stateless, body):
    assert (await stateless.post("/api/v1/dsp/design", json=body)).status_code == 422


# --- POST /dsp/recognize ------------------------------------------------------------------------------------------


async def test_recognize(stateless):
    peq = d.peaking(42, 4, -3.5).model_dump()
    response = await stateless.post("/api/v1/dsp/recognize", json={"biquads": [peq, [0.9, 0.1, 0.05, 0.3, -0.2]]})
    data = response.json()
    assert [f["label"] for f in data["filters"]] == ["PEQ 42 Hz Q 4 -3.5 dB", "Raw"]
    assert "crossover" not in data

    group = (await stateless.post("/api/v1/dsp/recognize", json={"biquads": [HP, HP, FLAT], "group": True})).json()
    assert group["crossover"]["label"] == "LR4 HP 20 Hz"
    assert group["crossover"]["design"]["family"] == "lr" and group["crossover"]["design"]["order"] == 4
    custom = (await stateless.post("/api/v1/dsp/recognize", json={"biquads": [peq], "group": True})).json()
    assert custom["crossover"] == {"design": None, "label": "Custom"}

    bad = await stateless.post("/api/v1/dsp/recognize", json={"biquads": [[1, 0, 0, 0, 1]]})
    assert bad.status_code == 422 and "unstable" in bad.json()["detail"]
    assert (await stateless.post("/api/v1/dsp/recognize", json={"biquads": []})).status_code == 422


# --- GET …/presets/{p}/response -----------------------------------------------------------------------------------


async def shaker(api) -> None:
    for g, design in ((0, LR4_LP_80), (1, LR4_HP_20)):
        response = await api.put(f"{BASE}/0/outputs/0/crossover/{g}", json={"design": design, "bypass": False})
        assert response.status_code == 200, response.text


async def test_output_response_matches_golden_table(api):
    await shaker(api)
    for freq, expected in SHAKER_DB.items():
        params = {"outputs": "0", "range": f"{freq}-{freq * 2}", "points": 2}
        exact = await api.get(f"{BASE}/0/response", params={**params, "float32": "false"})
        assert exact.status_code == 200, exact.text
        assert exact.json()["sum"]["magnitude_db"][0] == pytest.approx(expected, abs=0.06), freq
        # The default evaluates what the device holds: float32 moves it by up to ~0.2 dB near 10 Hz.
        device = (await api.get(f"{BASE}/0/response", params=params)).json()
        assert device["sum"]["magnitude_db"][0] == pytest.approx(expected, abs=0.25), freq


async def test_response_defaults_and_channels(api, session):
    await shaker(api)
    await session.edit(0, {"outputs": {"2": {"mute": True}, "3": {"mute": True}, "1": {"invert": True}}})
    data = (await api.get(f"{BASE}/0/response", params={"channels": "true", "range": "sub"})).json()
    assert data["outputs"] == [0, 1] and data["inputs"] == [0]
    assert data["range"] == [pytest.approx(5), pytest.approx(300)] and len(data["sum"]["freqs"]) == 512
    assert set(data["channels"]) == {"0", "1"}
    assert data["master_db"] is None
    assert any("float32" in w for w in data["warnings"])
    # Output 2 (default routing: fed by input 1 = index 1) is silent from input 0; output 1 is flat and inverted.
    out1 = (await api.get(f"{BASE}/0/response", params={"outputs": "1", "inputs": "all", "range": "100-200"})).json()
    assert out1["inputs"] == [0, 1]
    assert out1["sum"]["magnitude_db"][0] == pytest.approx(0, abs=1e-6)
    assert abs(out1["sum"]["phase_deg"][0]) == pytest.approx(180, abs=1e-3)
    silent = (await api.get(f"{BASE}/0/response", params={"outputs": "1", "inputs": "0"})).json()
    assert max(silent["sum"]["magnitude_db"]) == -200


async def test_response_with_master_volume(api, session, fake):
    plain = (await api.get(f"{BASE}/0/response", params={"outputs": "2", "range": "100-200", "points": 2})).json()
    with_master = await api.get(
        f"{BASE}/0/response", params={"outputs": "2", "range": "100-200", "points": 2, "master": "true"}
    )
    data = with_master.json()
    assert data["master_db"] == fake.master["volume"] == -20.0
    assert plain["sum"]["magnitude_db"][0] == pytest.approx(0, abs=1e-9)
    assert data["sum"]["magnitude_db"][0] == pytest.approx(-20.0)
    assert data["warnings"] == []

    session.monitor.update_master(session.master.model_copy(update={"mute": True}))
    muted = (await api.get(f"{BASE}/0/response", params={"outputs": "2", "points": 2, "master": "true"})).json()
    assert "the master is muted (not shown in the plot)" in muted["warnings"]


async def test_response_master_unknown(api, monkeypatch):
    monkeypatch.setattr(DeviceSession, "master", property(lambda _self: None))
    response = await api.get(f"{BASE}/0/response", params={"master": "true"})
    assert response.status_code == 503


async def test_response_fir_taps(api, session, repo):
    await session.edit(0, {"outputs": {"0": {"fir": {"bypass": False}}}})
    data = (await api.get(f"{BASE}/0/response", params={"outputs": "0", "points": 8})).json()
    assert any("FIR is active but its taps are unknown" in w for w in data["warnings"])

    blob = await repo.put_fir_blob([0.5, 0.0], name="half")
    await session.edit(0, {"outputs": {"0": {"fir": {"blob_id": blob.id}}}})
    data = (await api.get(f"{BASE}/0/response", params={"outputs": "0", "range": "100-200", "points": 2})).json()
    assert not any("FIR" in w for w in data["warnings"])
    assert data["sum"]["magnitude_db"][0] == pytest.approx(20 * math.log10(0.5), abs=1e-3)


@pytest.mark.parametrize(
    "params",
    [
        {"outputs": "4"},
        {"outputs": "a"},
        {"inputs": "2"},
        {"range": "abc"},
        {"range": "300-200"},
        {"range": "5"},
        {"range": "10-60000"},
        {"points": 1},
    ],
)
async def test_response_rejections(api, params):
    assert (await api.get(f"{BASE}/0/response", params=params)).status_code == 422


async def test_response_unknown_preset(api):
    response = await api.get(f"{BASE}/7/response")
    assert response.status_code == 404 and response.json()["error"] == "unknown_preset"


async def test_input_response(api):
    design = {"type": "peaking", "freq": 1000, "q": 1, "gain_db": 6}
    await api.put(f"{BASE}/0/inputs/1/peq/4", json={"design": design, "bypass": False})
    data = (await api.get(f"{BASE}/0/inputs/1/response", params={"range": "1000-2000", "points": 4})).json()
    assert data["input"] == 1 and data["magnitude_db"][0] == pytest.approx(6.0, abs=0.01)
    assert (await api.get(f"{BASE}/0/inputs/2/response")).status_code == 422
    assert (await api.get(f"{BASE}/0/inputs/0/response", params={"range": "full"})).status_code == 200
    assert (await api.get(f"{BASE}/0/inputs/0/response", params={"range": "9-3"})).status_code == 422
