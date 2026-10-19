"""Line up shakers with the sub (phase 9 design §14.5): distances in, a delay out.

Outputs · Distances · Check · Fine-tune · Done. The pure parts:

- `guess_pair` picks the defaults: the one felt in the seat from the Shakers (tactile) flag, then from names such as
  shaker / tactile / butt, then from the band (a band-pass such as 20–80 Hz); the one heard from across the room from
  the name "sub", then from a low-pass-only band.
- `line_up` is the maths, ``delay(felt) = delay(heard) + (d_heard − d_felt) / 343 m/s`` (the speed the Delay tab's
  helpers use), rounded to whole samples as the device stores it. A negative result delays the heard one instead; a
  result above the device's maximum (79.8 ms on a 2x4HD) is refused with the reason.

Apply sends the one delay (nothing is sent before). The optional fine-tune step applies at once: ±1 ms nudges and a
polarity try on the felt output, each labelled "applies now". Undo all covers the delay and the nudges: the run's
`UndoRecord` travels with the steps and each write is merged into it.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.model.config_doc import OutputCh
from app.model.device_profile import DeviceProfile
from app.sync.session import ConfirmationRequired, DesiredValidationError, MasterValidationError
from app.sync.summary import OutputSummary, PresetSummary, analyse_band, plain
from app.web.params import SPEED_OF_SOUND_M_S
from app.web.tasks import (
    Step,
    TaskCtx,
    UndoRecord,
    Wizard,
    apply_edits,
    confirm_error,
    float_param,
    int_param,
    undo,
    write_error,
)

STEPS = ("Outputs", "Distances", "Check", "Fine-tune", "Done")
FOOT_M = 0.3048
MAX_DISTANCE_M = 100.0
NUDGE_MS = 1.0
FELT_WORDS = re.compile(r"shak|tactile|butt|transducer|seat|couch|rumble", re.IGNORECASE)
HEARD_WORDS = re.compile(r"\bsub|woofer", re.IGNORECASE)
UNITS = ("m", "ft")


# --- pure parts -------------------------------------------------------------------------------------------------------


def _band_pass(out: OutputCh | None) -> bool:
    if out is None:
        return False
    band = analyse_band(out)
    return band.hp is not None and band.lp is not None and band.lp[0] <= 200


def _low_pass_only(out: OutputCh | None) -> bool:
    if out is None:
        return False
    band = analyse_band(out)
    return band.hp is None and band.lp is not None and band.lp[0] <= 200


def guess_pair(outputs: Sequence[OutputSummary], docs: Sequence[OutputCh | None]) -> tuple[int | None, int | None]:
    """(felt, heard): 0-based outputs to preselect, or None when there is nothing to go on."""
    used = [o for o in outputs if not o.unused] or list(outputs)

    def doc(o: OutputSummary) -> OutputCh | None:
        return docs[o.index] if o.index < len(docs) else None

    felt = next((o.index for o in used if o.tactile), None)
    if felt is None:
        felt = next((o.index for o in used if FELT_WORDS.search(o.name)), None)
    if felt is None:
        felt = next((o.index for o in used if _band_pass(doc(o)) and not HEARD_WORDS.search(o.name)), None)
    rest = [o for o in used if o.index != felt]
    heard = next((o.index for o in rest if HEARD_WORDS.search(o.name)), None)
    if heard is None:
        heard = next((o.index for o in rest if _low_pass_only(doc(o))), None)
    if heard is None and rest:
        heard = rest[0].index
    return felt, heard


@dataclass(frozen=True)
class LineUp:
    target: str  # "felt" or "heard": the output that gets the new delay
    delay_ms: float  # the new delay, rounded to whole samples
    samples: int
    exact_ms: float  # before rounding
    heard_travel_ms: float  # how long sound from the heard one takes to reach the seat
    felt_travel_ms: float
    error: str | None = None  # refused (beyond the device's maximum)


def to_metres(value: float, unit: str) -> float:
    return value * FOOT_M if unit == "ft" else value


def line_up(
    felt_delay_ms: float,
    heard_delay_ms: float,
    felt_m: float,
    heard_m: float,
    profile: DeviceProfile,
    speed: float = SPEED_OF_SOUND_M_S,
) -> LineUp:
    """Make both arrive together: the felt one waits for the heard one's sound to travel the extra distance."""
    heard_travel = heard_m / speed * 1000.0
    felt_travel = felt_m / speed * 1000.0
    exact = heard_delay_ms + heard_travel - felt_travel
    target = "felt"
    if exact < -1e-9:  # the felt one is farther: delay the heard one instead, by the same difference
        target = "heard"
        exact = felt_delay_ms + felt_travel - heard_travel
    samples = profile.delay_samples(exact)
    delay = profile.delay_ms_from_samples(samples)
    error = None
    if samples > profile.delay_max_samples:
        reach = profile.delay_max_ms / 1000.0 * speed
        most = plain(profile.delay_max_ms, 1)
        error = (
            f"That would be a {plain(exact, 2)} ms wait, but the device can wait at most {most} ms "
            f"(about {plain(reach, 0)} m of sound travel). Check the distances."
        )
    return LineUp(target, delay, samples, exact, heard_travel, felt_travel, error)


def ms(value: float) -> str:
    return f"{plain(value, 2)} ms"


# --- the wizard -----------------------------------------------------------------------------------------------------


@dataclass
class Answers:
    felt: int | None  # 0-based
    heard: int | None
    seat: bool  # the felt one is in or under the seat (0 m)
    felt_dist: float | None  # in ``unit``
    heard_dist: float | None
    unit: str
    unit_given: bool
    target: str | None = None  # "felt" / "heard": who got the delay (set once applied, so a nudge moves the same one)


def _distance(params: Mapping[str, str], name: str) -> float | None:
    value = float_param(params, name)
    return value if value is not None and value >= 0 else None


def answers(params: Mapping[str, str], summary: PresetSummary | None, docs: Sequence[OutputCh | None]) -> Answers:
    n = len(summary.outputs) if summary is not None else 0

    def pick(name: str) -> int | None:
        value = int_param(params, name)
        return value - 1 if value is not None and 1 <= value <= n else None

    felt, heard = pick("felt"), pick("heard")
    if summary is not None and (felt is None or heard is None):
        g_felt, g_heard = guess_pair(summary.outputs, docs)
        given = pick("output")  # from a card's Adjust menu: that output, as the felt one unless it's the heard guess
        if given is not None and felt is None and heard is None:
            if given == g_heard and g_felt is not None and g_felt != given:
                felt, heard = g_felt, given
            else:
                felt = given
                heard = g_heard if g_heard != given else (g_felt if g_felt not in (None, given) else None)
        felt = felt if felt is not None else (g_felt if g_felt != heard else None)
        heard = heard if heard is not None else (g_heard if g_heard != felt else None)
        if heard is None and felt is not None:
            heard = next((o.index for o in summary.outputs if o.index != felt), None)
    unit = params.get("unit") if params.get("unit") in UNITS else "m"
    return Answers(
        felt,
        heard,
        params.get("seat", "1") != "0",
        _distance(params, "felt_dist"),
        _distance(params, "heard_dist"),
        str(unit),
        params.get("unit") in UNITS,
        params.get("target") if params.get("target") in ("felt", "heard") else None,
    )


def _params(a: Answers) -> dict[str, str]:
    out = {"seat": "1" if a.seat else "0"}
    if a.unit_given:  # only once chosen on step 2, so step 2 can still apply the remembered unit (design §14.5)
        out["unit"] = a.unit
    if a.felt is not None:
        out["felt"] = str(a.felt + 1)
    if a.heard is not None:
        out["heard"] = str(a.heard + 1)
    if a.felt_dist is not None and not a.seat:
        out["felt_dist"] = f"{a.felt_dist:g}"
    if a.heard_dist is not None:
        out["heard_dist"] = f"{a.heard_dist:g}"
    if a.target is not None:
        out["target"] = a.target
    return out


def _docs(ctx: TaskCtx, summary: PresetSummary | None) -> list[OutputCh | None]:
    if ctx.session is None or summary is None or summary.preset is None:
        return []
    try:
        return list(ctx.session.desired(summary.preset).outputs)
    except Exception:
        return []


def _outputs_error(a: Answers) -> str | None:
    if a.felt is None or a.heard is None:
        return "Choose both outputs."
    if a.felt == a.heard:
        return "Choose two different outputs."
    return None


def _distances_error(a: Answers) -> str | None:
    limit = MAX_DISTANCE_M if a.unit == "m" else MAX_DISTANCE_M / FOOT_M
    if a.heard_dist is None or a.heard_dist <= 0:
        return "Enter how far the second output's speaker is from your seat."
    if a.heard_dist > limit or (a.felt_dist or 0) > limit:
        return f"Enter a distance up to {plain(limit, 0)} {a.unit}."
    if not a.seat and a.felt_dist is None:
        return "Enter how far the first output is from your seat, or choose “in or under my seat”."
    return None


def _step_outputs(ctx: TaskCtx, a: Answers, summary: PresetSummary | None, error: str | None = None) -> Step:
    rows = []
    if summary is not None:
        for o in summary.outputs:
            note = "Shakers (tactile)" if o.tactile else ("not in use" if o.unused else "")
            rows.append({"n": o.index + 1, "name": o.name, "note": note})
    return Step(
        1,
        "Which two?",
        "tasks/line_up/step_outputs.html",
        _params(a),
        {
            "rows": rows,
            "felt": a.felt + 1 if a.felt is not None else None,
            "heard": a.heard + 1 if a.heard is not None else None,
            "error": error,
            "no_device": summary is None,
        },
    )


def _names(summary: PresetSummary, a: Answers) -> tuple[str, str]:
    assert a.felt is not None and a.heard is not None
    return summary.outputs[a.felt].name, summary.outputs[a.heard].name


def _step_distances(ctx: TaskCtx, a: Answers, summary: PresetSummary, error: str | None = None) -> Step:
    felt, heard = _names(summary, a)
    return Step(
        2,
        "How far from your seat?",
        "tasks/line_up/step_distances.html",
        _params(a),
        {
            "felt_name": felt,
            "heard_name": heard,
            "a": a,
            "felt_dist": f"{a.felt_dist:g}" if a.felt_dist is not None else "",
            "heard_dist": f"{a.heard_dist:g}" if a.heard_dist is not None else "",
            "error": error,
        },
    )


def _plan(ctx: TaskCtx, summary: PresetSummary, a: Answers) -> tuple[LineUp, float, float] | None:
    """The result, with the felt and heard outputs' delays now."""
    docs = _docs(ctx, summary)
    if ctx.session is None or a.felt is None or a.heard is None or a.heard_dist is None or not docs:
        return None
    felt_now, heard_now = docs[a.felt].delay_ms, docs[a.heard].delay_ms
    felt_m = 0.0 if a.seat else to_metres(a.felt_dist or 0.0, a.unit)
    result = line_up(felt_now, heard_now, felt_m, to_metres(a.heard_dist, a.unit), ctx.session.profile)
    return result, felt_now, heard_now


def result_lines(felt: str, heard: str, result: LineUp, felt_now: float, heard_now: float) -> list[str]:
    """Step 3 in words (design §14.5): how long the sound takes, who waits how long, and who keeps what."""
    travel = f"Sound from {heard} takes {ms(result.heard_travel_ms)} to reach you."
    if result.felt_travel_ms > 0:
        travel = (
            f"Sound from {heard} takes {ms(result.heard_travel_ms)} to reach you, and from {felt} "
            f"{ms(result.felt_travel_ms)}."
        )
    if result.error:
        return [travel, result.error]
    waits, keeps, keeps_ms = (felt, heard, heard_now) if result.target == "felt" else (heard, felt, felt_now)
    lines = [travel]
    if result.target == "heard":
        lines.append(f"{felt} is farther from you than {heard}, so {heard} waits instead.")
    lines.append(
        f"{waits} will wait {ms(result.delay_ms)} ({result.samples} samples) so both arrive together. "
        f"{keeps} keeps its {ms(keeps_ms)}."
    )
    waits_now = felt_now if result.target == "felt" else heard_now
    if waits_now > 0 and abs(waits_now - result.delay_ms) >= 0.005:
        # P13: say what it is today, so "9 ms was already about right" is visible.
        lines.append(f"{waits} waits {ms(waits_now)} now.")
    return lines


def _step_check(ctx: TaskCtx, a: Answers, summary: PresetSummary, error: str | None = None) -> Step:
    felt, heard = _names(summary, a)
    found = _plan(ctx, summary, a)
    if found is None:
        return _step_distances(ctx, a, summary, error="Enter the distances first.")
    result, felt_now, heard_now = found
    target = a.felt if result.target == "felt" else a.heard
    current = felt_now if result.target == "felt" else heard_now
    unchanged = result.error is None and abs(current - result.delay_ms) < 0.005
    return Step(
        3,
        "Check",
        "tasks/line_up/step_check.html",
        _params(a),
        {
            "lines": result_lines(felt, heard, result, felt_now, heard_now),
            "refused": result.error is not None,
            "unchanged": unchanged,
            "who": felt if result.target == "felt" else heard,
            "error": error,
        },
        highlight=(f"o{target}:delay",),
    )


def _target(ctx: TaskCtx, summary: PresetSummary, a: Answers) -> int | None:
    """The output the run delayed (the felt one unless it was farther away)."""
    if a.target is not None:
        return a.felt if a.target == "felt" else a.heard
    found = _plan(ctx, summary, a)
    if found is None or found[0].error:
        return a.felt
    return a.felt if found[0].target == "felt" else a.heard


def _step_tune(
    ctx: TaskCtx,
    a: Answers,
    summary: PresetSummary,
    *,
    record: UndoRecord | None,
    message: str | None = None,
    error: str | None = None,
    applied: bool = False,
    toast: str | None = None,
) -> Step:
    assert a.felt is not None
    docs = _docs(ctx, summary)
    target = _target(ctx, summary, a)
    assert target is not None
    max_ms = ctx.session.profile.delay_max_ms if ctx.session is not None else 0.0
    now = docs[target].delay_ms if docs else 0.0
    inverted = bool(docs[a.felt].invert) if docs else False
    return Step(
        4,
        "Fine-tune by feel",
        "tasks/line_up/step_tune.html",
        {**_params(a), **({"undo": record.to_json()} if record else {})},
        {
            "who": summary.outputs[target].name,
            "felt_name": summary.outputs[a.felt].name,
            "now": ms(now),
            "can_less": now >= NUDGE_MS / 2,
            "can_more": now + NUDGE_MS <= max_ms + 1e-9,
            "inverted": inverted,
            "message": message,
            "error": error,
        },
        highlight=(f"o{target}:delay", f"o{a.felt}:polarity"),
        applied=applied,
        toast=toast,
        undo=record,
    )


def _step_done(
    ctx: TaskCtx,
    a: Answers,
    summary: PresetSummary,
    *,
    record: UndoRecord | None,
    message: str | None = None,
    error: bool = False,
    applied: bool = False,
) -> Step:
    assert a.felt is not None
    docs = _docs(ctx, summary)
    target = _target(ctx, summary, a)
    assert target is not None
    who = summary.outputs[target].name
    felt = summary.outputs[a.felt].name
    wait = ms(docs[target].delay_ms) if docs else "?"
    polarity = "180°" if docs and docs[a.felt].invert else "0°"
    line = f"{who} waits {wait}" + (f", polarity {polarity}." if target == a.felt else f"; {felt} polarity {polarity}.")
    return Step(
        5,
        "Done",
        "tasks/line_up/step_done.html",
        {**_params(a), **({"undo": record.to_json()} if record else {})},
        {"line": line, "message": message, "is_error": error},
        applied=applied,
        undo=record,
    )


async def render(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    summary = ctx.summary()
    a = answers(params, summary, _docs(ctx, summary))
    step = int_param(params, "step", 1) or 1
    if summary is None or step <= 1:
        return _step_outputs(ctx, a, summary)
    if error := _outputs_error(a):
        return _step_outputs(ctx, a, summary, error=error)
    if step == 2:
        return _step_distances(ctx, a, summary)
    if error := _distances_error(a):
        return _step_distances(ctx, a, summary, error=error)
    if step == 3:
        return _step_check(ctx, a, summary)
    record = UndoRecord.from_json(params.get("undo"))
    if step == 4:
        return _step_tune(ctx, a, summary, record=record)
    return _step_done(ctx, a, summary, record=record)


def _merge(old: UndoRecord | None, new: UndoRecord) -> UndoRecord:
    return old.merged(new) if old is not None and old.preset == new.preset else new


async def act(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    """``op=apply`` (step 3), ``op=nudge`` / ``op=polarity`` (step 4, applied at once) and ``op=undo``."""
    session = ctx.session
    summary = ctx.summary()
    a = answers(params, summary, _docs(ctx, summary))
    if session is None or summary is None or _outputs_error(a):
        return _step_outputs(ctx, a, summary, error=_outputs_error(a) or "Choose the outputs first.")
    if error := _distances_error(a):
        return _step_distances(ctx, a, summary, error=error)
    assert a.felt is not None
    preset = summary.preset if summary.preset is not None else 0
    op = params.get("op")
    previous = UndoRecord.from_json(params.get("undo"))
    if op == "undo":
        if previous is None:
            return _step_done(ctx, a, summary, record=None, message="Nothing to undo.")
        result = await undo(session, previous)
        ctx.forget()
        return _step_done(
            ctx,
            a,
            ctx.summary() or summary,
            record=previous if result.error else None,
            message=result.message,
            error=bool(result.error),
            applied=not result.error,
        )
    found = _plan(ctx, summary, a)
    if found is None:
        return _step_distances(ctx, a, summary, error="Enter the distances first.")
    result, felt_now, heard_now = found
    if op == "apply" or a.target is None:
        a.target = result.target
    target = a.felt if a.target == "felt" else a.heard
    assert target is not None
    docs = _docs(ctx, summary)
    if op == "apply":
        if result.error:
            return _step_check(ctx, a, summary, error=result.error)
        values: dict[str, float | bool] = {f"outputs.{target}.delay_ms": result.delay_ms}
        what = f"{summary.outputs[target].name} waits {ms(result.delay_ms)}."
    elif op == "nudge":
        by = NUDGE_MS if params.get("by") == "1" else -NUDGE_MS
        now = docs[target].delay_ms
        new = min(session.profile.delay_max_ms, max(0.0, now + by))
        new = session.profile.delay_ms_from_samples(session.profile.delay_samples(new))
        values = {f"outputs.{target}.delay_ms": new}
        what = f"Applied: {summary.outputs[target].name} waits {ms(new)}."
    elif op == "polarity":
        invert = params.get("invert") == "1"
        values = {f"outputs.{a.felt}.invert": invert}
        name = summary.outputs[a.felt].name
        what = f"Applied: {name} polarity {'180°' if invert else '0°'}." + (" Which felt tighter?" if invert else "")
    else:
        return await render(ctx, params)
    try:
        record = await apply_edits(session, preset, values, confirm=False)
    except ConfirmationRequired as exc:
        error = confirm_error(exc)
    except (DesiredValidationError, MasterValidationError) as exc:
        error = write_error(session, exc)
    except Exception as exc:
        error = f"It couldn't be sent: {exc or type(exc).__name__}."
    else:
        ctx.forget()
        fresh = ctx.summary() or summary
        merged = _merge(previous, record)
        return _step_tune(ctx, a, fresh, record=merged, message=what, applied=True, toast=what)
    if op == "apply":
        return _step_check(ctx, a, summary, error=error)
    return _step_tune(ctx, a, summary, record=previous, error=error)


WIZARD = Wizard(
    id="line-up",
    label="Line up shakers",
    steps=STEPS,
    render=render,
    act=act,
    intro="Works out the delay from distances, so what you feel and what you hear arrive together.",
)

__all__ = ["WIZARD", "LineUp", "guess_pair", "line_up", "result_lines", "to_metres"]
