"""Home (``/``) and the Inputs / Outputs overview pages (phase 9 design §4).

Home answers "what is each output doing, and is anything wrong?" in words, top to bottom: the health line, the
signal-flow diagram, one card per output (with the §4.3 level rule), the folded "Not in use" outputs, a strip with
every meter, Presets & profiles, and the guided tasks. ``/inputs`` and ``/outputs`` reuse the same cards (design
§4.5). Today's dashboard moved to ``/meters`` (``app/web/routes.py``); a per-browser Start page setting can send the
browser there instead (``static/js/start.js``).

Everything on these pages is read from the summary engine (`app/sync/summary.py`), so it can't disagree with the
diagram, the problems pill or the output pages. Writes go through the editors' endpoint
(``/ui/devices/{serial}/presets/{preset}/edit``), so the server applies the same guards (level limits, the big-step
confirmation).

The level rule (`level_step`, mirrored by ``levelStep`` in ``static/js/dashboard.js``): **+** raises the output
level up to its limit, then every enabled route into the output by the same step, up to the route limit (which is the
output's limit too); with both at their limits it is disabled and the card says why. **−** only ever lowers the output
level.
"""

import contextlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.sync.session import DeviceSession
from app.sync.summary import (
    ARROW,
    CAUTION,
    PROBLEM,
    InputSummary,
    OutputSummary,
    PresetSummary,
    Total,
    join_words,
    session_summary,
    signed,
    signed1,
)
from app.web import devices, flow, shell
from app.web.routes import _nav_children, _not_found_page, output_cards, page_context
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

# Live topics of Home and the overview pages: the header, the meters, and whatever changes a summary.
HOME_TOPICS = ("levels", "master", "device", "sync", "desired", "verify")

STEP_DB = 0.5  # the card's −/+ step (design §4.3)
LEVEL_ANCHOR = "home-out"  # the cards' id prefix: the diagram's Level block scrolls to "#home-out1"
EPS = 1e-9

# --- the level rule (design §4.3) ----------------------------------------------------------------------------------


def _r1(value: float) -> float:
    """On the 0.1 dB grid, half up (as ``Math.round`` in the browser, so both sides agree)."""
    return math.floor(value * 10 + 0.5) / 10


@dataclass(frozen=True)
class LevelStep:
    """What one press of a card's − or + sends: ``values`` maps device paths to their new level (empty when the press
    can't change anything, so the button is disabled). ``target``: "output", "routes" or "none"."""

    values: dict[str, float] = field(default_factory=dict)
    target: str = "none"

    @property
    def disabled(self) -> bool:
        return not self.values


def level_step(total: Total, output: int, direction: int, *, gain_min: float, step: float = STEP_DB) -> LevelStep:
    """The §4.3 rule for one press. ``direction`` is +1 (the + button) or −1 (the − button).

    + raises the output level while it is below the output's limit; at the limit it raises every enabled route into
    the output that is still below the limit (routes share the output's limit, ``Total.ceiling``). − lowers only the
    output level, down to the device minimum; it never touches a route."""
    ceiling = total.ceiling
    if direction < 0:
        if total.output > gain_min + EPS:
            return LevelStep({f"outputs.{output}.gain_db": max(gain_min, _r1(total.output - step))}, "output")
        return LevelStep()
    if total.output < ceiling - EPS:
        return LevelStep({f"outputs.{output}.gain_db": min(ceiling, _r1(total.output + step))}, "output")
    raised = {
        f"inputs.{r.input}.routing.{output}.gain_db": min(ceiling, _r1(r.route_db + step))
        for r in total.routes
        if r.route_db < ceiling - EPS
    }
    return LevelStep(raised, "routes") if raised else LevelStep()


def _routes_words(o: OutputSummary) -> str:
    routes = [f"{r.name} {ARROW} {o.name}" for r in o.total.routes]
    return ("the route " if len(routes) == 1 else "the routes ") + join_words(routes)


def card_view(o: OutputSummary, *, gain_min: float, root: str, request: Request | None = None) -> dict[str, Any]:
    """One output card (design §4.1 / §4.3): the summary's words plus the level rule's buttons and notes."""
    total = o.total
    plus = level_step(total, o.index, +1, gain_min=gain_min)
    minus = level_step(total, o.index, -1, gain_min=gain_min)
    ceiling = total.ceiling
    if total.routes:
        top = max(r.input_db for r in total.routes) + 2 * ceiling
        max_note = f"{o.name} is at its maximum ({signed1(top)} dB total)."
    else:
        max_note = f"{o.name} is at its level limit ({signed(ceiling)} dB) and no input is routed to it."
    routes_note = ""
    if total.routes:
        routes_note = f"Output is at its {signed(ceiling)} dB limit: + now raises {_routes_words(o)}."
    min_note = f"{o.name} is at its lowest output level ({signed(gain_min)} dB)."
    silencing = next((p for p in o.problems if p.silences), None)
    reason = None
    if o.badge_reason and o.badge in ("silent", "check", "overload"):
        reason = next((p for p in o.problems if p.text == o.badge_reason), None)
    in_names = [r.name for r in total.routes]
    lower_href = (shell.task_href(request, "louder") if request is not None else None) or "/outputs"
    headroom = o.headroom
    if reason is not None and reason.code == "high_total" and headroom.level != "none":
        reason = None  # the headroom line below says the same thing, more concretely
    return {
        "o": o,
        "index": o.index,
        "key": f"o{o.index}",
        "anchor": f"{LEVEL_ANCHOR}{o.index + 1}",
        "href": f"{root}/outputs/{o.index + 1}",
        "plus": plus,
        "minus": minus,
        "routes_note": routes_note,
        "max_note": max_note,
        "min_note": min_note,
        "lower_href": root + lower_href,
        "silencing": silencing,
        "reason": reason,
        "headroom": headroom if headroom.level != "none" else None,
        "routes_json": [
            {"input": r.input, "name": r.name, "in": r.input_db, "route": r.route_db} for r in total.routes
        ],
        "in_names": in_names,
        "adjust": adjust_links(o, root, request),
    }


def adjust_links(o: OutputSummary, root: str, request: Request | None) -> list[dict[str, str]]:
    """The card's Adjust ▾ menu (design §4.3): the output page's tabs, Rename, and this output's guided tasks."""
    n = o.index + 1
    base = f"{root}/outputs/{n}"
    links = [
        {"label": "Band (crossover)", "href": flow.block_href("band", o.index, root)},
        {"label": "EQ", "href": flow.block_href("eq", o.index, root)},
        {"label": "Delay", "href": flow.block_href("delay", o.index, root)},
        {"label": "Polarity", "href": flow.block_href("polarity", o.index, root)},
        {"label": "Compressor (limiter)", "href": flow.block_href("compressor", o.index, root)},
        {"label": "Response graph", "href": f"{base}?tab=response"},
        {"label": "Rename", "href": f"{base}#channel-name"},
    ]
    if request is not None:
        for task_id, label in (("eq-cut", "Fix a boom"), ("line-up", "Line up"), ("louder", "Make louder")):
            href = shell.task_href(request, task_id)
            if href and href.startswith("/tasks/"):
                links.append({"label": f"{label}…", "href": f"{root}{href}?output={n}", "task": "1"})
    return links


def input_view(i: InputSummary, root: str) -> dict[str, Any]:
    """One input card of ``/inputs`` (design §4.5): "In 1: level 0 dB, no EQ, sent to Shakers (+4 dB), Out 3"."""
    return {
        "i": i,
        "key": f"i{i.index}",
        "href": f"{root}/inputs/{i.index + 1}",
        "issues": [p for p in i.problems if p.level in (PROBLEM, CAUTION)],
        "notes": [p for p in i.problems if p.level not in (PROBLEM, CAUTION)],
    }


# --- page context ---------------------------------------------------------------------------------------------------


def _root(request: Request) -> str:
    return request.scope.get("root_path", "").rstrip("/")


def _wizard_href(request: Request, task_id: str) -> str | None:
    """A guided task's link, or None while its fallback is Home itself (a link from Home to Home helps nobody)."""
    href = shell.task_href(request, task_id)
    return None if not href or href == "/" else _root(request) + href


def _part_url(request: Request, part: str, serial: int | None) -> str:
    return f"{_root(request)}/ui/home/{part}" + (f"?serial={serial}" if serial is not None else "")


async def _loaded_summary(session: DeviceSession | None) -> PresetSummary | None:
    if session is None or not hasattr(session, "ensure_loaded"):
        return None
    try:
        await session.ensure_loaded()
    except Exception:
        return None
    return session_summary(session)


def health_context(request: Request, session: DeviceSession | None, summary: PresetSummary | None) -> dict[str, Any]:
    """The health line (design §4.1): problems and cautions with their fixes, the notes folded, and a link to the full
    check. Reuses the problems pill's view, so the two always agree."""
    serial = session.serial if session is not None else None
    view = shell.problems_view(request, session, summary) if summary is not None else None
    root = _root(request)
    fix_url = None
    if view is not None and serial is not None:
        fix_url = f"{root}/ui/devices/{serial}/presets/{view.preset}/edit"
    return {
        "health": view,
        "health_url": _part_url(request, "health", serial),
        "health_fix_url": fix_url,
        # Before set-up every setting is unknown, so "everything looks fine" would claim a check that can't be made.
        "health_first_run": bool(getattr(session, "needs_adoption", False)) if session is not None else False,
        # Until the "Something wrong?" wizard exists its fallback is Home itself, so the link is left out here.
        "diagnose_href": _wizard_href(request, "diagnose"),
    }


async def cards_context(
    request: Request, session: DeviceSession | None, summary: PresetSummary | None, *, fold: bool = True
) -> dict[str, Any]:
    """The output cards of the playing preset (``fold``: outputs marked Not in use share one line, as on Home)."""
    serial = session.serial if session is not None else None
    root = _root(request)
    ctx: dict[str, Any] = {
        "cards": None,
        "folded": [],
        "fold": fold,
        "first_run": bool(getattr(session, "needs_adoption", False)) if session is not None else False,
        "adopt_href": root + (shell.task_href(request, "adopt") or "/device#adopt"),
        "cards_url": _part_url(request, "cards" if fold else "outputs", serial),
        "cards_preset": None,
        "cards_master_mute": None,
        "cards_edit_url": None,
    }
    if summary is None or summary.preset is None:
        return ctx
    gain_min = session.profile.gain_min_db
    state = await output_cards(session) or []
    by_index = {c["index"]: c for c in state}
    in_names = [i.name for i in summary.inputs]
    out_names = [o.name for o in summary.outputs]
    cards = []
    for o in summary.outputs:
        view = card_view(o, gain_min=gain_min, root=root, request=request)
        view["state"] = by_index.get(o.index)
        view["reason_fixes"] = flow.fixes(view["reason"], root, in_names, out_names) if view["reason"] else []
        cards.append(view)
    master = session.master
    ctx.update(
        cards=cards,
        folded=[c for c in cards if c["o"].unused] if fold else [],
        cards_preset=summary.preset,
        cards_master_mute=master.mute if master is not None else None,
        cards_edit_url=f"{root}/ui/devices/{serial}/presets/{summary.preset}/edit",
    )
    return ctx


def _save_href(wizard: str | None) -> str:
    """ "+ Save current as…": the Save path of the profiles wizard (design §14.8), else the Profiles page."""
    return f"{wizard}?do=save" if wizard and wizard.startswith("/tasks/") else (wizard or "/profiles")


async def presets_context(request: Request, session: DeviceSession | None) -> dict[str, Any]:
    """Presets & profiles (design §4.1): which device preset is playing and which saved profile it matches, what waits
    for the other presets, and the saved profiles (each opens the load wizard; it never loads in one tap)."""
    from app.web import profiles as profiles_ui  # local: app.web.profiles imports the editing pages' helpers

    serial = session.serial if session is not None else None
    root = _root(request)
    ctx: dict[str, Any] = {
        "presets_url": _part_url(request, "presets", serial),
        "playing": None,
        "same_as": [],
        "waiting": [],
        "saved": [],
        "saved_more": 0,
        "saved_error": None,
        "profiles_href": f"{root}/profiles",
        "save_href": root + _save_href(shell.task_href(request, "profiles")),
        "preset_url": f"{root}/ui/devices/{serial}/master/preset" if serial is not None else None,
    }
    if session is None or not getattr(session, "loaded", False):
        return ctx
    master = session.master
    playing = master.preset if master is not None else None
    if not isinstance(playing, int) or not 0 <= playing < session.profile.n_presets:
        return ctx
    ctx["playing"] = playing
    for p in range(session.profile.n_presets):
        if p == playing:
            continue
        with contextlib.suppress(Exception):
            pending = int(session.sync_summary(p)["counts"].get("pending", 0))
            if pending:
                ctx["waiting"].append(
                    {"preset": p, "count": pending, "href": f"{root}/device?preset={p + 1}#sync"},
                )
    try:
        listing = await profiles_ui.list_context(request, session)
    except Exception as exc:  # the profiles store is optional; Home must still render
        ctx["saved_error"] = str(exc) or type(exc).__name__
        return ctx
    ctx["saved_error"] = listing.get("profiles_error")
    items = listing.get("profiles", [])
    ctx["same_as"] = [
        p["name"]
        for p in items
        if playing in p.get("matches", []) or any(s["preset"] == playing and s["same"] for s in p.get("loaded", []))
    ]
    wizard = shell.task_href(request, "profiles") or "/profiles"
    shown = items[:6]
    ctx["saved"] = [
        {
            "id": p["id"],
            "name": p["name"],
            # The load wizard (design §14.8) once it exists; until then the profile's row on the Profiles page.
            "href": (
                f"{root}{wizard}?profile={p['id']}"
                if wizard.startswith("/tasks/")
                else f"{root}/profiles#profile-{p['id']}"
            ),
        }
        for p in shown
    ]
    ctx["saved_more"] = max(0, len(items) - len(shown))
    return ctx


def tasks_context(request: Request) -> dict[str, Any]:
    """The Guided tasks card (design §4.1): each task with a one-line hint."""
    hints = {
        "diagnose": "find why an output is silent",
        "louder": "e.g. shakers vs sub",
        "eq-cut": "add an EQ cut",
        "line-up": "distance → delay",
        "set-levels": "avoid clipping",
        "profiles": "keep a setup to use later",
        "adopt": "tell the app what the device holds",
    }
    return {
        "guided": [
            {
                "id": t.id,
                "label": t.label,
                "hint": hints.get(t.id, ""),
                # "Something wrong?" falls back to Home's own health line until its wizard exists.
                "href": _wizard_href(request, t.id) or "#home-health-title",
            }
            for t in shell.TASKS
        ]
    }


def strip_context(
    summary: PresetSummary | None, input_names: Sequence[str], output_names: Sequence[str]
) -> dict[str, Any]:
    """The meter strip (design §4.1): every channel's meter and CLIP light, muted outputs marked 🔇."""
    muted = {o.index for o in summary.outputs if o.muted} if summary is not None else set()
    return {
        "strip": [
            *({"key": f"i{n}", "name": name, "out": False, "muted": False} for n, name in enumerate(input_names)),
            *({"key": f"o{n}", "name": name, "out": True, "muted": n in muted} for n, name in enumerate(output_names)),
        ]
    }


def _levels_ctx(ctx: dict[str, Any], session: DeviceSession | None) -> dict[str, Any]:
    status = session.status if session is not None else None
    return {
        "input_names": [child.label for child in _nav_children(ctx["nav"], "/inputs")],
        "output_names": [child.label for child in _nav_children(ctx["nav"], "/outputs")],
        "initial_levels": {
            "i": list(status.input_levels) if status else [],
            "o": list(status.output_levels) if status else [],
        },
    }


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


# --- pages ----------------------------------------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, name="home")
async def home(request: Request, serial: int | None = None) -> Response:
    session = devices.pick_session(request, serial)
    if serial is not None and session is None:
        return _not_found_page(request, serial)
    ctx = page_context(request, session, HOME_TOPICS)
    summary = await _loaded_summary(session)
    levels = _levels_ctx(ctx, session)
    ctx.update(
        title="Home",
        meters_page=f"{_root(request)}/meters",
        **levels,
        **health_context(request, session, summary),
        **await cards_context(request, session, summary),
        **await presets_context(request, session),
        **tasks_context(request),
        **strip_context(summary, levels["input_names"], levels["output_names"]),
        **await flow.flow_context(request, session, level_anchor=LEVEL_ANCHOR),
    )
    return _no_store(templates.TemplateResponse(request, "home.html", ctx))


def _preset_redirect(request: Request, first: str) -> Response | None:
    """``/outputs?preset=2`` used to open the first output of preset 2; links that name a preset keep doing that. The
    overview itself always shows the playing preset."""
    if "preset" not in request.query_params:
        return None
    query = urlencode(dict(request.query_params))
    return RedirectResponse(f"{_root(request)}{first}?{query}", status_code=307)


@router.get("/outputs", response_class=HTMLResponse, name="outputs_overview")
async def outputs_overview(request: Request, serial: int | None = None) -> Response:
    """Every output as a Home card, none folded (design §4.5); each links to its full page."""
    if (redirect := _preset_redirect(request, "/outputs/1")) is not None:
        return redirect
    session = devices.pick_session(request, serial)
    if serial is not None and session is None:
        return _not_found_page(request, serial)
    ctx = page_context(request, session, HOME_TOPICS)
    summary = await _loaded_summary(session)
    ctx.update(
        title="Outputs",
        **_levels_ctx(ctx, session),
        **await cards_context(request, session, summary, fold=False),
    )
    return _no_store(templates.TemplateResponse(request, "outputs_overview.html", ctx))


@router.get("/inputs", response_class=HTMLResponse, name="inputs_overview")
async def inputs_overview(request: Request, serial: int | None = None) -> Response:
    """One card per input (design §4.5): what it is sent to, in words; each links to its full page."""
    if (redirect := _preset_redirect(request, "/inputs/1")) is not None:
        return redirect
    session = devices.pick_session(request, serial)
    if serial is not None and session is None:
        return _not_found_page(request, serial)
    ctx = page_context(request, session, HOME_TOPICS)
    summary = await _loaded_summary(session)
    ctx.update(title="Inputs", **_levels_ctx(ctx, session), **inputs_context(request, session, summary))
    return _no_store(templates.TemplateResponse(request, "inputs_overview.html", ctx))


def inputs_context(request: Request, session: DeviceSession | None, summary: PresetSummary | None) -> dict[str, Any]:
    root = _root(request)
    serial = session.serial if session is not None else None
    in_names = [i.name for i in summary.inputs] if summary is not None else []
    out_names = [o.name for o in summary.outputs] if summary is not None else []
    inputs = None
    if summary is not None:
        inputs = []
        for i in summary.inputs:
            view = input_view(i, root)
            view["issue_fixes"] = [flow.fixes(p, root, in_names, out_names) for p in view["issues"]]
            view["note_fixes"] = [flow.fixes(p, root, in_names, out_names) for p in view["notes"]]
            inputs.append(view)
    return {
        "inputs": inputs,
        "inputs_url": _part_url(request, "inputs", serial),
        "inputs_preset": summary.preset if summary is not None else None,
        "inputs_edit_url": (
            f"{root}/ui/devices/{serial}/presets/{summary.preset}/edit"
            if summary is not None and serial is not None
            else None
        ),
    }


# --- partials (re-fetched on SSE desired / sync / verify and when the playing preset changes) ----------------------

PARTS = {
    "health": "partials/home_health.html",
    "cards": "partials/home_cards.html",
    "outputs": "partials/home_cards.html",
    "presets": "partials/home_presets.html",
    "inputs": "partials/input_cards.html",
}


@router.get("/ui/home/{part}", response_class=HTMLResponse)
async def home_part(request: Request, part: str, serial: int | None = None) -> Response:
    template = PARTS.get(part)
    if template is None:
        return _no_store(HTMLResponse("Unknown part", status_code=404))
    session = devices.pick_session(request, serial)
    summary = await _loaded_summary(session)
    ctx: dict[str, Any] = {"session": session, "serial": session.serial if session is not None else None}
    if part == "health":
        ctx.update(health_context(request, session, summary))
    elif part in ("cards", "outputs"):
        ctx.update(await cards_context(request, session, summary, fold=part == "cards"))
    elif part == "presets":
        ctx.update(await presets_context(request, session))
    else:
        ctx.update(inputs_context(request, session, summary))
    return _no_store(templates.TemplateResponse(request, template, ctx))


__all__ = ["LevelStep", "card_view", "level_step", "router"]
