"""FIR routes on a real session and the stateful fake daemon: capability gate, upload, load/clear/bypass ordering."""

import json

import numpy as np
import pytest

from app.formats.wav import write_wav

from ..conftest import SERIAL
from ..session.conftest import settle

BASE = f"/api/v1/devices/{SERIAL}/presets/0/outputs/2/fir"
TAPS = np.r_[0.5, 0.25, np.zeros(62)].astype(np.float32)


def fir_writes(fake) -> list[dict]:
    return [o["fir"] for o in fake.outputs_written() if "fir" in o]


def wav_file(taps=TAPS, name="impulse.wav", **kwargs):
    return {"file": (name, write_wav(taps, **kwargs), "audio/wav")}


@pytest.fixture
async def enabled(api):
    response = await api.put("/api/v1/fir/capability", json={"enabled": True})
    assert response.status_code == 200, response.text
    return response.json()


async def upload(api, **kwargs) -> dict:
    response = await api.post(BASE, files=wav_file(**kwargs))
    assert response.status_code == 200, response.text
    return response.json()


async def test_everything_but_reads_is_refused_while_fir_is_off(api, fake):
    state = (await api.get(BASE)).json()
    assert state["supported"] is False and state["bypass"] is True and state["sync_status"] == {}
    assert (await api.get("/api/v1/fir/capability")).json() == {"enabled": False, "devices": {str(SERIAL): False}}
    for response in (
        await api.post(BASE, files=wav_file()),
        await api.post(f"{BASE}/load?confirm=true", json={"blob_id": "x"}),
        await api.post(f"{BASE}/clear?confirm=true"),
        await api.put(BASE, json={"bypass": False}),
    ):
        assert response.status_code == 409
        assert response.json()["error"] == "fir_disabled"
    assert fake.configs == []


async def test_capability_toggle_is_persisted(api, enabled, session, repo, settings):
    assert enabled == {"enabled": True, "changed": [SERIAL], "devices": {str(SERIAL): True}}
    assert await repo.get_setting("fir.experimental") is True
    assert (await api.get("/api/v1/fir/capability")).json()["enabled"] is True
    state = (await api.get(BASE)).json()
    assert state["supported"] is True
    assert set(state["sync_status"]) == {f"{state['path']}.bypass", f"{state['path']}.blob_id"}
    off = await api.put("/api/v1/fir/capability", json={"enabled": False})
    assert off.json()["devices"] == {str(SERIAL): False} and not session.profile.fir_supported


async def test_upload_stores_and_previews_without_writing(api, enabled, session, fake):
    data = await upload(api)
    assert data["blob"]["n_taps"] == 64 and data["blob"]["name"] == "impulse.wav"
    preview = data["preview"]
    assert preview["n_taps"] == 64 and preview["peak_index"] == 0 and preview["latency_basis"] == "peak"
    assert preview["impulse"]["values"][:2] == [0.5, 0.25]
    assert len(preview["response"]["freqs"]) == 256
    # Uploading only stores the impulse.
    assert (await api.get(BASE)).json()["blob_id"] is None
    await settle(session)
    assert fake.configs == []
    # Same content -> same blob.
    assert (await upload(api, name="again.wav"))["blob"]["id"] == data["blob"]["id"]
    blobs = (await api.get(f"/api/v1/devices/{SERIAL}/fir/blobs")).json()
    assert [b["id"] for b in blobs] == [data["blob"]["id"]]
    one = (await api.get(f"/api/v1/devices/{SERIAL}/fir/blobs/{data['blob']['id']}")).json()
    assert one["preview"]["n_taps"] == 64
    assert (await api.get(f"/api/v1/devices/{SERIAL}/fir/blobs/nope")).status_code == 404


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"sample_rate": 48_000}, "must be 96 kHz"),
        ({"taps": [[0.5, 0.5]], "channels": 2}, "must be mono"),
        ({"taps": np.r_[1.0, np.zeros(4096)]}, "at most 4096"),
        ({"taps": [0.0, 0.0]}, "all zeros"),
    ],
)
async def test_bad_uploads_are_refused(api, enabled, kwargs, needle):
    response = await api.post(BASE, files=wav_file(**kwargs))
    assert response.status_code == 422
    assert response.json()["error"] == "invalid" and needle in response.json()["problems"][0]


async def test_not_a_wav(api, enabled):
    response = await api.post(BASE, files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 422 and "not a WAV file" in response.json()["problems"][0]


async def test_load_needs_confirmation_then_sends_taps_alone(api, enabled, session, fake):
    blob = (await upload(api))["blob"]["id"]
    refused = await api.post(f"{BASE}/load", json={"blob_id": blob})
    assert refused.status_code == 409 and refused.json()["error"] == "confirmation_required"
    assert "audio drops out" in refused.json()["detail"]
    loaded = await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob})
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["device_paths"] == ["outputs.2.fir.blob_id"]
    await settle(session)
    [body] = [b for b in fake.configs if "fir" in b["outputs"][0]]
    assert body == {"outputs": [{"index": 2, "fir": {"coefficients": TAPS.astype(float).tolist()}}]}
    state = (await api.get(BASE)).json()
    assert state["blob"]["id"] == blob and state["preview"]["n_taps"] == 64
    assert state["sync_status"]["outputs.2.fir.blob_id"] == "synced"


async def test_load_and_enable_unbypasses_after_the_taps(api, enabled, session, fake):
    blob = (await upload(api))["blob"]["id"]
    response = await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob, "enable": True})
    assert response.status_code == 200
    await settle(session)
    writes = fir_writes(fake)
    assert [list(w) for w in writes] == [["coefficients"], ["bypass"]]
    assert writes[1] == {"bypass": False}


async def test_upload_with_load(api, enabled, session, fake, repo):
    refused = await api.post(f"{BASE}?load=true", files=wav_file())
    assert refused.status_code == 409 and await repo.list_fir_blobs() == []
    response = await api.post(f"{BASE}?load=true&confirm=true&enable=false", files=wav_file())
    assert response.status_code == 200, response.text
    assert response.json()["device_paths"] == ["outputs.2.fir.blob_id"]
    await settle(session)
    assert len(fir_writes(fake)) == 1


async def test_enable_and_bypass(api, enabled, session, fake):
    refused = await api.put(BASE, json={"bypass": False})
    assert refused.status_code == 422 and "load a FIR filter first" in refused.json()["problems"][0]
    blob = (await upload(api))["blob"]["id"]
    await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob})
    await settle(session)
    on = await api.put(BASE, json={"bypass": False})
    assert on.status_code == 200 and on.json()["fir"]["bypass"] is False
    await settle(session)
    assert fir_writes(fake)[-1] == {"bypass": False}
    off = await api.put(BASE, json={"bypass": True})
    assert off.status_code == 200
    await settle(session)
    assert fir_writes(fake)[-1] == {"bypass": True}


async def test_clear_bypasses_first_then_clears(api, enabled, session, fake):
    assert (await api.post(f"{BASE}/clear")).status_code == 200  # nothing loaded: no confirm needed, nothing changes
    blob = (await upload(api))["blob"]["id"]
    await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob, "enable": True})
    await settle(session)
    before = len(fir_writes(fake))
    refused = await api.post(f"{BASE}/clear")
    assert refused.status_code == 409 and refused.json()["error"] == "confirmation_required"
    cleared = await api.post(f"{BASE}/clear?confirm=true")
    assert cleared.status_code == 200 and cleared.json()["fir"]["blob_id"] is None
    await settle(session)
    assert fir_writes(fake)[before:] == [{"bypass": True}, {"coefficients": []}]


async def test_load_unknown_blob_and_bad_output(api, enabled):
    response = await api.post(f"{BASE}/load?confirm=true", json={"blob_id": "missing"})
    assert response.status_code == 404 and response.json()["error"] == "fir_blob_not_found"
    bad = await api.post(f"/api/v1/devices/{SERIAL}/presets/0/outputs/7/fir/load?confirm=true", json={"blob_id": "x"})
    assert bad.status_code == 422
    assert (await api.get(f"/api/v1/devices/{SERIAL}/presets/0/outputs/7/fir")).status_code == 422


async def test_load_warning_shows_the_peak_gain(api, enabled):
    blob = (await upload(api))["blob"]["id"]
    refused = await api.post(f"{BASE}/load", json={"blob_id": blob})
    assert refused.status_code == 409 and "(output 3: peak gain -2.5 dB)" in refused.json()["detail"]


async def test_a_loud_fir_is_never_enabled(api, enabled, session, fake):
    loud = np.r_[100.0, np.zeros(63)].astype(np.float32)  # +40 dB, above the 24 dB boost limit
    data = await upload(api, taps=loud)
    assert data["preview"]["peak_gain_db"] == 40.0
    blob = data["blob"]["id"]
    refused = await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob, "enable": True})
    assert refused.status_code == 422 and "boosts 40.0 dB" in refused.json()["problems"][0]
    # Loading it bypassed is allowed; enabling it later is not, on any path.
    assert (await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob})).status_code == 200
    assert (await api.put(f"{BASE}?confirm=true", json={"bypass": False})).status_code == 422
    patch = {"outputs": {"2": {"fir": {"bypass": False}}}}
    assert (await api.patch(f"/api/v1/devices/{SERIAL}/presets/0?confirm=true", json=patch)).status_code == 422
    await settle(session)
    assert {"bypass": False} not in fir_writes(fake)


async def test_enabling_a_fir_that_raises_the_level_needs_confirmation(api, enabled, session, fake):
    blob = (await upload(api, taps=np.r_[3.0, np.zeros(63)].astype(np.float32)))["blob"]["id"]  # +9.5 dB
    await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob})
    refused = await api.put(BASE, json={"bypass": False})
    assert refused.status_code == 409 and "outputs.2.fir peak by 9.54 dB" in refused.json()["detail"]
    assert (await api.put(f"{BASE}?confirm=true", json={"bypass": False})).status_code == 200


async def test_generic_patch_follows_the_fir_rules(api, enabled, session, fake):
    url = f"/api/v1/devices/{SERIAL}/presets/0"
    blob = (await upload(api))["blob"]["id"]
    load = {"outputs": {"2": {"fir": {"blob_id": blob, "bypass": False}}}}
    refused = await api.patch(url, json=load)
    assert refused.status_code == 409 and "audio drops out" in refused.json()["detail"]
    empty = await api.patch(url, json={"outputs": {"1": {"fir": {"bypass": False}}}})
    assert empty.status_code == 422 and "load a FIR filter first" in empty.json()["problems"][0]
    missing = await api.patch(f"{url}?confirm=true", json={"outputs": {"1": {"fir": {"blob_id": "nope"}}}})
    assert missing.status_code == 422 and "no stored FIR filter 'nope'" in missing.json()["problems"][0]
    await settle(session)
    assert fir_writes(fake) == []
    assert (await api.patch(f"{url}?confirm=true", json=load)).status_code == 200
    clear = {"outputs": {"2": {"fir": {"blob_id": None, "bypass": True}}}}
    refused = await api.patch(url, json=clear)
    assert refused.status_code == 409 and refused.json()["detail"].startswith("clearing a FIR filter")


async def test_failed_taps_are_never_enabled(api, enabled, session, fake):
    session.writer.max_attempts = 2
    session.writer.backoff_min_s = session.writer.backoff_max_s = 0.01
    fake.fail_when = lambda body: 500 if "coefficients" in json.dumps(body) else None
    blob = (await upload(api))["blob"]["id"]
    response = await api.post(f"{BASE}/load?confirm=true", json={"blob_id": blob, "enable": True})
    assert response.status_code == 200
    await settle(session)
    assert fake.rejected and all("coefficients" in json.dumps(body) for body in fake.rejected)
    sent = [o["fir"] for body in fake.configs + fake.rejected for o in body["outputs"] if "fir" in o]
    assert {"bypass": False} not in sent
    status = (await api.get(BASE)).json()["sync_status"]
    assert status["outputs.2.fir.blob_id"] in ("failed", "unknown")
    assert status["outputs.2.fir.bypass"] != "synced"
