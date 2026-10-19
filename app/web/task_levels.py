"""Set levels: watch the meters, avoid clipping (phase 9 design §14.6).

Seven steps: start quiet (Volume down to −40 dB), play something loud you know (the input meters, large, with the
target zone of −12 to −3 dBFS shaded; it waits for signal), check the inputs, watch the outputs for 30 s, balance,
everyday volume, done.

The meters live in the browser (static/js/task_levels.js): a step watches the level feed, keeps each channel's peak
and counts how often it reached CLIP, and hands those to the next step as ``pk_i`` / ``cl_i`` (inputs) or ``pk_o`` /
``cl_o`` (outputs), comma-separated. The feedback is worked out here, by the pure functions below. Without JavaScript
there are no readings and the steps say "can't tell".

Unlike the other wizards, the fix buttons here act at once (design §14.1) and say so: Set Volume to −40 dB, Lower In 1
by 3 dB, Lower Shakers by 3 dB, Lower the route instead, Add a compressor at −3 dBFS, and the everyday Volume steps.
Each is listed under "Changes so far", and Undo all puts back exactly what this run wrote (field by field, skipping
what changed elsewhere since).

*Uncertain* (design §19): the target zone and the CLIP threshold are rules of thumb, and the mock's meters are flat
unless its level pattern is on (``MINIDSPD_MOCK_LEVELS``).
"""

import contextlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.sync.session import ConfirmationRequired, DesiredValidationError, MasterValidationError
from app.sync.summary import ARROW, MINUS, PresetSummary, Thresholds, plain
from app.web.tasks import (
    Step,
    TaskCtx,
    UndoRecord,
    Wizard,
    apply_edits,
    apply_master,
    confirm_error,
    confirmed,
    float_param,
    int_param,
    possessive,
    undo,
    write_error,
)
from app.web.templating import meter_fraction

STEPS = ("Start quiet", "Play", "Inputs", "Outputs", "Balance", "Volume", "Done")
QUIET_DB = -40.0  # step 1 turns Volume down to this
TARGET_LOW_DBFS = -12.0  # the target zone for input peaks (a rule of thumb)
TARGET_HIGH_DBFS = -3.0
CLIP_DBFS = -1.0  # a reading at or above this is CLIP (static/js/meters.js CLIP_DBFS)
SIGNAL_MARGIN_DB = 30.0  # above the no-signal floor by this much counts as signal (as Something wrong? reads it)
STEP_DB = 3.0  # the fix buttons lower by this much
COMP_THRESHOLD_DBFS = -3.0
COMP_RATIO = 10.0
WATCH_S = 30  # how long step 4 watches the outputs
WAIT_S = 15  # step 2 says "No signal" after this long without any
VOLUME_STEPS = (-3.0, -1.0, 1.0, 3.0)
USUAL_KEY = "ui.usual_volume.{serial}"  # the remembered everyday Volume (Settings' typical band shows it)
MAX_CHANGES = 30

OK, CLOSE, HOT, QUIET, NONE, UNKNOWN = "ok", "close", "hot", "quiet", "none", "unknown"


def dbfs(value: float) -> str:
    return f"{MINUS if value < 0 else ''}{plain(abs(value), 1)} dBFS" if round(value, 1) != 0 else "0 dBFS"


def db(value: float) -> str:
    """``−40 dB``, ``+3 dB``."""
    if round(value, 1) == 0:
        return "0 dB"
    return f"{MINUS if value < 0 else '+'}{plain(abs(value), 1)} dB"


def times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


# --- readings -------------------------------------------------------------------------------------------------------


def parse_readings(text: str | None, count: int) -> list[float | None]:
    """``"-8.2,,-120"`` -> ``[-8.2, None, -120.0]`` (padded or cut to ``count``)."""
    values: list[float | None] = []
    for part in (text or "").split(",")[:count]:
        try:
            value = float(part.strip().replace(MINUS, "-"))
        except ValueError:
            values.append(None)
            continue
        values.append(min(0.0, value) if math.isfinite(value) else None)
    return values + [None] * (count - len(values))


def parse_counts(text: str | None, count: int) -> list[int]:
    out: list[int] = []
    for part in (text or "").split(",")[:count]:
        try:
            out.append(max(0, min(9999, int(part.strip()))))
        except ValueError:
            out.append(0)
    return out + [0] * (count - len(out))


def has_signal(peak: float | None, th: Thresholds) -> bool:
    return peak is not None and peak > th.no_signal_dbfs + SIGNAL_MARGIN_DB


@dataclass(frozen=True)
class Fix:
    """A one-tap fix that applies at once: ``values`` are preset paths, ``done`` what it did in words."""

    id: str
    label: str
    values: dict[str, Any]
    done: str
    preview: str = ""


@dataclass(frozen=True)
class Reading:
    index: int
    name: str
    status: str  # ok | close | hot | quiet | none | unknown
    text: str
    peak: float | None = None
    clips: int = 0
    detail: str = ""
    fixes: tuple[Fix, ...] = ()

    @property
    def glyph(self) -> str:
        return {OK: "✓", CLOSE: "⚠", HOT: "✗", QUIET: "✓", NONE: "·", UNKNOWN: "?"}[self.status]

    @property
    def word(self) -> str:
        return {OK: "OK", CLOSE: "Close to CLIP", HOT: "CLIP", QUIET: "OK", NONE: "No signal", UNKNOWN: "Can't tell"}[
            self.status
        ]


def input_feedback(
    summary: PresetSummary,
    peaks: Sequence[float | None],
    clips: Sequence[int],
    th: Thresholds,
    *,
    gain_min: float,
    measured: bool = True,
) -> list[Reading]:
    """Step 3: each input's peak against the target zone (−12 to −3 dBFS), CLIP first (design §14.6)."""
    out = []
    for i in summary.inputs:
        peak = peaks[i.index] if i.index < len(peaks) else None
        clip = clips[i.index] if i.index < len(clips) else 0
        if not measured:
            out.append(Reading(i.index, i.name, UNKNOWN, f"{i.name}: can't tell (the meters need JavaScript)"))
            continue
        if clip or (peak is not None and peak >= CLIP_DBFS):
            fixes: tuple[Fix, ...] = ()
            gain = summary_input_gain(summary, i.index)
            if gain is not None and gain - STEP_DB >= gain_min:
                new = round(gain - STEP_DB, 2)
                fixes = (
                    Fix(
                        "lower_in",
                        f"Lower {i.name} by {plain(STEP_DB, 1)} dB",
                        {f"inputs.{i.index}.gain_db": new},
                        f"{i.name} input level {db(gain)} {ARROW} {db(new)}",
                    ),
                )
            out.append(
                Reading(
                    i.index,
                    i.name,
                    HOT,
                    f"{i.name} hits CLIP: the signal arriving is too hot.",
                    peak,
                    max(clip, 1),
                    "Lower the player's or AV receiver's level; the miniDSP can't undo clipping that happens before "
                    "it.",
                    fixes,
                )
            )
        elif not has_signal(peak, th):
            out.append(Reading(i.index, i.name, NONE, f"{i.name}: no signal.", peak))
        elif peak is not None and peak >= TARGET_HIGH_DBFS:
            out.append(
                Reading(
                    i.index,
                    i.name,
                    CLOSE,
                    f"{i.name} peaks at {dbfs(peak)}: just below CLIP.",
                    peak,
                    detail="Lower the player's level a little if you can.",
                )
            )
        elif peak is not None and peak >= TARGET_LOW_DBFS:
            out.append(Reading(i.index, i.name, OK, f"{i.name} peaks at {dbfs(peak)}", peak))
        else:
            low = dbfs(peak) if peak is not None else "a low level"
            out.append(
                Reading(
                    i.index,
                    i.name,
                    QUIET,
                    f"{i.name} peaks at {low}: on the quiet side, which is fine.",
                    peak,
                    detail=f"If you can, raise the player's level a little: the target is {dbfs(TARGET_LOW_DBFS)} to "
                    f"{dbfs(TARGET_HIGH_DBFS)}.",
                )
            )
    return out


def summary_input_gain(summary: PresetSummary, index: int) -> float | None:
    """An input's level, from any route total it feeds (the summary keeps it per route)."""
    for o in summary.outputs:
        for r in o.total.routes:
            if r.input == index:
                return r.input_db
    return None


def output_results(
    summary: PresetSummary,
    peaks: Sequence[float | None],
    clips: Sequence[int],
    th: Thresholds,
    *,
    gain_min: float,
    doc: Any = None,
    measured: bool = True,
) -> list[Reading]:
    """Step 4: each output in use: its peak and CLIP count, total and headroom, and fixes for one that clipped."""
    out = []
    for o in summary.outputs:
        if o.unused:
            continue
        peak = peaks[o.index] if o.index < len(peaks) else None
        clip = clips[o.index] if o.index < len(clips) else 0
        t = o.total
        if not measured:
            out.append(Reading(o.index, o.name, UNKNOWN, f"{o.name}: can't tell (the meters need JavaScript)"))
            continue
        if clip or (peak is not None and peak >= CLIP_DBFS):
            n = max(clip, 1)
            fixes = _output_fixes(o, gain_min=gain_min, doc=doc)
            out.append(
                Reading(
                    o.index,
                    o.name,
                    HOT,
                    f"{o.name} clipped {times(n)} at {t.text} total.",
                    peak,
                    n,
                    o.headroom.text,
                    fixes,
                )
            )
        elif has_signal(peak, th):
            assert peak is not None
            out.append(
                Reading(o.index, o.name, OK, f"{o.name} peaks at {dbfs(peak)}, no CLIP", peak, detail=o.headroom.text)
            )
        else:
            why = " (it is muted)" if o.muted else ""
            out.append(Reading(o.index, o.name, NONE, f"{o.name}: no signal{why}.", peak))
    return out


def _output_fixes(o: Any, *, gain_min: float, doc: Any) -> tuple[Fix, ...]:
    t = o.total
    found: list[Fix] = []
    new_out = round(t.output - STEP_DB, 2)
    if new_out >= gain_min:
        found.append(
            Fix(
                "out",
                f"Lower {o.name} by {plain(STEP_DB, 1)} dB",
                {f"outputs.{o.index}.gain_db": new_out},
                f"{o.name} output level {db(t.output)} {ARROW} {db(new_out)}",
            )
        )
    if t.routes and all(r.route_db - STEP_DB >= gain_min for r in t.routes):
        values = {f"inputs.{r.input}.routing.{o.index}.gain_db": round(r.route_db - STEP_DB, 2) for r in t.routes}
        words = ", ".join(f"{r.name} {ARROW} {o.name}" for r in t.routes)
        found.append(
            Fix(
                "route",
                "Lower the route instead",
                values,
                f"the route {words} {MINUS}{plain(STEP_DB, 1)} dB",
                preview=f"Lowers the route {words} by {plain(STEP_DB, 1)} dB; {possessive(o.name)} output level stays.",
            )
        )
    comp = doc.outputs[o.index].compressor if doc is not None and o.index < len(doc.outputs) else None
    if comp is None or comp.bypass or comp.threshold_db > COMP_THRESHOLD_DBFS:
        path = f"outputs.{o.index}.compressor"
        found.append(
            Fix(
                "comp",
                f"Add a compressor at {dbfs(COMP_THRESHOLD_DBFS)}…",
                {
                    f"{path}.bypass": False,
                    f"{path}.threshold_db": COMP_THRESHOLD_DBFS,
                    f"{path}.ratio": COMP_RATIO,
                },
                f"{possessive(o.name)} compressor on at {dbfs(COMP_THRESHOLD_DBFS)}",
                preview=f"Turns on {possessive(o.name)} compressor (limiter) at {dbfs(COMP_THRESHOLD_DBFS)}, "
                f"{plain(COMP_RATIO, 1)}:1, so peaks above {dbfs(COMP_THRESHOLD_DBFS)} are held down. Loud peaks sound "
                "a little flatter.",
            )
        )
    return tuple(found)


def done_lines(
    in_peaks: Sequence[float | None],
    out_results: Sequence[Reading] | None,
    volume: float | None,
    usual: float | None,
    th: Thresholds,
) -> list[str]:
    """Step 7 in words: "Inputs peak around −8 dBFS. No output clipped. Usual volume −22 dB."."""
    lines = []
    heard = [p for p in in_peaks if has_signal(p, th)]
    if heard:
        lines.append(f"Inputs peak around {dbfs(max(p for p in heard if p is not None))}.")
    else:
        lines.append("The inputs weren't measured.")
    if out_results is None or all(r.status == UNKNOWN for r in out_results):
        lines.append("The outputs weren't watched.")
    else:
        hot = [r.name for r in out_results if r.status == HOT]
        lines.append(f"{', '.join(hot)} clipped in the last check." if hot else "No output clipped.")
    if usual is not None:
        lines.append(f"Usual volume {plain(usual, 1).replace('-', MINUS)} dB.")
    elif volume is not None:
        lines.append(f"Volume {plain(volume, 1).replace('-', MINUS)} dB.")
    return lines


def zone() -> dict[str, float]:
    """The target zone's position on the meters' scale, in percent (static/js/task_levels.js draws the bars)."""
    low, high = meter_fraction(TARGET_LOW_DBFS), meter_fraction(TARGET_HIGH_DBFS)
    return {
        "left": round(low * 100, 2),
        "width": round((high - low) * 100, 2),
        "clip": round(meter_fraction(-1) * 100, 2),
    }


# --- the wizard -----------------------------------------------------------------------------------------------------


@dataclass
class Run:
    """This run's answers and history, carried in hidden fields."""

    pk_i: str = ""
    cl_i: str = ""
    pk_o: str = ""
    cl_o: str = ""
    usual: float | None = None
    changes: list[str] = field(default_factory=list)
    record: UndoRecord | None = None
    secs: int = WATCH_S

    @classmethod
    def from_params(cls, params: Mapping[str, str]) -> "Run":
        try:
            changes = json.loads(params.get("changes") or "[]")
        except ValueError:
            changes = []
        changes = [str(c) for c in changes if isinstance(c, str)][:MAX_CHANGES] if isinstance(changes, list) else []
        secs = int_param(params, "secs", WATCH_S) or WATCH_S
        return cls(
            pk_i=(params.get("pk_i") or "")[:200],
            cl_i=(params.get("cl_i") or "")[:200],
            pk_o=(params.get("pk_o") or "")[:200],
            cl_o=(params.get("cl_o") or "")[:200],
            usual=float_param(params, "usual"),
            changes=changes,
            record=UndoRecord.from_json(params.get("undo")),
            secs=max(3, min(120, secs)),
        )

    def params(self) -> dict[str, str]:
        out = {
            k: v for k, v in (("pk_i", self.pk_i), ("cl_i", self.cl_i), ("pk_o", self.pk_o), ("cl_o", self.cl_o)) if v
        }
        if self.usual is not None:
            out["usual"] = f"{self.usual:g}"
        if self.changes:
            out["changes"] = json.dumps(self.changes, separators=(",", ":"), ensure_ascii=False)
        if self.record:
            out["undo"] = self.record.to_json()
        if self.secs != WATCH_S:
            out["secs"] = str(self.secs)
        return out

    def add(self, record: UndoRecord | None, change: str) -> None:
        if record:
            self.record = self.record.merged(record) if self.record and self.record.preset == record.preset else record
        self.changes = [*self.changes, change][-MAX_CHANGES:]


def _master(ctx: TaskCtx) -> Any:
    return ctx.session.master if ctx.session is not None else None


def _th(ctx: TaskCtx) -> Thresholds:
    return Thresholds.from_settings(ctx.settings)


def _base(ctx: TaskCtx, run: Run, *, error: str | None = None, message: str | None = None) -> dict[str, Any]:
    return {
        "error": error,
        "message": message,
        "changes": run.changes,
        "no_device": ctx.session is None or ctx.summary() is None,
    }


def _step(ctx: TaskCtx, run: Run, number: int, title: str, template: str, data: dict[str, Any], **kw: Any) -> Step:
    record = run.record if run.record else None
    return Step(number, title, template, run.params(), data, undo=record, **kw)


def _step_quiet(ctx: TaskCtx, run: Run, **kw: Any) -> Step:
    master = _master(ctx)
    volume = master.volume if master is not None else None
    data = {
        **_base(ctx, run, error=kw.pop("error", None), message=kw.pop("message", None)),
        "volume": volume,
        "volume_text": db(volume) if volume is not None else None,
        "quiet": QUIET_DB,
        "quiet_text": db(QUIET_DB),
        "is_quiet": volume is not None and volume <= QUIET_DB + 0.01,
        "muted": bool(master is not None and master.mute),
    }
    return _step(ctx, run, 1, "Start quiet", "tasks/levels/step_quiet.html", data, **kw)


def _channel_names(summary: PresetSummary | None, side: str) -> list[str]:
    if summary is None:
        return []
    return [i.name for i in summary.inputs] if side == "i" else [o.name for o in summary.outputs]


def _step_play(ctx: TaskCtx, run: Run, **kw: Any) -> Step:
    summary = ctx.summary()
    master = _master(ctx)
    session = ctx.session
    data = {
        **_base(ctx, run, error=kw.pop("error", None), message=kw.pop("message", None)),
        "watch": {
            "side": "i",
            "names": _channel_names(summary, "i"),
            "secs": 0,
            "wait": WAIT_S,
            "floor": _th(ctx).no_signal_dbfs + SIGNAL_MARGIN_DB,
        },
        "zone": zone(),
        "source_choices": list(session.profile.sources) if session is not None else [],
        "source": master.source if master is not None else None,
        "diagnose_href": ctx.task_href("diagnose", what="all"),
    }
    return _step(ctx, run, 2, "Play something you know", "tasks/levels/step_play.html", data, **kw)


def _gain_min(ctx: TaskCtx) -> float:
    return ctx.session.profile.gain_min_db if ctx.session is not None else -72.0


def _inputs(ctx: TaskCtx, run: Run, summary: PresetSummary) -> list[Reading]:
    n = len(summary.inputs)
    return input_feedback(
        summary,
        parse_readings(run.pk_i, n),
        parse_counts(run.cl_i, n),
        _th(ctx),
        gain_min=_gain_min(ctx),
        measured=bool(run.pk_i),
    )


def _step_inputs(ctx: TaskCtx, run: Run, **kw: Any) -> Step:
    summary = ctx.summary()
    readings = _inputs(ctx, run, summary) if summary is not None else []
    data = {**_base(ctx, run, error=kw.pop("error", None), message=kw.pop("message", None)), "readings": readings}
    return _step(ctx, run, 3, "Check the inputs", "tasks/levels/step_inputs.html", data, **kw)


def _outputs(ctx: TaskCtx, run: Run, summary: PresetSummary) -> list[Reading]:
    n = len(summary.outputs)
    doc = None
    with contextlib.suppress(Exception):
        doc = ctx.session.desired(summary.preset or 0) if ctx.session is not None else None
    return output_results(
        summary,
        parse_readings(run.pk_o, n),
        parse_counts(run.cl_o, n),
        _th(ctx),
        gain_min=_gain_min(ctx),
        doc=doc,
        measured=bool(run.pk_o),
    )


def _step_outputs(ctx: TaskCtx, run: Run, phase: str, **kw: Any) -> Step:
    summary = ctx.summary()
    data: dict[str, Any] = {
        **_base(ctx, run, error=kw.pop("error", None), message=kw.pop("message", None)),
        "phase": phase,
        "zone": zone(),
    }
    if phase == "result" and summary is not None:
        data["results"] = _outputs(ctx, run, summary)
    else:
        names = _channel_names(summary, "o")
        unused = [o.unused for o in summary.outputs] if summary is not None else []
        data["watch"] = {"side": "o", "names": names, "secs": run.secs, "wait": 0, "floor": -1000, "skip": unused}
        data["totals"] = [o.total.text for o in summary.outputs] if summary is not None else []
    params = {**run.params(), "phase": phase}
    record = run.record if run.record else None
    return Step(4, "Watch the outputs", "tasks/levels/step_outputs.html", params, data, undo=record, **kw)


def _step_balance(ctx: TaskCtx, run: Run, **kw: Any) -> Step:
    data = {
        **_base(ctx, run, error=kw.pop("error", None), message=kw.pop("message", None)),
        "louder_href": ctx.task_href("louder"),
    }
    return _step(ctx, run, 5, "Balance", "tasks/levels/step_balance.html", data, **kw)


async def _usual(ctx: TaskCtx, run: Run) -> float | None:
    if run.usual is not None:
        return run.usual
    session = ctx.session
    repo = getattr(session, "repo", None)
    if session is None or repo is None:
        return None
    with contextlib.suppress(Exception):
        value = await repo.get_setting(USUAL_KEY.format(serial=session.serial))
        if isinstance(value, int | float) and math.isfinite(value):
            return float(value)
    return None


async def _step_volume(ctx: TaskCtx, run: Run, **kw: Any) -> Step:
    master = _master(ctx)
    volume = master.volume if master is not None else None
    settings = ctx.settings
    low = float(getattr(settings, "guidance_volume_typical_min_db", -40.0))
    high = float(getattr(settings, "guidance_volume_typical_max_db", -10.0))
    session = ctx.session
    vmin = session.profile.master_volume_min_db if session is not None else -127.0
    vmax = session.profile.master_volume_max_db if session is not None else 0.0
    usual = await _usual(ctx, run)

    def pos(v: float) -> float:
        return round(100 * (min(vmax, max(vmin, v)) - vmin) / (vmax - vmin), 2)

    data = {
        **_base(ctx, run, error=kw.pop("error", None), message=kw.pop("message", None)),
        "volume": volume,
        "volume_text": db(volume) if volume is not None else "unknown",
        "band_text": f"{db(low)} and {db(high)}",
        "band": {"left": pos(low), "width": round(pos(high) - pos(low), 2)},
        "at": pos(volume) if volume is not None else None,
        "usual": usual,
        "usual_text": db(usual) if usual is not None else None,
        "usual_at": pos(usual) if usual is not None else None,
        "vol_steps": [
            {"delta": d, "label": f"{'+' if d > 0 else MINUS}{plain(abs(d), 1)} dB", "ok": volume is not None}
            for d in VOLUME_STEPS
        ],
        "confirm_needed": kw.pop("confirm_needed", None),
    }
    return _step(ctx, run, 6, "Everyday volume", "tasks/levels/step_volume.html", data, **kw)


async def _step_done(ctx: TaskCtx, run: Run, **kw: Any) -> Step:
    summary = ctx.summary()
    master = _master(ctx)
    outputs = _outputs(ctx, run, summary) if summary is not None and run.pk_o else None
    n_in = len(summary.inputs) if summary is not None else 0
    lines = done_lines(
        parse_readings(run.pk_i, n_in),
        outputs,
        master.volume if master is not None else None,
        await _usual(ctx, run),
        _th(ctx),
    )
    data = {
        **_base(ctx, run, error=kw.pop("error", None), message=kw.pop("message", None)),
        "lines": lines,
        "save_href": ctx.task_href("profiles", do="save"),
    }
    return _step(ctx, run, 7, "Done", "tasks/levels/step_done.html", data, **kw)


async def render(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    run = Run.from_params(params)
    step = int_param(params, "step", 1) or 1
    if ctx.session is None or ctx.summary() is None or step <= 1:
        return _step_quiet(ctx, run)
    if step == 2:
        return _step_play(ctx, run)
    if step == 3:
        return _step_inputs(ctx, run)
    if step == 4:
        return _step_outputs(ctx, run, "result" if params.get("phase") == "result" else "watch")
    if step == 5:
        return _step_balance(ctx, run)
    if step == 6:
        return await _step_volume(ctx, run)
    return await _step_done(ctx, run)


def _fail(ctx: TaskCtx, exc: Exception) -> str:
    if isinstance(exc, ConfirmationRequired):
        return confirm_error(exc)
    if isinstance(exc, DesiredValidationError | MasterValidationError) and ctx.session is not None:
        return write_error(ctx.session, exc)
    return f"It couldn't be sent: {exc or type(exc).__name__}."


async def _rerender(ctx: TaskCtx, run: Run, step: int, params: Mapping[str, str], **kw: Any) -> Step:
    if step == 1:
        return _step_quiet(ctx, run, **kw)
    if step == 2:
        return _step_play(ctx, run, **kw)
    if step == 3:
        return _step_inputs(ctx, run, **kw)
    if step == 4:
        return _step_outputs(ctx, run, "result" if params.get("phase") == "result" else "watch", **kw)
    if step == 5:
        return _step_balance(ctx, run, **kw)
    if step == 6:
        return await _step_volume(ctx, run, **kw)
    return await _step_done(ctx, run, **kw)


async def act(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    """The live actions (each applies now): ``quiet``, ``unmute``, ``source``, ``lower_in``, ``fix`` (out / route /
    comp), ``vol`` (``delta``), ``usual``; and ``undo`` (everything this run wrote)."""
    session = ctx.session
    run = Run.from_params(params)
    step = int_param(params, "step", 1) or 1
    summary = ctx.summary()
    if session is None or summary is None:
        return await render(ctx, params)
    preset = summary.preset if summary.preset is not None else 0
    op = params.get("op")
    if op == "undo":
        if not run.record:
            return await _rerender(ctx, run, step, params, message="Nothing to undo.")
        # A run takes minutes, so its Undo all isn't limited to the shell's 30 s; what changed elsewhere since is
        # still left alone (field by field).
        result = await undo(session, run.record, now=run.record.at)
        ctx.forget()
        if result.error:
            return await _rerender(ctx, run, step, params, error=result.error)
        run.record, run.changes = None, []
        return await _rerender(ctx, run, step, params, message=result.message, applied=True)
    if op == "usual":
        master = _master(ctx)
        volume = master.volume if master is not None else None
        if volume is None:
            return await _step_volume(ctx, run, error="The Volume isn't known yet.")
        repo = getattr(session, "repo", None)
        if repo is not None:
            with contextlib.suppress(Exception):
                await repo.set_setting(USUAL_KEY.format(serial=session.serial), volume)
        run.usual = volume
        return await _step_volume(ctx, run, message=f"Remembered {db(volume)} as your usual level.")
    try:
        record, change, next_kw = await _live(ctx, run, op, params, preset, summary)
    except (ConfirmationRequired, DesiredValidationError, MasterValidationError) as exc:
        extra = {"confirm_needed": params.get("delta")} if isinstance(exc, ConfirmationRequired) and op == "vol" else {}
        return await _rerender(ctx, run, step, params, error=_fail(ctx, exc), **extra)
    except Exception as exc:  # the daemon is down, a switch is running, ...
        return await _rerender(ctx, run, step, params, error=_fail(ctx, exc))
    if change is None:
        return await render(ctx, params)
    run.add(record, change)
    ctx.forget()
    toast = f"Set levels: {change}."
    if op == "fix":
        # Measure again after a fix: the outputs are watched afresh.
        run.pk_o, run.cl_o = "", ""
        return _step_outputs(
            ctx, run, "watch", message=f"Done: {change}. Watching again…", applied=True, toast=toast, **next_kw
        )
    return await _rerender(ctx, run, step, params, message=f"Done: {change}.", applied=True, toast=toast, **next_kw)


async def _live(
    ctx: TaskCtx, run: Run, op: str | None, params: Mapping[str, str], preset: int, summary: PresetSummary
) -> tuple[UndoRecord | None, str | None, dict[str, Any]]:
    """Apply one live action: (its undo record, what it did in words, extra step arguments)."""
    session = ctx.session
    assert session is not None
    master = _master(ctx)
    if op == "quiet":
        volume = master.volume if master is not None else None
        if volume is not None and volume <= QUIET_DB:
            return None, None, {}
        record = await apply_master(session, preset, confirm=False, volume=QUIET_DB)
        was = f"{db(volume)} {ARROW} " if volume is not None else ""
        return record, f"Volume {was}{db(QUIET_DB)}", {}
    if op == "unmute":
        record = await apply_master(session, preset, confirm=False, mute=False)
        return record, "Mute all off", {}
    if op == "source":
        choice = params.get("source")
        if choice not in session.profile.sources:
            return None, None, {}
        await session.set_source(str(choice), source="ui")
        return None, f"Source {choice}", {}
    if op == "vol":
        volume = master.volume if master is not None else None
        delta = float_param(params, "delta")
        if volume is None or delta is None or not any(abs(delta - d) < 0.01 for d in VOLUME_STEPS):
            return None, None, {}
        p = session.profile
        new = min(p.master_volume_max_db, max(p.master_volume_min_db, round(volume + delta, 1)))
        record = await apply_master(session, preset, confirm=confirmed(params), volume=new)
        return record, f"Volume {db(volume)} {ARROW} {db(new)}", {}
    if op == "lower_in":
        readings = _inputs(ctx, run, summary)
        index = int_param(params, "input")
        fix = next((f for r in readings if r.index == index for f in r.fixes), None)
        if fix is None:
            return None, None, {}
        record = await apply_edits(session, preset, fix.values, confirm=False)
        return record, fix.done, {}
    if op == "fix":
        index = int_param(params, "output")
        o = next((x for x in summary.outputs if x.index == index and not x.unused), None)
        fixes = _output_fixes(o, gain_min=_gain_min(ctx), doc=session.desired(preset)) if o is not None else ()
        fix = next((f for f in fixes if f.id == params.get("fix")), None)
        if fix is None:
            return None, None, {}
        record = await apply_edits(session, preset, fix.values, confirm=False)
        return record, fix.done, {}
    return None, None, {}


WIZARD = Wizard(
    id="set-levels",
    label="Set levels",
    steps=STEPS,
    render=render,
    act=act,
    intro="Watches the meters while you play something loud, and helps you avoid clipping and pick an everyday volume.",
)

__all__ = [
    "WIZARD",
    "Reading",
    "done_lines",
    "input_feedback",
    "output_results",
    "parse_counts",
    "parse_readings",
]
