"""Fix a boom (phase 9 design §14.2): one EQ cut (or boost) on one output, without a minus sign to type.

Output · Frequency · Amount · Check · Done. The pure parts:

- `free_band` picks the band to use: the first unused one (nothing designed in it), else the first switched-off one
  (its old setting is replaced, and the Check step says so). Bands managed by another tool are never used. With all
  10 in use there is nothing to pick, and the wizard says so instead of overwriting one.
- `half_depth_range` is the range the width choices state ("Medium (Q 4): about 35–45 Hz"): where the cut is at least
  half its depth, worked out from the same RBJ peaking design the device gets (`app.dsp.design.peaking`).
- `preview` draws the output's EQ and band filters now and with the new band (5–300 Hz for a low note), as an SVG
  with a one-line reading.

Apply sends the whole band (Peaking, frequency, Q, gain, switched on) in one edit through `app.sync.filters`, as the
EQ table does, so the big-step guard and the boost limit behave as usual; Undo puts the band back as it was.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from app.dsp import response as resp
from app.dsp.design import FS, peaking
from app.model.config_doc import OutputCh, PeqBand
from app.model.filters import FilterDesign
from app.sync import filters as svc
from app.sync.session import ConfirmationRequired, DesiredValidationError, MasterValidationError
from app.sync.summary import (
    MINUS,
    PresetSummary,
    analyse_band,
    analyse_eq,
    eq_clause,
    hz,
    hz_range,
    is_empty,
    plain,
)
from app.web.tasks import (
    Step,
    TaskCtx,
    UndoRecord,
    Wizard,
    confirm_error,
    confirmed,
    float_param,
    int_param,
    undo,
    values_of,
    write_error,
)

STEPS = ("Output", "Frequency", "Amount", "Check", "Done")
CUT, BOOST = "cut", "boost"
QUICK_DB = (2.0, 4.0, 6.0)
DEFAULT_DB = 4.0
MAX_DB = 15.0  # a "boom" fix; bigger changes belong in the EQ table
FREQ_MIN, FREQ_MAX = 5.0, 20_000.0
SUB_VIEW = (5.0, 300.0)
FULL_VIEW = (20.0, 20_000.0)
EPS = 1e-9


@dataclass(frozen=True)
class Width:
    id: str
    label: str
    q: float
    note: str = ""


WIDTHS = (
    Width("narrow", "Narrow", 8.0),
    Width("medium", "Medium", 4.0, "good for booms"),
    Width("wide", "Wide", 1.4),
)
WIDTH_BY_ID = {w.id: w for w in WIDTHS}
DEFAULT_WIDTH = "medium"


# --- pure parts -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class FreeBand:
    index: int  # 0-based
    replaces: str | None  # what the switched-off band held ("1 EQ cut at 63 Hz"), None for an unused band


def _unused(band: PeqBand) -> bool:
    """Nothing worth keeping: a flat band, or a switched-off all-zero one (how a factory-reset 2x4HD reports them)."""
    if band.bypass and is_empty(band.compiled):
        return True
    return band.compiled.is_passthrough and band.design.type in ("passthrough", "raw")


def free_bands(bands: Sequence[PeqBand]) -> list[int]:
    """Bands the wizard may use: not managed by another tool, and unused or switched off."""
    return [k for k, b in enumerate(bands) if not b.external and (_unused(b) or b.bypass)]


def free_band(bands: Sequence[PeqBand]) -> FreeBand | None:
    """The band a new cut goes into: the first unused band, else the first switched-off one; None when all are in
    use."""
    for k, band in enumerate(bands):
        if not band.external and _unused(band):
            return FreeBand(k, None)
    for k, band in enumerate(bands):
        if not band.external and band.bypass:
            held = eq_clause(analyse_eq([band.model_copy(update={"bypass": False})])) or "a filter"
            return FreeBand(k, held.removeprefix("1 "))
    return None


def _mag_db(freq: float, q: float, gain_db: float, f: float, fs: float) -> float:
    tf = resp.biquad_tf(peaking(freq, q, gain_db, fs), np.array([f]), fs, float32=False)
    return float(20 * np.log10(abs(tf.h[0])))


def half_depth_range(freq: float, q: float, gain_db: float, fs: float = FS) -> tuple[float, float]:
    """The frequencies either side of ``freq`` where a peaking cut or boost reaches half its depth in dB."""
    half = abs(gain_db) / 2

    def inside(f: float) -> bool:
        return abs(_mag_db(freq, q, gain_db, f, fs)) >= half

    def edge(far: float) -> float:
        lo, hi = math.log(freq), math.log(far)  # inside at lo, outside (or the limit) at hi
        if inside(far):
            return far
        for _ in range(60):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if inside(math.exp(mid)) else (lo, mid)
        return math.exp((lo + hi) / 2)

    return edge(max(freq / 64, 0.5)), edge(min(freq * 64, fs / 2 * 0.999))


def about_hz(freq: float) -> float:
    """Rounded for "about": whole hertz below 100 Hz, else two significant figures."""
    if freq < 100:
        return float(math.floor(freq + 0.5))
    digits = 2 - int(math.floor(math.log10(freq))) - 1
    return float(round(freq, digits))


def width_text(width: Width, freq: float, gain_db: float, fs: float = FS) -> str:
    """ "Medium (Q 4): about 35–45 Hz, good for booms"."""
    lo, hi = half_depth_range(freq, width.q, gain_db, fs)
    text = f"{width.label} (Q {width.q:g}): about {hz_range(about_hz(lo), about_hz(hi))}"
    return f"{text}, {width.note}" if width.note else text


def reading(freq: float, q: float, gain_db: float, fs: float = FS) -> str:
    """The preview's one-line reading: "Lowers about 35–45 Hz, deepest −4 dB at 40 Hz."."""
    lo, hi = half_depth_range(freq, q, gain_db, fs)
    span = hz_range(about_hz(lo), about_hz(hi))
    if gain_db < 0:
        return f"Lowers about {span}, deepest {MINUS}{plain(-gain_db, 1)} dB at {hz(freq)}."
    return f"Raises about {span}, most +{plain(gain_db, 1)} dB at {hz(freq)}."


def new_band(current: PeqBand, freq: float, q: float, gain_db: float, fs: float = FS) -> PeqBand:
    """The band as Apply will store it (for the preview; the server compiles it again on Apply)."""
    design = FilterDesign(type="peaking", freq=freq, q=q, gain_db=gain_db)
    return current.model_copy(update={"design": design, "compiled": peaking(freq, q, gain_db, fs), "bypass": False})


def view_for(freq: float) -> tuple[float, float]:
    return SUB_VIEW if freq <= 200 else FULL_VIEW


# --- the preview graph (server-drawn SVG, aria-hidden; the reading line says it in words) ---------------------------

W, H = 520.0, 180.0
PAD_L, PAD_R, PAD_T, PAD_B = 40.0, 8.0, 8.0, 22.0
TICKS = (5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)


def _shape_db(out: OutputCh, freqs: np.ndarray, fs: float) -> np.ndarray:
    """The output's EQ and band filters (its "shape"; levels, routing and delay left out)."""
    tf = resp.peq_tf(out.peq, freqs, fs) * resp.crossover_tf(out.crossover, freqs, fs)
    with np.errstate(divide="ignore"):
        return np.maximum(20 * np.log10(np.abs(tf.h)), resp.DB_FLOOR)


def preview(out: OutputCh, band: int, after: PeqBand, freq: float, fs: float = FS, points: int = 160) -> dict:
    """Polylines of the output now and with the new band, plus the axis marks, in SVG user units."""
    f_lo, f_hi = view_for(freq)
    freqs = np.geomspace(f_lo, f_hi, points)
    changed = out.model_copy(update={"peq": [after if k == band else b for k, b in enumerate(out.peq)]})
    now, new = _shape_db(out, freqs, fs), _shape_db(changed, freqs, fs)
    top = float(min(24.0, max(6.0, math.ceil(max(now.max(), new.max()) / 6) * 6)))
    bottom = float(max(-30.0, min(-12.0, math.floor(min(now.min(), new.min()) / 6) * 6)))
    lx0, lx1 = math.log10(f_lo), math.log10(f_hi)

    def x(f: float) -> float:
        return PAD_L + (math.log10(f) - lx0) / (lx1 - lx0) * (W - PAD_L - PAD_R)

    def y(db: float) -> float:
        db = min(top, max(bottom, db))
        return PAD_T + (top - db) / (top - bottom) * (H - PAD_T - PAD_B)

    def line(values: np.ndarray) -> str:
        return " ".join(f"{x(float(f)):.1f},{y(float(v)):.1f}" for f, v in zip(freqs, values, strict=True))

    step = 6 if top - bottom <= 36 else 12
    # The SVG stretches to the box (180 px tall, any width); the tick labels are HTML placed in percent, so they don't.
    return {
        "w": W,
        "h": H,
        "left": PAD_L,
        "right": W - PAD_R,
        "top_y": PAD_T,
        "bottom_y": H - PAD_B,
        "left_pct": round(PAD_L / W * 100, 2),
        "now": line(now),
        "new": line(new),
        "mark": round(x(freq), 1),
        "x_ticks": [
            {"x": round(x(t), 1), "pct": round(x(t) / W * 100, 2), "label": f"{t // 1000}k" if t >= 1000 else str(t)}
            for t in TICKS
            if f_lo < t < f_hi
        ],
        "y_ticks": [
            {"y": round(y(v), 1), "pct": round(y(v) / H * 100, 2), "label": plain(v, 0), "zero": v == 0}
            for v in range(int(bottom), int(top) + 1, step)
        ],
        "range": hz_range(f_lo, f_hi),
    }


# --- the wizard -----------------------------------------------------------------------------------------------------


@dataclass
class Answers:
    n: int | None  # 0-based output
    freq: float | None
    kind: str  # cut | boost
    amount: float
    width: str

    @property
    def gain_db(self) -> float:
        return -self.amount if self.kind == CUT else self.amount

    @property
    def q(self) -> float:
        return WIDTH_BY_ID[self.width].q


def answers(params: Mapping[str, str], n_outputs: int | None) -> Answers:
    output = int_param(params, "output")
    n = output - 1 if output is not None else None
    if n_outputs is not None and (n is None or not 0 <= n < n_outputs):
        n = None
    freq = float_param(params, "freq")
    if freq is not None and not FREQ_MIN <= freq <= FREQ_MAX:
        freq = None
    kind = BOOST if params.get("kind") == BOOST else CUT
    amount = float_param(params, "amount", DEFAULT_DB) or DEFAULT_DB
    amount = min(MAX_DB, max(0.5, round(abs(amount) * 2) / 2))
    width = params.get("width") if params.get("width") in WIDTH_BY_ID else DEFAULT_WIDTH
    return Answers(n, freq, kind, amount, str(width))


def _params(a: Answers) -> dict[str, str]:
    out = {"kind": a.kind, "amount": f"{a.amount:g}", "width": a.width}
    if a.n is not None:
        out["output"] = str(a.n + 1)
    if a.freq is not None:
        out["freq"] = f"{a.freq:g}"
    return out


def _doc_output(ctx: TaskCtx, summary: PresetSummary, n: int) -> OutputCh | None:
    if ctx.session is None or summary.preset is None:
        return None
    try:
        return ctx.session.desired(summary.preset).outputs[n]
    except Exception:
        return None


def _fs(ctx: TaskCtx) -> float:
    return float(ctx.session.profile.sample_rate) if ctx.session is not None else float(FS)


def _eq_href(ctx: TaskCtx, n: int) -> str:
    return f"{ctx.root}/outputs/{n + 1}?tab=peq"


def _step_output(ctx: TaskCtx, a: Answers, summary: PresetSummary | None, error: str | None = None) -> Step:
    rows = []
    chosen = a.n
    if summary is not None:
        for o in summary.outputs:
            out = _doc_output(ctx, summary, o.index)
            bands = out.peq if out is not None else []
            free = free_bands(bands)
            clause = eq_clause(analyse_eq(bands))
            full = bool(bands) and not free
            rows.append(
                {
                    "n": o.index + 1,
                    "name": o.name,
                    "text": f"{o.name}: {clause or 'no EQ yet'}",
                    "free": f"{len(free)} of {len(bands)} EQ bands free" if not full else "",
                    "full": full,
                    "why": f"All {len(bands)} EQ bands on {o.name} are in use." if full else "",
                    "eq_href": _eq_href(ctx, o.index),
                    "note": "not in use" if o.unused else ("muted" if o.muted else ""),
                }
            )
        if chosen is None or rows[chosen]["full"]:
            usable = [r for r, o in zip(rows, summary.outputs, strict=True) if not r["full"] and not o.unused]
            usable = usable or [r for r in rows if not r["full"]]
            chosen = usable[0]["n"] - 1 if usable else None
    return Step(
        1,
        "Which output booms?",
        "tasks/eq_cut/step_output.html",
        _params(a),
        {
            "rows": rows,
            "chosen": chosen + 1 if chosen is not None else None,
            "error": error,
            "no_device": summary is None,
        },
    )


def _context(ctx: TaskCtx, summary: PresetSummary, a: Answers) -> tuple[OutputCh, FreeBand | None] | None:
    assert a.n is not None
    out = _doc_output(ctx, summary, a.n)
    if out is None:
        return None
    return out, free_band(out.peq)


def _band_note(out: OutputCh, name: str, freq: float | None) -> str | None:
    """A soft warning when the frequency is well outside the band the output plays."""
    if freq is None:
        return None
    band = analyse_band(out)
    lo = band.hp[0] if band.hp else None
    hi = band.lp[0] if band.lp else None
    if (hi is not None and freq > hi * 2) or (lo is not None and freq < lo / 2):
        if lo is not None and hi is not None:
            plays = hz_range(lo, hi)
        else:
            plays = f"below {hz(hi)}" if hi is not None else f"above {hz(lo or 0.0)}"
        return f"{name} only plays {plays}, so a change at {hz(freq)} will make little difference."
    return None


def _step_freq(ctx: TaskCtx, a: Answers, summary: PresetSummary, error: str | None = None) -> Step:
    assert a.n is not None
    o = summary.outputs[a.n]
    out = _doc_output(ctx, summary, a.n)
    clause = eq_clause(analyse_eq(out.peq)) if out is not None else None
    return Step(
        2,
        "Which frequency?",
        "tasks/eq_cut/step_freq.html",
        _params(a),
        {"o": o, "freq": f"{a.freq:g}" if a.freq is not None else "", "has": clause, "error": error},
        highlight=(f"o{a.n}:eq",),
    )


def _step_amount(ctx: TaskCtx, a: Answers, summary: PresetSummary, error: str | None = None) -> Step:
    assert a.n is not None and a.freq is not None
    o = summary.outputs[a.n]
    found = _context(ctx, summary, a)
    fs = _fs(ctx)
    graph = None
    if found is not None:
        out, free = found
        if free is not None:
            graph = preview(out, free.index, new_band(out.peq[free.index], a.freq, a.q, a.gain_db, fs), a.freq, fs)
    widths = [{"id": w.id, "text": width_text(w, a.freq, a.gain_db, fs)} for w in WIDTHS]
    return Step(
        3,
        "How much, and how wide?",
        "tasks/eq_cut/step_amount.html",
        _params(a),
        {
            "o": o,
            "a": a,
            "quick": QUICK_DB,
            "max_db": MAX_DB,
            "widths": widths,
            "graph": graph,
            "reading": reading(a.freq, a.q, a.gain_db, fs),
            "band_note": _band_note(found[0], o.name, a.freq) if found else None,
            "error": error,
        },
        highlight=(f"o{a.n}:eq",),
    )


def _big(ctx: TaskCtx, a: Answers) -> bool:
    step = float(getattr(ctx.settings, "gain_step_confirm_db", 6.0))
    return a.kind == BOOST and step > 0 and a.amount > step + EPS


def _width_words(a: Answers) -> str:
    w = WIDTH_BY_ID[a.width]
    return f"{w.label.lower()} width (Q {w.q:g})"


def plan_text(name: str, a: Answers, free: FreeBand) -> list[str]:
    """Step 4 in words: "Sub: cut 4 dB at 40 Hz, medium width (Q 4).", "Uses EQ band 2 (free), switched on."."""
    assert a.freq is not None
    lines = [f"{name}: {a.kind} {plain(a.amount, 1)} dB at {hz(a.freq)}, {_width_words(a)}."]
    if free.replaces is None:
        lines.append(f"Uses EQ band {free.index + 1} (free), switched on.")
    else:
        lines.append(
            f"Uses EQ band {free.index + 1}, switched on. It was switched off; its old setting ({free.replaces}) is "
            "replaced."
        )
    lines.append("Nothing else changes.")
    return lines


def _step_check(ctx: TaskCtx, a: Answers, summary: PresetSummary, error: str | None = None) -> Step:
    assert a.n is not None and a.freq is not None
    o = summary.outputs[a.n]
    found = _context(ctx, summary, a)
    if found is None or found[1] is None:
        return _step_output(ctx, a, summary, error=f"All EQ bands on {o.name} are in use.")
    out, free = found
    assert free is not None
    return Step(
        4,
        "Check",
        "tasks/eq_cut/step_check.html",
        _params(a),
        {
            "o": o,
            "lines": plan_text(o.name, a, free),
            "reading": reading(a.freq, a.q, a.gain_db, _fs(ctx)),
            "band_note": _band_note(out, o.name, a.freq),
            "confirm_line": (
                f"I confirm boosting {o.name} by {plain(a.amount, 1)} dB at {hz(a.freq)}." if _big(ctx, a) else None
            ),
            "error": error,
        },
        highlight=(f"o{a.n}:eq",),
    )


def done_text(name: str, bands: Sequence[PeqBand], new_index: int | None) -> str:
    """ "Sub now has 2 EQ cuts: −4 dB at 40 Hz (new) and −4 dB at 40 Hz."."""
    items = []
    cuts = boosts = 0
    for k, band in enumerate(bands):
        d = band.design
        if band.bypass or d.type != "peaking" or not d.gain_db or d.freq is None:
            continue
        cuts += d.gain_db < 0
        boosts += d.gain_db > 0
        sign = MINUS if d.gain_db < 0 else "+"
        item = f"{sign}{plain(abs(d.gain_db), 1)} dB at {hz(d.freq)}" + (" (new)" if k == new_index else "")
        items.append((k != new_index, item))  # the new one first
    if not items:
        return f"{name} has no EQ cut or boost now."
    n = len(items)
    noun = "cut" if boosts == 0 else "boost" if cuts == 0 else "band"
    listed = [item for _, item in sorted(items, key=lambda e: e[0])]
    head = f"{name} now has {n} EQ {noun}{'s' if n != 1 else ''}"
    if n > 4:
        return f"{head}: {', '.join(listed[:3])} and {n - 3} more."
    return f"{head}: {listed[0]}." if n == 1 else f"{head}: {', '.join(listed[:-1])} and {listed[-1]}."


def _step_done(
    ctx: TaskCtx,
    a: Answers,
    summary: PresetSummary,
    *,
    record: UndoRecord | None,
    message: str | None = None,
    applied: bool = False,
    toast: str | None = None,
    error: bool = False,
) -> Step:
    assert a.n is not None
    return Step(
        5,
        "Done",
        "tasks/eq_cut/step_done.html",
        {**_params(a), **({"undo": record.to_json()} if record else {})},
        {"o": summary.outputs[a.n], "message": message, "is_error": error, "eq_href": _eq_href(ctx, a.n)},
        applied=applied,
        toast=toast,
        undo=record if record else None,
    )


async def render(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    summary = ctx.summary()
    a = answers(params, len(summary.outputs) if summary is not None else None)
    step = int_param(params, "step", 1) or 1
    if summary is None or a.n is None or step <= 1:
        return _step_output(ctx, a, summary)
    found = _context(ctx, summary, a)
    if found is not None and found[1] is None and step < 5:
        o = summary.outputs[a.n]
        return _step_output(ctx, a, summary, error=f"All EQ bands on {o.name} are in use. Change one in the EQ table.")
    if step == 2 or a.freq is None:
        error = None
        if step > 2 and a.freq is None:
            error = f"Enter a frequency from {plain(FREQ_MIN)} Hz to {plain(FREQ_MAX / 1000)} kHz."
        return _step_freq(ctx, a, summary, error=error)
    if step == 3:
        return _step_amount(ctx, a, summary)
    if step == 4:
        return _step_check(ctx, a, summary)
    return _step_done(ctx, a, summary, record=UndoRecord.from_json(params.get("undo")))


async def act(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    """``op=apply`` (step 4's Apply) and ``op=undo`` (the Done step's Undo)."""
    session = ctx.session
    summary = ctx.summary()
    a = answers(params, len(summary.outputs) if summary is not None else None)
    if session is None or summary is None or a.n is None:
        return _step_output(ctx, a, summary, error="Choose an output first.")
    preset = summary.preset if summary.preset is not None else 0
    if params.get("op") == "undo":
        record = UndoRecord.from_json(params.get("undo"))
        if record is None:
            return _step_done(ctx, a, summary, record=None, message="Nothing to undo.")
        result = await undo(session, record)
        ctx.forget()
        return _step_done(
            ctx,
            a,
            ctx.summary() or summary,
            record=record if result.error else None,
            message=result.message,
            applied=not result.error,
            error=bool(result.error),
        )
    if params.get("op") != "apply":
        return await render(ctx, params)
    if a.freq is None:
        return _step_freq(ctx, a, summary, error="Enter a frequency first.")
    found = _context(ctx, summary, a)
    if found is None or found[1] is None:
        name = summary.outputs[a.n].name
        return _step_output(ctx, a, summary, error=f"All EQ bands on {name} are in use. Change one in the EQ table.")
    free = found[1]
    assert free is not None
    big = _big(ctx, a)
    if big and not confirmed(params):
        return _step_check(ctx, a, summary, error="Tick the box first to confirm the boost.")
    path = f"outputs.{a.n}.peq.{free.index}"
    name = summary.outputs[a.n].name
    before = values_of(session, preset, [path])
    try:
        await svc.set_peq_band(
            session,
            preset,
            "outputs",
            a.n,
            free.index,
            design=FilterDesign(type="peaking", freq=a.freq, q=a.q, gain_db=a.gain_db),
            bypass=False,
            confirm=big,
            commit=True,
            source="ui",
            summary=f"{name}: Fix a boom, {a.kind} {plain(a.amount, 1)} dB at {hz(a.freq)}",
        )
    except ConfirmationRequired as exc:
        return _step_check(ctx, a, summary, error=confirm_error(exc))
    except (DesiredValidationError, MasterValidationError) as exc:
        return _step_check(ctx, a, summary, error=write_error(session, exc))
    except Exception as exc:
        return _step_check(ctx, a, summary, error=f"It couldn't be sent: {exc or type(exc).__name__}.")
    after = values_of(session, preset, [path])
    record = UndoRecord(preset, {path: (before.get(path), after.get(path))})
    ctx.forget()
    fresh = ctx.summary() or summary
    out = _doc_output(ctx, fresh, a.n)
    text = done_text(name, out.peq if out is not None else [], free.index)
    return _step_done(ctx, a, fresh, record=record, message=text, applied=True, toast=text)


WIZARD = Wizard(
    id="eq-cut",
    label="Fix a boom",
    steps=STEPS,
    render=render,
    act=act,
    intro="Tames one booming note on an output with an EQ cut, without typing a minus sign.",
)

__all__ = [
    "WIDTHS",
    "WIZARD",
    "FreeBand",
    "about_hz",
    "done_text",
    "free_band",
    "free_bands",
    "half_depth_range",
    "plan_text",
    "preview",
    "reading",
    "width_text",
]
