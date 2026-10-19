"""The guided tasks' shell and its two wizards on a real session (phase 9 design §14.1, §14.3, §14.4)."""

import html
import json
import re
import time

from app.web.tasks import UndoRecord

from .conftest import HX, SERIAL

ORIGIN = {**HX, "Origin": "http://test"}


async def _silent_sub(ctx) -> None:
    """In 1 feeds Out 1 only; nothing feeds Out 2 (the Sub)."""
    values = {f"inputs.{i}.routing.{o}.enabled": (i == 0 and o == 0) for i in range(2) for o in range(4)}
    response = await ctx.edit(0, values, commit=True, confirm=True)
    assert response.status_code == 200, response.text
    await ctx.settle()


def _undo(text: str) -> str:
    found = re.search(r'name="undo" value="([^"]*)"', text)
    assert found, "no Undo in the step"
    return html.unescape(found.group(1))


async def _post(ctx, task: str, **form):
    return await ctx.http.post(f"/ui/tasks/{task}", data={"mode": "dialog", **form}, headers=ORIGIN)


async def test_task_pages_and_links_light_up(ctx):
    page = await ctx.http.get("/tasks")
    assert page.status_code == 200
    assert 'href="/tasks/diagnose"' in page.text and 'href="/tasks/louder"' in page.text
    assert "Guided tasks" in page.text
    home = (await ctx.http.get("/")).text
    assert 'data-tasks="diagnose louder eq-cut line-up"' in home  # the dialog on every page
    assert 'href="/tasks/diagnose"' in home  # the Help me… menu and Home's Guided tasks
    assert (await ctx.http.get("/tasks/set-levels")).status_code == 404  # not built yet: its links keep their fallback
    assert (await ctx.http.get("/ui/tasks/nope")).status_code == 404


async def test_a_step_renders_as_a_page_and_as_a_fragment(ctx):
    await _silent_sub(ctx)
    page = await ctx.http.get("/tasks/diagnose?step=2&what=one&output=2")
    assert page.status_code == 200 and "<title>Something wrong?" in page.text
    assert 'id="task-heading-page"' in page.text and "No input is routed to Out 2" in page.text
    fragment = await ctx.http.get("/ui/tasks/diagnose?step=2&what=one&output=2&mode=page", headers=HX)
    assert "<html" not in fragment.text and "No input is routed to Out 2" in fragment.text
    assert fragment.headers["HX-Push-Url"] == "/tasks/diagnose?step=2&what=one&output=2"
    dialog = await ctx.http.get("/ui/tasks/diagnose?step=2&what=one&output=2&mode=dialog", headers=HX)
    assert "HX-Push-Url" not in dialog.headers and "data-task-close" in dialog.text
    assert "data-highlight='[\"o1:level\"]'" in dialog.text


async def test_writes_need_htmx_and_the_same_origin(ctx):
    form = {"what": "one", "output": "2", "step": "2", "check": "o1-route", "fix": "0", "op": "fix"}
    assert (await ctx.http.post("/ui/tasks/diagnose", data=form)).status_code == 400
    cross = await ctx.http.post("/ui/tasks/diagnose", data=form, headers={**HX, "Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403


async def test_diagnose_fix_routes_in_1_and_undo_removes_it(ctx):
    await _silent_sub(ctx)
    form = {"what": "one", "output": "2", "step": "2", "check": "o1-route", "fix": "0", "op": "fix"}
    response = await _post(ctx, "diagnose", **form)
    assert response.status_code == 200
    assert "Fixed: In 1 → Out 2 at 0 dB" in response.text
    assert ctx.session.desired(0).inputs[0].routing[1].enabled
    record = _undo(response.text)
    done = await ctx.http.get(
        "/ui/tasks/diagnose",
        params={
            "step": "3",
            "what": "one",
            "output": "2",
            "undo": record,
            "fixed": json.dumps([{"done": "In 1 → Out 2 at 0 dB", "cause": "no input was routed", "o": "Out 2"}]),
        },
        headers=HX,
    )
    assert "Out 2 was silent because no input was routed. Fixed: In 1 → Out 2 at 0 dB." in done.text
    undone = await _post(ctx, "diagnose", what="one", output="2", step="3", op="undo", undo=record)
    assert "Undone: In 1 → Out 2 route is back." in undone.text
    assert not ctx.session.desired(0).inputs[0].routing[1].enabled


async def test_undo_skips_a_setting_changed_elsewhere(ctx):
    await _silent_sub(ctx)
    form = {"what": "one", "output": "2", "step": "2", "check": "o1-route", "fix": "0", "op": "fix"}
    record = _undo((await _post(ctx, "diagnose", **form)).text)
    # Someone (another tab, Home Assistant) turns the route off and on with another level meanwhile.
    await ctx.edit(0, {"inputs.0.routing.1.enabled": False}, commit=True)
    undone = await _post(ctx, "diagnose", what="one", output="2", step="2", op="undo", undo=record)
    assert "In 1 → Out 2 route was changed elsewhere, so left as is." in html.unescape(undone.text)
    # The JSON variant (the toast's Undo) says the same.
    response = await ctx.http.post("/ui/tasks/undo", json={"record": record}, headers=ORIGIN)
    assert response.status_code == 200 and response.json()["skipped"] == ["In 1 → Out 2 route"]


async def test_undo_is_refused_after_its_window(ctx):
    old = UndoRecord(0, {"outputs.0.gain_db": (0.0, 3.0)}, at=time.time() - 120)
    response = await ctx.http.post("/ui/tasks/undo", json={"record": old.to_json()}, headers=ORIGIN)
    assert response.status_code == 409 and "30 seconds" in response.json()["message"]


async def test_louder_applies_the_chosen_way_and_asks_for_a_big_raise(ctx):
    base = {"output": "1", "dir": "up", "method": "output", "step": "4"}
    before = ctx.session.desired(0).outputs[0].gain_db
    response = await _post(ctx, "louder", **base, amount="3", op="apply")
    assert response.status_code == 200 and "Out 1 is louder" in response.text
    assert ctx.session.desired(0).outputs[0].gain_db == before + 3
    record = _undo(response.text)
    # A raise above the big-step threshold (6 dB) needs the checkbox: nothing is sent without it.
    refused = await _post(ctx, "louder", **base, amount="8", op="apply")
    assert "Tick the box" in refused.text and "I confirm raising Out 1 by 8 dB." in refused.text
    assert ctx.session.desired(0).outputs[0].gain_db == before + 3
    # Undo of the first raise puts it back.
    undone = await _post(ctx, "louder", **{**base, "step": "5"}, amount="3", op="undo", undo=record)
    assert "Undone" in undone.text and ctx.session.desired(0).outputs[0].gain_db == before


async def test_louder_at_the_limit_preselects_the_route(ctx):
    await ctx.edit(0, {"outputs.0.gain_db": 12.0, "inputs.0.routing.0.gain_db": 4.0}, commit=True, confirm=True)
    how = await ctx.http.get("/ui/tasks/louder?step=3&output=1&dir=up&amount=3", headers=HX)
    assert re.search(r'value="routes" checked', how.text)
    assert re.search(r'value="output"[^>]*disabled', how.text)
    assert "Raise the route In 1 → Out 1 from +4 to +7 dB (output level stays at its +12 limit)" in how.text


async def test_the_dialog_is_on_every_page_without_a_device_too(ctx):
    for path in ("/", "/routing", "/settings", "/help"):
        assert 'id="task-dialog"' in (await ctx.http.get(path)).text, path
    assert SERIAL
