"""Make one output louder (or quieter) (phase 9 design §14.4).

`options` is pure: for an output, a direction and an amount it works out the ways to get there, each with its result
in words and whether it is possible right now:

- (a) the output's own level, while it stays within the output's level limit;
- (b) the route(s) into it (preselected once the output level is at its limit, friction P04);
- (c) louder only: the other outputs down and Volume up by the same amount (same overall loudness, this output
  relatively louder), when Volume has room. It is marked recommended when the new total would reach the high-total
  caution (Settings, default +12 dB).

The wizard previews the chosen one (with the headroom sentence and, for a big raise, a checkbox), sends it on Apply
through the session, and offers Undo and "A bit more (+1)".
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.sync.session import ConfirmationRequired, DesiredValidationError, MasterValidationError
from app.sync.summary import ARROW, MINUS, OutputSummary, PresetSummary, join_words, plain, signed
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

STEPS = ("Output", "Amount", "How", "Check", "Done")
QUICK_DB = (1.0, 2.0, 3.0, 6.0)
DEFAULT_DB = 3.0
MAX_DB = 24.0
UP, DOWN = "up", "down"
METHODS = ("output", "routes", "others")
EPS = 1e-9


def _db(value: float) -> str:
    """``+8``, ``−3``, ``0`` (no unit)."""
    return signed(value, 1)


@dataclass(frozen=True)
class Option:
    id: str  # output | routes | others
    label: str
    available: bool
    reason: str = ""  # why it isn't possible now
    values: dict[str, float] = field(default_factory=dict)  # preset paths -> new level
    volume: float | None = None  # the new Volume ("others")
    new_total: float = 0.0  # the output's highest total afterwards
    raise_db: float = 0.0  # the biggest single raise it makes (for the big-step confirmation)
    raise_what: str = ""  # what that raise is, for the checkbox ("Shakers", "the Volume")
    recommended: bool = False
    blocks: tuple[str, ...] = ()  # flow diagram blocks it changes


def options(
    summary: PresetSummary,
    n: int,
    direction: str,
    amount: float,
    *,
    gain_min: float,
    volume: float | None,
    volume_max: float,
    high_total: float,
) -> list[Option]:
    """The ways to make output ``n`` louder (``direction`` "up") or quieter ("down") by ``amount`` dB (§14.4 step 3)."""
    o = summary.outputs[n]
    t = o.total
    sign = 1.0 if direction == UP else -1.0
    d = abs(amount)
    verb = "Raise" if sign > 0 else "Lower"
    new_total = t.max + sign * d
    level_block = f"o{n}:level"
    found: list[Option] = []

    # (a) the output level
    new_out = round(t.output + sign * d, 2)
    if sign > 0 and new_out > t.ceiling + EPS:
        whose = possessive(o.name)
        why = (
            f"{whose} output level is at its {_db(t.ceiling)} dB limit."
            if t.at_limit
            else f"{whose} output level would pass its {_db(t.ceiling)} dB limit (it is at {_db(t.output)})."
        )
        found.append(Option("output", f"{verb} {possessive(o.name)} output level", False, why))
    elif sign < 0 and new_out < gain_min - EPS:
        why = f"{possessive(o.name)} output level can't go below {_db(gain_min)} dB (it is at {_db(t.output)})."
        found.append(Option("output", f"{verb} {possessive(o.name)} output level", False, why))
    else:
        label = f"{verb} {possessive(o.name)} output level from {_db(t.output)} to {_db(new_out)} dB"
        found.append(
            Option(
                "output",
                label,
                True,
                values={f"outputs.{n}.gain_db": new_out},
                new_total=new_total,
                raise_db=d if sign > 0 else 0.0,
                raise_what=o.name,
                blocks=(level_block,),
            )
        )

    # (b) the route(s) into it
    routes = t.routes
    route_words = [f"{r.name} {ARROW} {o.name}" for r in routes]
    if not routes:
        found.append(Option("routes", f"{verb} the route into {o.name}", False, f"No input is routed to {o.name}."))
    else:
        new_routes = {r.input: round(r.route_db + sign * d, 2) for r in routes}
        stuck = [r for r in routes if not gain_min - EPS <= new_routes[r.input] <= t.ceiling + EPS]
        stays = (
            f"output level stays at its {_db(t.output)} limit"
            if t.at_limit
            else f"output level stays at {_db(t.output)}"
        )
        if len(routes) == 1:
            r = routes[0]
            label = (
                f"{verb} the route {route_words[0]} from {_db(r.route_db)} to {_db(new_routes[r.input])} dB ({stays})"
            )
        else:
            label = f"{verb} the routes {join_words(route_words)} by {plain(d, 1)} dB ({stays})"
        if stuck:
            limit = _db(t.ceiling) if sign > 0 else _db(gain_min)
            r = stuck[0]
            why = f"The route {r.name} {ARROW} {o.name} would pass its {limit} dB limit (it is at {_db(r.route_db)})."
            found.append(Option("routes", label, False, why))
        else:
            found.append(
                Option(
                    "routes",
                    label,
                    True,
                    values={f"inputs.{r.input}.routing.{n}.gain_db": new_routes[r.input] for r in routes},
                    new_total=new_total,
                    raise_db=d if sign > 0 else 0.0,
                    raise_what=("the route " if len(routes) == 1 else "the routes ") + join_words(route_words),
                    blocks=(level_block,),
                )
            )

    # (c) the others down, Volume up (louder only)
    if sign > 0:
        others = [x for x in summary.outputs if x.index != n and not x.unused]
        names = join_words([x.name for x in others])
        label = (
            f"Lower {names} by {plain(d, 1)} dB and raise Volume by {plain(d, 1)} dB (same overall loudness, "
            f"{o.name} relatively louder, more headroom)"
            if others
            else "Lower the other outputs and raise Volume"
        )
        why = ""
        if not others:
            why = "There is no other output in use to lower."
        elif volume is None:
            why = "The Volume isn't known yet."
        elif volume + d > volume_max + EPS:
            why = f"Volume has no room: it is at {plain(volume, 1)} dB and can go up to {plain(volume_max, 1)} dB."
        else:
            low = next((x for x in others if x.total.output - d < gain_min - EPS), None)
            if low is not None:
                why = (
                    f"{possessive(low.name)} output level can't go below {_db(gain_min)} dB "
                    f"(it is at {_db(low.total.output)})."
                )
        if why:
            found.append(Option("others", label, False, why))
        else:
            found.append(
                Option(
                    "others",
                    label,
                    True,
                    values={f"outputs.{x.index}.gain_db": round(x.total.output - d, 2) for x in others},
                    volume=round((volume or 0.0) + d, 2),
                    new_total=t.max,
                    raise_db=d,
                    raise_what="the Volume",
                    recommended=new_total >= high_total,
                    blocks=(level_block, *(f"o{x.index}:level" for x in others)),
                )
            )
    return found


def preselect(o: OutputSummary, found: Sequence[Option], direction: str) -> str | None:
    """The option chosen by default: the route once the output is at its limit (P04), else the output level."""
    available = [x.id for x in found if x.available]
    if not available:
        return None
    if direction == UP and o.total.at_limit and "routes" in available:
        return "routes"
    for choice in ("output", "routes", "others"):
        if choice in available:
            return choice
    return available[0]


def result_lines(summary: PresetSummary, n: int, option: Option, volume: float | None) -> list[str]:
    """Step 4 in words: "Shakers: +16 → +19 dB total." and the headroom sentence afterwards."""
    o = summary.outputs[n]
    t = o.total
    lines = []
    if option.id == "others":
        lines.append(f"{o.name}: {_db(t.max)} dB total (unchanged).")
        others = [summary.outputs[int(p.split(".")[1])] for p in option.values]
        lines.append(f"{join_words([x.name for x in others])}: {MINUS}{plain(option.raise_db, 1)} dB each.")
        if volume is not None and option.volume is not None:
            lines.append(f"Volume: {plain(volume, 1)} → {plain(option.volume, 1)} dB.")
        return lines
    lines.append(f"{o.name}: {_db(t.max)} → {_db(option.new_total)} dB total.")
    main = next((r.name for r in t.routes if r.input == t.input), None)
    if main is not None and option.new_total > 0:
        lines.append(f"Peaks above about {MINUS}{plain(option.new_total, 1)} dBFS on {main} will overload it.")
    return lines


# --- the wizard -----------------------------------------------------------------------------------------------------


@dataclass
class Answers:
    n: int | None  # 0-based
    direction: str
    amount: float
    method: str | None


def answers(params: Mapping[str, str], summary: PresetSummary | None) -> Answers:
    output = int_param(params, "output")
    n = output - 1 if output is not None else None
    if summary is not None and (n is None or not 0 <= n < len(summary.outputs)):
        n = None
    direction = DOWN if params.get("dir") == DOWN else UP
    amount = float_param(params, "custom") if params.get("custom") else None
    if amount is None or amount <= 0:
        amount = float_param(params, "amount", DEFAULT_DB)
    amount = min(MAX_DB, max(0.5, round(abs(amount or DEFAULT_DB) * 2) / 2))
    method = params.get("method") if params.get("method") in METHODS else None
    return Answers(n, direction, amount, method)


def _params(a: Answers) -> dict[str, str]:
    out = {"dir": a.direction, "amount": f"{a.amount:g}"}
    if a.n is not None:
        out["output"] = str(a.n + 1)
    if a.method:
        out["method"] = a.method
    return out


def _options(ctx: TaskCtx, summary: PresetSummary, a: Answers) -> list[Option]:
    session = ctx.session
    master = session.master if session is not None else None
    settings = ctx.settings
    assert a.n is not None and session is not None
    return options(
        summary,
        a.n,
        a.direction,
        a.amount,
        gain_min=session.profile.gain_min_db,
        volume=master.volume if master is not None else None,
        volume_max=session.profile.master_volume_max_db,
        high_total=float(getattr(settings, "guidance_high_total_db", 12.0)),
    )


def _step_output(ctx: TaskCtx, a: Answers, summary: PresetSummary | None, error: str | None = None) -> Step:
    rows = []
    if summary is not None:
        for o in summary.outputs:
            rows.append(
                {
                    "n": o.index + 1,
                    "name": o.name,
                    "text": f"{o.name} now: {o.total.text} ({o.total.breakdown})",
                    "note": "not in use" if o.unused else ("muted" if o.muted else ""),
                }
            )
    chosen = (a.n if a.n is not None else next((o.index for o in summary.outputs if not o.unused), 0)) if summary else 0
    return Step(
        1,
        "Which output?",
        "tasks/louder/step_output.html",
        _params(a),
        {"rows": rows, "chosen": chosen + 1, "error": error, "no_device": summary is None},
    )


def _step_amount(ctx: TaskCtx, a: Answers, summary: PresetSummary) -> Step:
    assert a.n is not None
    o = summary.outputs[a.n]
    custom = "" if a.amount in QUICK_DB else f"{a.amount:g}"
    return Step(
        2,
        "How much?",
        "tasks/louder/step_amount.html",
        _params(a),
        {"o": o, "quick": QUICK_DB, "amount": a.amount, "custom": custom, "direction": a.direction},
        highlight=(f"o{a.n}:level",),
    )


def _step_how(ctx: TaskCtx, a: Answers, summary: PresetSummary, error: str | None = None) -> Step:
    assert a.n is not None
    o = summary.outputs[a.n]
    found = _options(ctx, summary, a)
    chosen = a.method if any(x.id == a.method and x.available for x in found) else preselect(o, found, a.direction)
    chosen_option = next((x for x in found if x.id == chosen), None)
    return Step(
        3,
        "How?",
        "tasks/louder/step_how.html",
        _params(a),
        {"o": o, "options": found, "chosen": chosen, "error": error, "direction": a.direction, "amount": a.amount},
        highlight=chosen_option.blocks if chosen_option else (f"o{a.n}:level",),
    )


def _big(ctx: TaskCtx, option: Option) -> bool:
    step = float(getattr(ctx.settings, "gain_step_confirm_db", 6.0))
    return step > 0 and option.raise_db > step + EPS


def _step_check(ctx: TaskCtx, a: Answers, summary: PresetSummary, error: str | None = None) -> Step:
    assert a.n is not None
    found = _options(ctx, summary, a)
    option = next((x for x in found if x.id == a.method), None)
    if option is None or not option.available:
        why = option.reason if option is not None else None
        return _step_how(ctx, a, summary, error=why or "Choose how first.")
    master = ctx.session.master if ctx.session is not None else None
    volume = master.volume if master is not None else None
    big = _big(ctx, option)
    return Step(
        4,
        "Check",
        "tasks/louder/step_check.html",
        _params(a),
        {
            "o": summary.outputs[a.n],
            "option": option,
            "lines": result_lines(summary, a.n, option, volume),
            "confirm_line": f"I confirm raising {option.raise_what} by {plain(option.raise_db, 1)} dB."
            if big
            else None,
            "error": error,
        },
        highlight=option.blocks,
    )


def _step_done(
    ctx: TaskCtx,
    a: Answers,
    summary: PresetSummary,
    *,
    record: UndoRecord | None,
    message: str | None = None,
    applied: bool = False,
    toast: str | None = None,
) -> Step:
    assert a.n is not None
    o = summary.outputs[a.n]
    more = {**_params(a), "amount": "1", "dir": UP, "step": "4"}
    return Step(
        5,
        "Done",
        "tasks/louder/step_done.html",
        {**_params(a), **({"undo": record.to_json()} if record else {})},
        {"o": o, "message": message, "more": more},
        applied=applied,
        toast=toast,
        undo=record if record else None,
    )


async def render(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    summary = ctx.summary()
    a = answers(params, summary)
    step = int_param(params, "step", 1) or 1
    if summary is None or a.n is None or step <= 1:
        return _step_output(ctx, a, summary)
    if step == 2:
        return _step_amount(ctx, a, summary)
    if step == 3 or a.method is None:
        return _step_how(ctx, a, summary)
    if step == 4:
        return _step_check(ctx, a, summary)
    return _step_done(ctx, a, summary, record=UndoRecord.from_json(params.get("undo")))


def _done_text(summary: PresetSummary, n: int, option: Option, direction: str) -> str:
    o = summary.outputs[n]
    word = "louder" if direction == UP else "quieter"
    return f"{o.name} is {word}: {o.total.text} total ({o.total.breakdown})."


async def act(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    """``op=apply`` (step 4's Apply) and ``op=undo`` (step 5's Undo)."""
    session = ctx.session
    summary = ctx.summary()
    a = answers(params, summary)
    if session is None or summary is None or a.n is None:
        return _step_output(ctx, a, summary, error="Choose an output first.")
    preset = summary.preset if summary.preset is not None else 0
    if params.get("op") == "undo":
        record = UndoRecord.from_json(params.get("undo"))
        if record is None:
            return _step_done(ctx, a, summary, record=None, message="Nothing to undo.")
        result = await undo(session, record)
        ctx.forget()
        summary = ctx.summary() or summary
        return _step_done(
            ctx,
            a,
            summary,
            record=None if not result.error else record,
            message=result.message,
            applied=not result.error,
        )
    if params.get("op") != "apply":
        return await render(ctx, params)
    found = _options(ctx, summary, a)
    option = next((x for x in found if x.id == a.method), None)
    if option is None or not option.available:
        return _step_how(ctx, a, summary, error=(option.reason if option else "") or "Choose how first.")
    big = _big(ctx, option)
    if big and not confirmed(params):
        return _step_check(ctx, a, summary, error="Tick the box first to confirm the raise.")
    record: UndoRecord | None = None
    try:
        record = await apply_edits(session, preset, option.values, confirm=big)
        if option.volume is not None:
            try:
                record = record.merged(await apply_master(session, preset, confirm=big, volume=option.volume))
            except Exception:
                await undo(session, record)  # don't leave the others lowered without the Volume raise
                raise
    except ConfirmationRequired as exc:
        return _step_check(ctx, a, summary, error=confirm_error(exc))
    except (DesiredValidationError, MasterValidationError) as exc:
        return _step_check(ctx, a, summary, error=write_error(session, exc))
    except Exception as exc:
        return _step_check(ctx, a, summary, error=f"It couldn't be sent: {exc or type(exc).__name__}.")
    ctx.forget()
    after = ctx.summary() or summary
    text = _done_text(after, a.n, option, a.direction)
    return _step_done(ctx, a, after, record=record, message=text, applied=True, toast=text)


WIZARD = Wizard(
    id="louder",
    label="Make one louder",
    steps=STEPS,
    render=render,
    act=act,
    intro="Makes one output louder or quieter than the others, the way that still works at its level limit.",
)

__all__ = ["WIZARD", "Option", "options", "preselect", "result_lines"]
