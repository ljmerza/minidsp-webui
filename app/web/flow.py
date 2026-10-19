"""The signal-flow diagram (phase 9 design §5, must-have 2): inputs on the left, a line per route, and one row per
output with its settings as blocks, its health badge and its meter.

Everything here is computed on the server from the summary engine (`app/sync/summary.py`), so the diagram can't
disagree with the cards, and from `DeviceProfile` counts (no channel counts are written into the code). The lines are
inline SVG drawn on a fixed row geometry (`flow_layout`), so the browser never measures the layout. The SVG is
decorative (``aria-hidden``): every route is repeated as text in its output row ("Fed by In 1 at +4 dB").

The template is ``partials/flow.html`` (re-fetched from ``/ui/flow`` on ``desired`` / ``sync`` / ``verify``
events) inside ``partials/flow_card.html``; the live meters bind to the enclosing ``levelMeters`` component
(static/js/meters.js) and static/js/flow.js does the rest (roving focus between blocks, the inline fixes, the clip
announcement).
"""

import itertools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.model.device_profile import DeviceProfile
from app.sync.summary import Action, InputSummary, OutputSummary, PresetSummary, Problem, session_summary
from app.web import devices
from app.web.glossary import term
from app.web.templating import templates

router = APIRouter(include_in_schema=False)

# --- geometry (px) ----------------------------------------------------------------------------------------------------
# Fixed row heights: the SVG's coordinates are the HTML rows' coordinates (static/css/flow.css uses the same numbers
# through the custom properties the template sets). Change them together.

ROUTE_W = 150  # the column the route lines are drawn in
ROW_H = 80  # one output row: a heading line and a line of setting blocks
ROW_GAP = 8
FOLD_H = 44  # the shared "Out 3 · Out 4 ◌ Not in use" line
IN_H = 100  # one input box: name, meter, EQ and level, and the "not sent anywhere" note
IN_GAP = 12
ARROW = 7  # the arrow head's length; lines stop this short of the row


@dataclass(frozen=True)
class Slot:
    top: float
    height: float

    @property
    def center(self) -> float:
        return self.top + self.height / 2

    @property
    def bottom(self) -> float:
        return self.top + self.height


@dataclass(frozen=True)
class Layout:
    """Where every box sits in one state of the diagram (``folded``: outputs marked Not in use share one line)."""

    width: int
    height: float
    inputs: tuple[Slot, ...]  # one per input, in order
    outputs: dict[int, Slot]  # output index -> its row, for the rows shown
    fold: Slot | None  # the shared Not in use line, after the rows


def flow_layout(profile: DeviceProfile, folded: Iterable[int] = (), *, fold_line: bool | None = None) -> Layout:
    """Rows for the profile's outputs (minus ``folded``, which share a line at the end), and the profile's inputs spread
    evenly over the same height. ``fold_line`` keeps the shared line even when nothing is folded (the "shown" state of
    the diagram still has it, holding the Hide button)."""
    hidden = set(folded)
    y = 0.0
    outputs: dict[int, Slot] = {}
    for o in range(profile.n_outputs):
        if o in hidden:
            continue
        outputs[o] = Slot(y, ROW_H)
        y += ROW_H + ROW_GAP
    fold = None
    if fold_line if fold_line is not None else bool(hidden):
        fold = Slot(y, FOLD_H)
        y += FOLD_H + ROW_GAP
    rows_h = max(0.0, y - ROW_GAP)
    n_in = profile.n_inputs
    inputs_h = n_in * IN_H + max(0, n_in - 1) * IN_GAP
    height = max(rows_h, float(inputs_h))
    pitch = height / n_in if n_in else 0.0
    inputs = tuple(Slot(pitch * (i + 0.5) - IN_H / 2, IN_H) for i in range(n_in))
    return Layout(ROUTE_W, height, inputs, outputs, fold)


def _num(value: float) -> str:
    return f"{round(value, 1):g}"


def route_path(y0: float, y1: float, width: float = ROUTE_W) -> str:
    """An S-curve from an input's centre (x 0) to just short of an output row's centre (x ``width``)."""
    end = width - ARROW
    mid = end / 2
    return f"M0 {_num(y0)} C{_num(mid)} {_num(y0)} {_num(mid)} {_num(y1)} {_num(end)} {_num(y1)}"


def _curve_point(t: float, y0: float, y1: float, width: float = ROUTE_W) -> tuple[float, float]:
    """The point at ``t`` on `route_path`'s curve (a cubic Bézier)."""
    end = width - ARROW
    mid = end / 2
    u = 1 - t
    x = 3 * u * u * t * mid + 3 * u * t * t * mid + t**3 * end
    y = u**3 * y0 + 3 * u * u * t * y0 + 3 * u * t * t * y1 + t**3 * y1
    return x, y


# --- the view model ---------------------------------------------------------------------------------------------------

MINUS = "−"

# Blocks in the order of design 5.1 (FIR, when loaded, last: its place in the chain is as unverified as the rest).
BLOCK_ORDER = ("level", "band", "eq", "delay", "polarity", "compressor", "fir")
BLOCK_NAMES = {
    "level": "Level",
    "band": "Band",
    "eq": "EQ",
    "delay": "Delay",
    "polarity": "Polarity",
    "compressor": "Compressor",
    "fir": "FIR",
}
# The output page tab each block opens (None: the control is in the page's header strip, see `block_href`).
BLOCK_TABS: dict[str, str | None] = {
    "level": None,
    "band": "crossover",
    "eq": "peq",
    "delay": "delay",
    "polarity": None,
    "compressor": "compressor",
    "fir": "fir",
}
# What the accessible name says the link opens.
BLOCK_OPENS = {
    "level": "Go to its level",
    "band": "Open band settings",
    "eq": "Open EQ settings",
    "delay": "Open delay settings",
    "polarity": "Go to its polarity switch",
    "compressor": "Open compressor settings",
    "fir": "Open FIR settings",
}
STATE_WORDS = {"on": "on", "default": "off", "caution": "check", "problem": "problem"}
STATE_GLYPHS = {"problem": "⛔", "caution": "⚠"}
# A block's corner glyph when its settings aren't all sent (design 5.2 "Not sent"); "waiting" is a pending write held
# for a preset that isn't playing.
SYNC_GLYPHS = {
    "failed": ("⚠", "Failed"),
    "changed": ("≠", "Changed on device"),
    "pending": ("⟳", "Sending"),
    "waiting": ("⏸", "Waiting"),
    "unknown": ("?", "Unknown"),
}
# The summary's sync blocks that belong to each diagram block (the route levels are part of the total level).
_SYNC_SOURCES = {"level": ("level", "sources"), "band": ("band",), "eq": ("eq",), "delay": ("delay",)}
_SYNC_ORDER = ("failed", "changed", "pending", "unknown")
# Fix buttons the diagram performs itself (static/js/flow.js); other actions show only when they have a link.
INLINE_ACTIONS = ("route", "unmute_input", "unmute_output")


@dataclass(frozen=True)
class Block:
    key: str  # one of BLOCK_ORDER
    label: str  # what the chip shows ("Band 20–80 Hz")
    state: str  # on | default | caution | problem
    glyph: str  # ⛔ / ⚠ for problem / caution, else ""
    sync: str | None  # failed | changed | pending | waiting | unknown, None when sent
    sync_glyph: str
    sync_word: str
    href: str
    aria_label: str


def block_name(key: str, label: str, state: str, sync: str = "") -> str:
    """A block's accessible name. It starts with the chip's visible text, so speech input ("click Polarity 0°") finds it
    (WCAG 2.5.3 Label in Name; the PR 17 Lighthouse run flagged "Polarity, 0 degrees" for the chip "Polarity 0°"),
    then says what the chip leaves out: the block's full name, the unit, the state, an unsent status and what opens.
    "Level +16 ▲max" -> "Level +16 max, total in dB, at its limit, check. Go to its level." """
    at_limit = "▲max" in label
    name = re.sub(r"\s+", " ", label.replace("▲max", "max")).strip()
    words = []
    if not name.startswith(BLOCK_NAMES[key]):
        name += f" ({BLOCK_NAMES[key]})"  # "Comp. off (Compressor)", "Full range (Band)"
    if key == "level":
        words.append("total in dB")
        if at_limit:
            words.append("at its limit")
    elif key == "delay" and not name.endswith("ms"):
        words.append("in milliseconds")
    if not label.endswith(f" {state}"):  # no "Comp. off, off"
        words.append(state)
    if sync:
        words.append(sync)
    return ", ".join([name, *words]) + f". {BLOCK_OPENS[key]}."


def block_sync(key: str, by_block: Mapping[str, str], waiting: bool) -> str | None:
    """The worst unsent status of a block's settings (None when all are sent)."""
    statuses = [by_block[b] for b in _SYNC_SOURCES.get(key, (key,)) if b in by_block]
    worst = next((s for s in _SYNC_ORDER if s in statuses), None)
    if worst == "pending" and waiting:
        return "waiting"
    return worst


def block_href(key: str, output: int, root: str = "", level_anchor: str | None = None) -> str:
    """Where a block's link goes: its output page tab, or the header control for Level and Polarity. On Home the Level
    block scrolls to the output's card instead (``level_anchor``: the card ids' prefix)."""
    if key == "level" and level_anchor:
        return f"#{level_anchor}{output + 1}"
    base = f"{root}/outputs/{output + 1}"
    tab = BLOCK_TABS.get(key)
    if tab:
        return f"{base}?tab={tab}"
    return f"{base}#{'gain-output' if key == 'level' else 'invert'}-{output}"


def output_blocks(
    out: OutputSummary, root: str = "", level_anchor: str | None = None, *, staged: bool | None = None
) -> list[Block]:
    """The output's setting blocks, in design order, with their state (5.2), unsent glyph and link (5.4).

    ``staged``: the preset's pending writes wait for it to be played (by default read from the output's headline)."""
    waiting = out.sync.headline.startswith("Waiting") if staged is None else staged
    blocks = []
    for key in BLOCK_ORDER:
        part = out.parts.get(key)
        if part is None:
            continue
        state = part.state if part.state in STATE_WORDS else "on"
        sync = block_sync(key, out.sync.by_block, waiting)
        sync_glyph, sync_word = SYNC_GLYPHS.get(sync or "", ("", ""))
        aria = block_name(key, part.label, STATE_WORDS[state], sync_word.lower() if sync else "")
        blocks.append(
            Block(
                key=key,
                label=part.label,
                state=state,
                glyph=STATE_GLYPHS.get(state, ""),
                sync=sync,
                sync_glyph=sync_glyph,
                sync_word=sync_word,
                href=block_href(key, out.index, root, level_anchor),
                aria_label=aria,
            )
        )
    return blocks


@dataclass(frozen=True)
class Fix:
    """A fix button or link of a health item, ready for the template."""

    id: str
    label: str
    href: str | None = None  # a link (app-relative paths get the root prefix)
    values: dict[str, Any] = field(default_factory=dict)  # an inline edit (path -> value) flow.js sends
    undo: dict[str, Any] = field(default_factory=dict)  # what puts it back
    done: str = ""  # the toast after an inline edit
    # Inline actions that aren't preset edits (static/js/shell.js, data-shell-action): "master" posts ``values`` (form
    # fields) to the header's master endpoint ``endpoint`` ("mute", "volume") with ``undo`` for Undo; "post" posts to
    # the edited preset's ``endpoint`` ("retry"); "clip" resets the CLIP lights.
    action: str | None = None
    endpoint: str | None = None


def fixes(problem: Problem, root: str, in_names: Sequence[str], out_names: Sequence[str]) -> list[Fix]:
    """The actions of a health item that the diagram can offer: inline edits it performs and links."""
    found: list[Fix] = []
    for action in problem.actions:
        fix = _fix(action, root, in_names, out_names)
        if fix is not None:
            found.append(fix)
    return found


def _fix(action: Action, root: str, in_names: Sequence[str], out_names: Sequence[str]) -> Fix | None:
    p = action.params
    if action.id == "route" and "input" in p and "output" in p:
        i, o = int(p["input"]), int(p["output"])
        path = f"inputs.{i}.routing.{o}.enabled"
        done = f"{_name(in_names, i, 'In')} now plays on {_name(out_names, o, 'Out')}."
        return Fix(action.id, action.label, values={path: True}, undo={path: False}, done=done)
    if action.id == "unmute_input" and "input" in p:
        i = int(p["input"])
        path = f"inputs.{i}.mute"
        return Fix(action.id, action.label, values={path: False}, undo={path: True}, done=f"{in_names[i]} un-muted.")
    if action.id == "unmute_output" and "output" in p:
        o = int(p["output"])
        path = f"outputs.{o}.mute"
        return Fix(action.id, action.label, values={path: False}, undo={path: True}, done=f"{out_names[o]} un-muted.")
    if action.id == "reset_filter" and "path" in p and "flat" in p:
        path = str(p["path"])
        undo = {path: p["was"]} if "was" in p else {}
        return Fix(action.id, action.label, values={path: p["flat"]}, undo=undo, done="Filter reset to flat.")
    if action.id == "unmute_all":
        undo = {"mute": "true"}
        done = "Everything un-muted (Mute all is off)."
        return Fix(
            action.id, action.label, values={"mute": "false"}, undo=undo, done=done, action="master", endpoint="mute"
        )
    if action.id == "set_volume" and "volume" in p:
        volume = float(p["volume"])
        undo = {"volume": p["was"]} if isinstance(p.get("was"), int | float) else {}
        done = f"Volume set to {_db(volume)}."
        return Fix(
            action.id, action.label, values={"volume": volume}, undo=undo, done=done, action="master", endpoint="volume"
        )
    if action.id == "retry":
        return Fix(action.id, action.label, done="Retrying.", action="post", endpoint="retry")
    if action.id == "reset_clip":
        return Fix(action.id, action.label, done="CLIP lights reset.", action="clip")
    if action.href:
        return Fix(action.id, action.label, href=root + action.href)
    # No inline version yet: the page that has the button (design 7).
    if action.id in ("send_app_values", "send_all"):
        return Fix(action.id, action.label, href=f"{root}/device#sync")
    if action.id == "switch_preset" and "preset" in p:
        return Fix(action.id, action.label, href=f"{root}/device?preset={int(p['preset']) + 1}#sync")
    if action.id == "edit_preset" and "preset" in p:
        return Fix(action.id, action.label, href=f"{root}/routing?preset={int(p['preset']) + 1}")
    return None


def _name(names: Sequence[str], index: int, prefix: str) -> str:
    return names[index] if 0 <= index < len(names) else f"{prefix} {index + 1}"


_ids = itertools.count(1)


def _db(value: float) -> str:
    """Route level text: "+4 dB", "−6 dB", "0 dB"."""
    v = round(value, 1)
    if v > 0:
        return f"+{v:g} dB"
    if v < 0:
        return f"{MINUS}{-v:g} dB"
    return "0 dB"


def build_flow(
    summary: PresetSummary,
    profile: DeviceProfile,
    *,
    root: str = "",
    level_anchor: str | None = None,
    staged: bool | None = None,
) -> dict[str, Any]:
    """Everything ``partials/flow.html`` draws, from one preset's summary (``staged``: see `output_blocks`)."""
    uid = f"flow{next(_ids)}"
    in_names = [i.name for i in summary.inputs]
    out_names = [o.name for o in summary.outputs]
    unused = [o.index for o in summary.outputs if o.unused]

    inputs = [_input_view(i, root, in_names, out_names) for i in summary.inputs]
    outputs = [_output_view(o, root, level_anchor, in_names, out_names, uid, staged) for o in summary.outputs]

    # Routes: (input, output, route level, input muted) for every enabled cell.
    routes = [(r.input, o.index, r.route_db, r.muted) for o in summary.outputs for r in o.total.routes]

    states = {"folded": flow_layout(profile, unused)}
    if unused:
        states["open"] = flow_layout(profile, (), fold_line=True)
    drawings = {
        name: _drawing(layout, routes, outputs, in_names, set(unused) if name == "folded" else set(), uid)
        for name, layout in states.items()
    }
    for view in inputs:
        view["tops"] = {name: _num(layout.inputs[view["index"]].top) for name, layout in states.items()}

    fold = None
    if unused:
        folded = [o for o in summary.outputs if o.unused]
        fold = {
            "names": [o.name for o in folded],
            "label": " · ".join(o.name for o in folded),
            "muted": all(o.muted for o in folded),
            "fed": sorted({in_names[r.input] for o in folded for r in o.total.routes}),
        }

    # The phone list: each input with the outputs it feeds; outputs nothing feeds come after.
    by_input: dict[int, list[dict[str, Any]]] = {i.index: [] for i in summary.inputs}
    for view in outputs:
        if view["unused"]:
            continue
        for src in view["sources"]:
            by_input.setdefault(src["input"], []).append(view)
    folded_by_input = {
        i.index: [o.name for o in summary.outputs if o.unused and any(r.input == i.index for r in o.total.routes)]
        for i in summary.inputs
    }
    return {
        "uid": uid,
        "inputs": inputs,
        "outputs": outputs,
        "fold": fold,
        "drawings": drawings,
        "width": ROUTE_W,
        "row_h": ROW_H,
        "row_gap": ROW_GAP,
        "fold_h": FOLD_H,
        "in_h": IN_H,
        "by_input": by_input,
        "folded_by_input": folded_by_input,
        "unrouted": [v for v in outputs if v["stub"] is not None],
        "names": {
            **{f"i{i.index}": i.name for i in summary.inputs},
            **{f"o{o.index}": o.name for o in summary.outputs},
        },
    }


def _input_view(i: InputSummary, root: str, in_names: Sequence[str], out_names: Sequence[str]) -> dict[str, Any]:
    note = next((p for p in i.problems if p.code == "input_unused"), None)
    details = ["EQ off" if i.eq == 0 else f"EQ {i.eq} band{'s' if i.eq != 1 else ''}"]
    if round(i.level_db, 1) != 0:
        details.append(f"level {_db(i.level_db)}")
    return {
        "index": i.index,
        "key": f"i{i.index}",
        "name": i.name,
        "href": f"{root}/inputs/{i.index + 1}",
        "muted": i.muted,
        "details": " · ".join(details),
        "sentence": i.sentence,
        "note": note,
        "note_fixes": fixes(note, root, in_names, out_names) if note else [],
        "sends_to": i.sends_to,
    }


def _output_view(
    o: OutputSummary,
    root: str,
    level_anchor: str | None,
    in_names: Sequence[str],
    out_names: Sequence[str],
    uid: str,
    staged: bool | None = None,
) -> dict[str, Any]:
    silencing = next((p for p in o.problems if p.silences), None)
    no_route = next((p for p in o.problems if p.code == "no_route"), None)
    muted_routed = next((p for p in o.problems if p.code == "muted_routed"), None)
    badge_problem = silencing or next((p for p in o.problems if o.badge_reason and p.text == o.badge_reason), None)
    sources = [
        {
            "input": r.input,
            "name": r.name,
            "level": _db(r.route_db),
            "muted": r.muted,
            "href": f"{root}/routing#in{r.input + 1}-out{o.index + 1}",
        }
        for r in o.total.routes
    ]
    route_fix = fixes(no_route, root, in_names, out_names) if no_route else []
    return {
        "index": o.index,
        "key": f"o{o.index}",
        "name": o.name,
        "href": f"{root}/outputs/{o.index + 1}",
        "badge": o.badge,
        "badge_glyph": o.badge_glyph,
        "badge_word": o.badge_word,
        "badge_reason": o.badge_reason,
        "badge_text": o.badge_reason or term(badge_term(o.badge)).short,
        "badge_term": badge_term(o.badge),
        "badge_fixes": fixes(badge_problem, root, in_names, out_names) if badge_problem else [],
        "sentence": o.sentence,
        "muted": o.muted,
        "unused": o.unused,
        "blocks": output_blocks(o, root, level_anchor, staged=staged),
        "sources": sources,
        "stub": no_route,
        "route_fix": next((f for f in route_fix if f.values), None),
        "stub_fixes": route_fix,
        "info": muted_routed,
        "info_short": _muted_routed_short(o) if muted_routed else "",
        "info_fixes": fixes(muted_routed, root, in_names, out_names) if muted_routed else [],
        "pid": f"{uid}-o{o.index}",
    }


def badge_term(badge: str) -> str:
    """The glossary entry of a health badge (``not_in_use`` -> ``b-not-in-use``). The first run's "? Unknown" has no
    badge entry of its own in the design's glossary: it is the Unknown status."""
    if badge == "unknown":
        return "st-unknown"
    return "b-" + badge.replace("_", "-")


def _muted_routed_short(o: OutputSummary) -> str:
    names = [r.name for r in o.total.routes]
    return f"Muted, but {_join(names)} {'is' if len(names) == 1 else 'are'} sent here"


def _join(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _drawing(
    layout: Layout,
    routes: Sequence[tuple[int, int, float, bool]],
    outputs: Sequence[dict[str, Any]],
    in_names: Sequence[str],
    folded: set[int],
    uid: str,
) -> dict[str, Any]:
    """The SVG of one diagram state: a line per route (routes into folded outputs share one line per input), and a
    dashed red stub into every output that nothing feeds."""
    lines: list[dict[str, Any]] = []
    to_fold: dict[int, list[tuple[float, bool]]] = {}
    for i, o, level, muted in routes:
        if o in folded:
            to_fold.setdefault(i, []).append((level, muted))
            continue
        slot = layout.outputs.get(o)
        if slot is None or i >= len(layout.inputs):
            continue
        lines.append(_line(layout, i, slot.center, level, muted, in_names, f"#in{i + 1}-out{o + 1}"))
    if layout.fold is not None:
        for i, cells in sorted(to_fold.items()):
            if i >= len(layout.inputs):
                continue
            levels = {round(level, 1) for level, _ in cells}
            muted = all(m for _, m in cells)
            line = _line(layout, i, layout.fold.center, cells[0][0], muted, in_names, "")
            if len(levels) > 1:
                line["label"] = ""
            lines.append(line)
    stubs = []
    for view in outputs:
        slot = layout.outputs.get(view["index"])
        if view["stub"] is None or slot is None:
            continue
        y = _num(slot.center)
        stubs.append({"output": view["index"], "d": f"M{_num(ROUTE_W * 0.45)} {y} H{_num(ROUTE_W - ARROW)}"})
    return {
        "height": _num(layout.height),
        "lines": lines,
        "stubs": stubs,
        "fold_top": _num(layout.fold.top) if layout.fold else None,
        "marker": uid,
    }


def _line(
    layout: Layout, i: int, y1: float, level: float, muted: bool, in_names: Sequence[str], anchor: str
) -> dict[str, Any]:
    y0 = layout.inputs[i].center
    lx, ly = _curve_point(0.72, y0, y1)
    label = f"({in_names[i]} muted)" if muted else _db(level)
    return {
        "input": i,
        "d": route_path(y0, y1),
        "label": label,
        "x": _num(lx),
        "y": _num(ly - 5),
        "muted": muted,
        "anchor": anchor,
    }


# --- page glue --------------------------------------------------------------------------------------------------------

_ANCHOR = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


async def flow_context(request: Request, session: Any, *, level_anchor: str | None = None) -> dict[str, Any]:
    """The diagram's template context for the playing preset (``flow`` is None until the device state is loaded)."""
    root = request.scope.get("root_path", "").rstrip("/")
    serial = session.serial if session is not None else None
    anchor = level_anchor if level_anchor and _ANCHOR.match(level_anchor) else None
    query = f"?serial={serial}" if serial is not None else ""
    if anchor:
        query += ("&" if query else "?") + f"level_anchor={anchor}"
    ctx: dict[str, Any] = {"flow": None, "flow_url": f"{root}/ui/flow{query}", "flow_edit_url": None}
    if session is None or not hasattr(session, "ensure_loaded"):
        return ctx
    try:
        await session.ensure_loaded()
    except Exception:
        return ctx
    summary = session_summary(session)
    if summary is None or summary.preset is None:
        return ctx
    master = session.master
    settings = getattr(session, "settings", None) or getattr(request.app.state, "settings", None)
    try:
        staged = bool(session.sync_summary(summary.preset).get("staged"))
    except Exception:
        staged = None
    ctx.update(
        flow=build_flow(summary, session.profile, root=root, level_anchor=anchor, staged=staged),
        flow_preset=summary.preset,
        flow_master_mute=bool(master.mute) if master is not None and master.mute is not None else None,
        flow_edit_url=f"{root}/ui/devices/{serial}/presets/{summary.preset}/edit",
        flow_floor=float(getattr(settings, "guidance_no_signal_dbfs", -90.0)),
    )
    return ctx


@router.get("/ui/flow", response_class=HTMLResponse)
async def flow_fragment(request: Request, serial: int | None = None, level_anchor: str | None = None) -> HTMLResponse:
    """The diagram, re-fetched by the page on SSE ``desired`` / ``sync`` / ``verify`` events and when the playing
    preset changes."""
    session = devices.pick_session(request, serial)
    ctx = await flow_context(request, session, level_anchor=level_anchor)
    response = templates.TemplateResponse(request, "partials/flow.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


__all__ = [
    "BLOCK_ORDER",
    "Block",
    "Layout",
    "Slot",
    "block_href",
    "block_sync",
    "build_flow",
    "flow_context",
    "flow_layout",
    "output_blocks",
    "route_path",
    "router",
]
