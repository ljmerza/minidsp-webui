"""REW preview/import/export through the router, on a real session and the stateful fake daemon."""

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import presets, rew
from app.dsp.design import high_shelf, highpass2, linkwitz_transform, lowpass2, peaking
from app.formats.rew import export_rew, parse_rew
from app.model.filters import PASSTHROUGH
from app.sync.session import SessionManager

from ..conftest import SERIAL
from ..session.conftest import drain, settle

REW_TEXT = (Path(__file__).parents[2] / "fixtures" / "rew-filters.txt").read_text()
FIXTURE = parse_rew(REW_TEXT).biquads
BASE = f"/api/v1/devices/{SERIAL}/presets"
KEYS = ("b0", "b1", "b2", "a1", "a2")
LP = lowpass2(80, 0.7071067811865476)
HP = highpass2(20, 0.7071067811865476)


@pytest.fixture
async def api(session, client, bus, settings):
    app = FastAPI()
    manager = SessionManager(client, bus, settings)
    manager.sessions[SERIAL] = session
    app.state.manager = manager
    for module in (presets, rew):
        app.include_router(module.router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


def peq_entries(fake, channel: int) -> list[tuple[int, dict]]:
    """``(call number, peq entry)`` for every PEQ entry written to ``channel``, in send order."""
    return [
        (n, entry)
        for n, body in enumerate(fake.configs)
        for out in body.get("outputs", [])
        if out["index"] == channel
        for entry in out.get("peq", [])
    ]


def assert_unbypass_after_coefficients(fake, channel: int, bands) -> None:
    entries = peq_entries(fake, channel)
    for k in bands:
        coeff_calls = [n for n, e in entries if e["index"] == k and "coeff" in e]
        unbypass_calls = [n for n, e in entries if e["index"] == k and e.get("bypass") is False]
        assert coeff_calls and unbypass_calls, k
        assert max(unbypass_calls) > min(coeff_calls), k
        # Never in the same call: the daemon applies a PEQ bypass before its coefficients.
        assert not any(e["index"] == k and "coeff" in e and e.get("bypass") is False for _, e in entries), k


async def import_rew(api, preset: int = 0, **body):
    return await api.post(f"{BASE}/{preset}/import/rew", json=body)


async def test_rew_fixture_into_out2_peq_round_trips_exactly(api, session, fake):
    response = await import_rew(api, text=REW_TEXT, bank="outputs.1.peq")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["warnings"] == []
    assert sorted(data["imported"]) == [f"outputs.1.peq.{k}" for k in range(10)]
    assert data["imported"]["outputs.1.peq.0"]["label"].startswith("PEQ 106 Hz")
    assert data["revision"] >= 1  # an import is a commit boundary
    doc = await session.get_desired(0)
    assert [band.compiled for band in doc.outputs[1].peq] == FIXTURE
    assert all(not band.bypass for band in doc.outputs[1].peq)

    assert (await settle(session)).ok
    assert_unbypass_after_coefficients(fake, 1, range(10))
    written = {e["index"]: e["coeff"] for _, e in peq_entries(fake, 1) if "coeff" in e}
    assert [tuple(written[k][c] for c in KEYS) for k in range(10)] == [bq.as_tuple() for bq in FIXTURE]
    assert {s for p, s in session.sync_status(0).items() if p.startswith("outputs.1.peq.")} == {"synced"}

    exported = await api.get(f"{BASE}/0/export/rew", params={"bank": "outputs.1.peq"})
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/plain")
    assert exported.text == REW_TEXT

    # Importing the same file again changes nothing and writes nothing.
    sent = len(fake.configs)
    again = await import_rew(api, text=exported.text, bank="outputs.1.peq")
    assert again.status_code == 200 and again.json()["device_paths"] == []
    await settle(session)
    assert len(fake.configs) == sent


async def test_failed_coefficient_writes_never_unbypass(api, session, fake):
    fake.fail = 500
    response = await import_rew(api, text=REW_TEXT, bank="outputs.1.peq")
    assert response.status_code == 200
    await settle(session)
    assert fake.configs == []
    status = session.sync_status(0)
    assert all(status[f"outputs.1.peq.{k}.bypass"] != "synced" for k in range(10))

    fake.fail = None
    session.writer.kick(0)
    assert (await settle(session)).ok
    assert_unbypass_after_coefficients(fake, 1, range(10))


async def test_import_one_band_and_pick(api, session, fake):
    response = await import_rew(api, text=REW_TEXT, bank="outputs.2.peq", index=4, pick=3)
    assert response.status_code == 200, response.text
    assert list(response.json()["imported"]) == ["outputs.2.peq.4"]
    doc = await session.get_desired(0)
    assert doc.outputs[2].peq[4].compiled == FIXTURE[2]
    assert all(band.compiled == PASSTHROUGH for k, band in enumerate(doc.outputs[2].peq) if k != 4)

    too_many = await import_rew(api, text=REW_TEXT, bank="outputs.2.peq", index=4)
    assert too_many.status_code == 422
    assert too_many.json()["problems"] == ["import into 'all' or pick one filter of the file"]
    out_of_range = await import_rew(api, text=REW_TEXT, bank="outputs.2.peq", index=4, pick=11)
    assert out_of_range.status_code == 422 and "the file has 10 filter(s)" in out_of_range.json()["detail"]


async def test_import_all_clears_and_keep_bypass(api, session):
    text = export_rew(FIXTURE[:2])
    response = await import_rew(api, text=text, bank="inputs.0.peq", unbypass=False)
    assert response.status_code == 200
    bank = (await session.get_desired(0)).inputs[0].peq
    assert [b.compiled for b in bank[:2]] == FIXTURE[:2]
    assert all(b.compiled == PASSTHROUGH for b in bank[2:])
    assert all(b.bypass for b in bank)


async def test_import_crossover_all_all(api, session, fake):
    text = export_rew([LP, LP, PASSTHROUGH, PASSTHROUGH, HP, HP])
    response = await import_rew(api, text=text, bank="outputs.0.crossover")
    assert response.status_code == 200, response.text
    imported = response.json()["imported"]
    assert imported["outputs.0.crossover.0"]["label"] == "LR4 LP 80 Hz"
    assert imported["outputs.0.crossover.1"]["label"] == "LR4 HP 20 Hz"
    assert (await settle(session)).ok
    groups = [g for body in fake.configs for out in body.get("outputs", []) for g in out.get("crossover", [])]
    assert {g["index"] for g in groups if g.get("bypass") is False} == {0, 1}

    exported = await api.get(f"{BASE}/0/export/rew", params={"bank": "outputs.0.crossover"})
    assert parse_rew(exported.text).biquads == [LP, LP, PASSTHROUGH, PASSTHROUGH, HP, HP, PASSTHROUGH, PASSTHROUGH]
    one = await api.get(f"{BASE}/0/export/rew", params={"bank": "outputs.0.crossover", "index": "1", "biquad": "0"})
    assert parse_rew(one.text).biquads == [HP]


async def test_import_crossover_one_group(api, session):
    response = await import_rew(api, text=export_rew([LP]), bank="outputs.3.crossover", index=1, biquad="all")
    assert response.status_code == 200
    group = (await session.get_desired(0)).outputs[3].crossover[1]
    assert group.compiled == [LP, PASSTHROUGH, PASSTHROUGH, PASSTHROUGH] and not group.bypass
    assert response.json()["imported"]["outputs.3.crossover.1"]["biquads_used"] == 1


async def test_import_refusals(api, session, fake):
    bad = await import_rew(api, text="biquad1,\nb0=1,\nb1=0", bank="outputs.0.peq")
    assert bad.status_code == 422
    assert bad.json()["problems"] == ["line 1: biquad1: missing b2, a1, a2"]
    empty = await import_rew(api, text="", bank="outputs.0.peq")
    assert empty.status_code == 422 and empty.json()["problems"][0].startswith("no biquads found")
    bank = await import_rew(api, text=REW_TEXT, bank="inputs.0.crossover")
    assert bank.status_code == 422 and bank.json()["detail"] == "only outputs have a crossover"
    xover = await import_rew(api, text=REW_TEXT, bank="outputs.0.crossover")
    assert xover.status_code == 422 and "10 filters don't fit" in xover.json()["detail"]
    preset = await import_rew(api, preset=7, text=REW_TEXT, bank="outputs.0.peq")
    assert preset.status_code == 404
    extra = await import_rew(api, text=REW_TEXT, bank="outputs.0.peq", surprise=1)
    assert extra.status_code == 422
    await settle(session)
    assert fake.configs == []


async def test_big_boost_needs_confirmation(api, session):
    text = export_rew([peaking(1000, 1, 10)])
    response = await import_rew(api, text=text, bank="outputs.0.peq", index=0)
    assert response.status_code == 409 and response.json()["error"] == "confirmation_required"
    assert (await session.get_desired(0)).outputs[0].peq[0].compiled == PASSTHROUGH
    confirmed = await import_rew(api, text=text, bank="outputs.0.peq", index=0, confirm=True)
    assert confirmed.status_code == 200
    too_big = await import_rew(api, text=export_rew([peaking(1000, 1, 30)]), bank="outputs.0.peq", confirm=True)
    assert too_big.status_code == 422 and too_big.json()["detail"].startswith("filter boost too large")


@pytest.mark.parametrize(
    "biquad",
    [
        linkwitz_transform(25, 0.7, 5, 0.5),  # +32.9 dB at DC in float32, only +21.6 dB at 5 Hz
        linkwitz_transform(20, 0.6, 3, 0.5),
        peaking(40000, 2, 60),  # peaks above the 24 kHz display range
        high_shelf(40000, 0.7071, 40),
    ],
    ids=["lt-25-5", "lt-20-3", "peak-40k", "shelf-40k"],
)
async def test_boost_outside_the_display_range_is_too_large(api, session, biquad):
    response = await import_rew(api, text=export_rew([biquad]), bank="outputs.0.peq", index=0, confirm=True)
    assert response.status_code == 422 and response.json()["detail"].startswith("filter boost too large")
    assert (await session.get_desired(0)).outputs[0].peq[0].compiled == PASSTHROUGH


async def test_if_match(api, session):
    stale = await api.post(
        f"{BASE}/0/import/rew", json={"text": REW_TEXT, "bank": "outputs.1.peq"}, headers={"If-Match": '"99"'}
    )
    assert stale.status_code == 412


async def test_import_is_logged(api, session, repo):
    assert (await import_rew(api, text=REW_TEXT, bank="outputs.1.peq")).status_code == 200
    await drain(session)
    events = await repo.list_events(kind="import")
    assert any("imported 10 REW filter(s) into outputs.1.peq of preset 1" in e.message for e in events)


async def test_preview(api):
    response = await api.post("/api/v1/import/rew", json={"text": "junk line\n" + REW_TEXT})
    assert response.status_code == 200
    data = response.json()
    assert data["ok"] is True and len(data["filters"]) == 10
    first = data["filters"][0]
    assert first["number"] == 1 and first["line"] == 2
    assert first["biquad"] == dict(zip(KEYS, FIXTURE[0].as_tuple(), strict=True))
    assert first["design"]["type"] == "peaking" and first["label"].startswith("PEQ 106 Hz")
    assert data["warnings"] == [{"line": 1, "text": "junk line", "message": "not part of a biquad; ignored"}]

    broken = (await api.post("/api/v1/import/rew", json={"text": "biquad1,\na1=3,\na2=0.5"})).json()
    assert broken["ok"] is False and broken["filters"] == []
    assert broken["errors"][0]["message"] == "biquad1: missing b0, b1, b2"


async def test_export_options(api, session):
    await import_rew(api, text=REW_TEXT, bank="outputs.1.peq")
    await session.edit(0, {"outputs": {"1": {"peq": {"0": {"bypass": True}}}}})
    effective = parse_rew((await api.get(f"{BASE}/0/export/rew", params={"bank": "outputs.1.peq"})).text)
    assert effective.biquads[0] == PASSTHROUGH and effective.biquads[1:] == FIXTURE[1:]
    stored = await api.get(f"{BASE}/0/export/rew", params={"bank": "outputs.1.peq", "include_bypassed": "true"})
    assert stored.text == REW_TEXT
    one = await api.get(f"{BASE}/0/export/rew", params={"bank": "outputs.1.peq", "index": "5"})
    assert parse_rew(one.text).biquads == [FIXTURE[5]]

    download = await api.get(f"{BASE}/2/export/rew", params={"bank": "outputs.1.peq", "download": "true"})
    assert download.headers["content-disposition"] == 'attachment; filename="minidsp-preset-3-output-2-peq.txt"'
    assert parse_rew(download.text).biquads == [PASSTHROUGH] * 10

    for params in ({"bank": "outputs.1.peq", "index": "x"}, {"bank": "outputs.1.peq", "index": "10"}, {"bank": "x"}):
        refused = await api.get(f"{BASE}/0/export/rew", params=params)
        assert refused.status_code == 422, params


async def test_clearing_an_active_crossover_group_needs_confirmation(api, session):
    hp = export_rew([HP, HP])
    assert (await import_rew(api, text=hp, bank="outputs.1.crossover", index=1)).status_code == 200
    lp = export_rew([LP, LP])  # both groups by default: group 2 (the high-pass) would become pass-through
    refused = await import_rew(api, text=lp, bank="outputs.1.crossover")
    assert refused.status_code == 409 and refused.json()["error"] == "confirmation_required"
    assert "clears crossover group 2 of outputs.1.crossover" in refused.json()["detail"]
    assert (await session.get_desired(0)).outputs[1].crossover[1].compiled[:2] == [HP, HP]
    one = await import_rew(api, text=lp, bank="outputs.1.crossover", index=0)
    assert one.status_code == 200
    confirmed = await import_rew(api, text=lp, bank="outputs.1.crossover", confirm=True)
    assert confirmed.status_code == 200
    assert any("clears crossover group 2" in w["message"] for w in confirmed.json()["warnings"])
