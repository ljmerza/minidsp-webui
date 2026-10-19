"""Something wrong? (phase 9 design §14.3): find out why an output is silent (or why it sounds distorted), in order,
with a fix for each problem the app can fix.

`run_checks` is pure: it reads `Facts` (the playing preset's summary and document, the master state, the live meters
and the sync statuses) and returns the checks in the design's order, each ✓ (ok), ✗ (fail, with its fixes), "can't
tell" (unknown) or not checked yet (pending, when it depends on an earlier ✗). The wizard (`WIZARD`) runs them, applies
a fix the user chose through the session (`app/web/tasks.py`) and runs them again.

Live checks (input signal, the output's meter) never fail on a borderline reading: below the "no signal" floor
(Settings, default −90 dBFS) is no signal, more than 30 dB above it is signal, and anything between is "can't tell"
(the mock's flat −60 dBFS reads as that). The thresholds are unverified on real hardware (design §19).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.model.config_doc import ConfigDoc
from app.model.device_profile import DeviceProfile
from app.sync.session import ConfirmationRequired, DesiredValidationError, MasterValidationError, SessionState
from app.sync.summary import (
    ARROW,
    MINUS,
    Levels,
    OutputSummary,
    PresetSummary,
    Thresholds,
    join_words,
    plain,
    signed,
)
from app.web.tasks import (
    Step,
    TaskCtx,
    UndoRecord,
    Wizard,
    apply_edits,
    apply_master,
    confirm_error,
    confirmed,
    int_param,
    possessive,
    undo,
    write_error,
)

OK, FAIL, UNKNOWN, PENDING = "ok", "fail", "unknown", "pending"
SIGNAL_MARGIN_DB = 30.0  # more than this above the no-signal floor counts as signal (as the summary's live rule)
LOW_TOTAL_DB = -40.0  # a total level below this is "very low" (design §14.3, check 7)
SET_VOLUME_DB = -30.0
WHATS = ("all", "one", "distorted")
STEPS = ("What's wrong?", "Checks", "Done")


# --- results ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fix:
    """A fix of a failed check. ``kind``: "edit" (``values``, preset paths), "master" (``master``: mute / volume),
    "retry" (send the failed settings again), "source" (choose among ``choices``), "link" (``href``, app-relative) or
    "task" (another guided task, ``task`` with ``params``)."""

    label: str
    kind: str
    preview: str = ""  # one line on what it will do
    done: str = ""  # "In 1 → Sub at 0 dB" (after "Fixed: ")
    values: dict[str, Any] = field(default_factory=dict)
    master: dict[str, Any] = field(default_factory=dict)
    href: str | None = None
    task: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    choices: tuple[str, ...] = ()
    current: str | None = None  # the source chosen now ("source" fixes)
    confirm: str | None = None  # a line the user ticks first (a big raise, un-muting a never-sent output)
    big_raise: bool = False  # ``confirm`` is the session's big-step guard (sent as ``confirm``)


@dataclass(frozen=True)
class Check:
    id: str  # stable within one run: "dev-mute", "o1-route"
    result: str  # ok | fail | unknown | pending
    text: str
    detail: str = ""
    fixes: tuple[Fix, ...] = ()
    cause: str = ""  # for the Done line: "no input was routed"
    blocks: tuple[str, ...] = ()  # flow diagram blocks this check is about ("o1:level")

    @property
    def glyph(self) -> str:
        return {OK: "✓", FAIL: "✗", UNKNOWN: "?", PENDING: "·"}[self.result]

    @property
    def word(self) -> str:
        return {OK: "OK", FAIL: "Problem", UNKNOWN: "Can't tell", PENDING: "Not checked yet"}[self.result]


@dataclass(frozen=True)
class Group:
    title: str  # "" for the device-wide checks
    checks: tuple[Check, ...]
    output: int | None = None


@dataclass(frozen=True)
class Facts:
    """Everything the checks read. ``clipped``: the latched CLIP lights the browser reports ("i0", "o1"); None when
    unknown (no JavaScript)."""

    summary: PresetSummary
    doc: ConfigDoc
    profile: DeviceProfile
    connected: bool = True
    conn_label: str = "Connected"
    master_mute: bool | None = False
    volume: float | None = None
    source: str | None = None
    playing: int | None = 0
    levels: Levels | None = None
    th: Thresholds = field(default_factory=Thresholds)
    statuses: Mapping[str, str] = field(default_factory=dict)
    step_db: float = 6.0  # the big-step guard (Settings GAIN_STEP_CONFIRM_DB)
    clipped: frozenset[str] | None = None


def signal(level: float | None, th: Thresholds) -> str | None:
    """ "yes", "no" or "unsure" for one meter reading; None when there is no reading."""
    if level is None:
        return None
    if level < th.no_signal_dbfs:
        return "no"
    if level > th.no_signal_dbfs + SIGNAL_MARGIN_DB:
        return "yes"
    return "unsure"


def _reading(values: Sequence[float | None], index: int) -> float | None:
    if index >= len(values):
        return None
    value = values[index]
    return float(value) if isinstance(value, int | float) else None


def dbfs(level: float) -> str:
    return f"{plain(level, 1)} dBFS"


def db(value: float) -> str:
    return f"{signed(value, 1)} dB"


# --- device-wide checks (1-4) ---------------------------------------------------------------------------------------


def device_checks(f: Facts) -> list[Check]:
    checks = []
    if f.connected:
        checks.append(Check("dev-conn", OK, "Device connected"))
    else:
        checks.append(
            Check(
                "dev-conn",
                FAIL,
                f"The device isn't connected ({f.conn_label})",
                "The app can't reach the miniDSP, so nothing else can be checked or fixed from here. Check its USB "
                "cable and power; the header shows when it is back.",
            )
        )
    if f.master_mute is None:
        checks.append(Check("dev-mute", UNKNOWN, "Mute all: can't tell", "The device hasn't reported it yet."))
    elif f.master_mute:
        fix = Fix("Unmute all", "master", "Turn Mute all off.", "Mute all is off", master={"mute": False})
        checks.append(Check("dev-mute", FAIL, "Mute all is on", fixes=(fix,), cause="Mute all was on"))
    else:
        checks.append(Check("dev-mute", OK, "Mute all is off"))
    if f.volume is None:
        checks.append(Check("dev-volume", UNKNOWN, "Volume: can't tell", "The device hasn't reported it yet."))
    elif f.volume < f.th.volume_quiet_db:
        raise_db = SET_VOLUME_DB - f.volume
        confirm = None
        if f.step_db > 0 and raise_db > f.step_db:
            confirm = f"I confirm raising the Volume by {plain(raise_db, 1)} dB (to {MINUS}30 dB)."
        fix = Fix(
            f"Set {MINUS}30 dB",
            "master",
            f"Raise the Volume from {plain(f.volume, 1)} to {MINUS}30 dB.",
            f"Volume {MINUS}30 dB",
            master={"volume": SET_VOLUME_DB},
            confirm=confirm,
            big_raise=confirm is not None,
        )
        text = f"Volume is at {plain(f.volume, 1)} dB, which is very quiet"
        checks.append(Check("dev-volume", FAIL, text, fixes=(fix,), cause="the Volume was very low"))
    else:
        checks.append(Check("dev-volume", OK, f"Volume is at {plain(f.volume, 1)} dB"))
    checks.append(_input_signal(f))
    return checks


def _input_signal(f: Facts) -> Check:
    names = [i.name for i in f.summary.inputs]
    readings = [_reading(f.levels.inputs, i) if f.levels else None for i in range(len(names))]
    states = [signal(r, f.th) for r in readings]
    detail = "The input meters show what reaches the miniDSP. The thresholds are rules of thumb."
    if f.levels is None or all(s is None for s in states):
        return Check("dev-signal", UNKNOWN, "Input signal: can't tell (the meters aren't reporting)", detail)
    if "yes" in states:
        i = states.index("yes")
        return Check("dev-signal", OK, f"{names[i]} shows signal ({dbfs(readings[i] or 0.0)})")
    if "unsure" in states:
        i = states.index("unsure")
        return Check(
            "dev-signal", UNKNOWN, f"Input signal: can't tell ({names[i]} reads {dbfs(readings[i] or 0.0)})", detail
        )
    fix = Fix(
        "Use this source",
        "source",
        "Is the player playing? Check the source the miniDSP listens to.",
        "",
        choices=tuple(f.profile.sources),
        current=f.source,
    )
    return Check(
        "dev-signal",
        FAIL,
        "No input shows signal",
        f"Source is {f.source or 'unknown'}. Is the player playing, and not paused?",
        (fix,),
        cause="no input had signal",
    )


# --- one output (5-11) ----------------------------------------------------------------------------------------------


def _band_phrase(o: OutputSummary) -> str:
    clause = o.parts["band"].clause or "full range"
    for verb in ("Would play ", "Plays ", "Would play", "Plays"):
        if clause.startswith(verb):
            return clause[len(verb) :].strip()
    return clause


def output_checks(f: Facts, n: int, *, earlier_failed: bool = False) -> list[Check]:
    """Checks 5-11 for output ``n``. ``earlier_failed``: a device-wide check failed (the meter check waits)."""
    o = f.summary.outputs[n]
    doc_out = f.doc.outputs[n]
    key = f"o{n}"
    name = o.name
    checks: list[Check] = []
    in_names = [i.name for i in f.summary.inputs]

    # 5. This output unmuted?
    if o.muted:
        never_sent = any(f.statuses.get(f"outputs.{n}.{p}", "unknown") == "unknown" for p in ("gain_db", "mute"))
        confirm = (
            f"I understand: {possessive(name)} level has never been sent, so its level on the device is unknown."
            if never_sent
            else None
        )
        fix = Fix(
            "Unmute", "edit", f"Unmute {name}.", f"{name} unmuted", values={f"outputs.{n}.mute": False}, confirm=confirm
        )
        checks.append(
            Check(f"{key}-mute", FAIL, f"{name} is muted", fixes=(fix,), cause="it was muted", blocks=(f"{key}:level",))
        )
    else:
        checks.append(Check(f"{key}-mute", OK, f"{name} is unmuted"))

    # 6. Any input routed here?
    routes = o.total.routes
    if not routes:
        pick = _route_input(f, n)
        cell = f.doc.inputs[pick].routing[n]
        values: dict[str, Any] = {f"inputs.{pick}.routing.{n}.enabled": True}
        if cell.gain_db != 0:
            values[f"inputs.{pick}.routing.{n}.gain_db"] = 0.0
        raise_db = -cell.gain_db
        confirm = None
        if f.step_db > 0 and raise_db > f.step_db:
            confirm = f"I confirm raising the {in_names[pick]} {ARROW} {name} route level by {plain(raise_db, 1)} dB."
        label = f"Route {in_names[pick]} {ARROW} {name} at 0 dB"
        fix = Fix(
            label,
            "edit",
            f"{label}. {name} will then play {_band_phrase(o)} from {in_names[pick]}.",
            f"{in_names[pick]} {ARROW} {name} at 0 dB",
            values=values,
            confirm=confirm,
            big_raise=confirm is not None,
        )
        open_routing = Fix("Open Routing", "link", href="/routing")
        checks.append(
            Check(
                f"{key}-route",
                FAIL,
                f"No input is routed to {name}",
                fixes=(fix, open_routing),
                cause="no input was routed",
                blocks=(f"{key}:level",),
            )
        )
    else:
        fed = join_words([f"{r.name} ({db(r.route_db)})" for r in routes])
        checks.append(Check(f"{key}-route", OK, f"{name} is fed by {fed}"))

    # 7. The feeding input muted, or its levels very low?
    if not routes:
        checks.append(Check(f"{key}-input", PENDING, "Input muted or levels low", "Checked after the fix."))
    else:
        muted = [r for r in routes if r.muted]
        if len(muted) == len(routes):
            fixes = tuple(
                Fix(
                    f"Unmute {r.name}",
                    "edit",
                    f"Unmute {r.name}.",
                    f"{r.name} unmuted",
                    values={f"inputs.{r.input}.mute": False},
                )
                for r in muted
            )
            words = join_words([r.name for r in muted])
            text = f"{words} {'is' if len(muted) == 1 else 'are'} muted, and nothing else is sent to {name}"
            checks.append(
                Check(f"{key}-input", FAIL, text, fixes=fixes, cause=f"{words} was muted", blocks=(f"{key}:level",))
            )
        elif o.total.max < LOW_TOTAL_DB:
            checks.append(_low_levels(f, n, o, key))
        else:
            checks.append(Check(f"{key}-input", OK, f"Input unmuted and levels fine ({o.total.text} total)"))

    # 8. Does the band pass something, and are no filters empty?
    band_problems = [p for p in o.problems if p.code in ("band_empty", "filter_empty")]
    if band_problems:
        fixes: list[Fix] = []
        for p in band_problems:
            for a in p.actions:
                if a.id == "reset_filter" and "path" in a.params:
                    what = p.text.split("its ", 1)[-1].split(" is empty", 1)[0]
                    fixes.append(
                        Fix(
                            f"Reset {what} to flat",
                            "edit",
                            f"Reset {possessive(name)} {what} to a flat, switched-off filter.",
                            f"{possessive(name)} {what} reset to flat",
                            values={str(a.params["path"]): a.params["flat"]},
                        )
                    )
                elif a.href:
                    fixes.append(Fix(a.label, "link", href=a.href))
        what = "its band passes nothing" if band_problems[0].code == "band_empty" else "a filter was empty"
        checks.append(
            Check(
                f"{key}-band",
                FAIL,
                band_problems[0].text.rstrip("."),
                fixes=tuple(fixes),
                cause=what,
                blocks=(f"{key}:band", f"{key}:eq"),
            )
        )
    else:
        checks.append(Check(f"{key}-band", OK, f"Band passes sound ({_band_phrase(o)}) and no filter is empty"))

    # 9. Is the compressor holding it down?
    crushing = next((p for p in o.problems if p.code == "limiter_crushing"), None)
    if crushing is not None:
        fix = Fix(
            "Turn the compressor off",
            "edit",
            f"Switch {possessive(name)} compressor off (its settings are kept).",
            f"{possessive(name)} compressor off",
            values={f"outputs.{n}.compressor.bypass": True},
        )
        checks.append(
            Check(
                f"{key}-comp",
                FAIL,
                crushing.text.rstrip("."),
                fixes=(fix,),
                cause="its compressor held it down",
                blocks=(f"{key}:compressor",),
            )
        )
    else:
        word = "off" if doc_out.compressor.bypass else "on, but not holding it down"
        checks.append(Check(f"{key}-comp", OK, f"Compressor is {word}"))

    # 10. Playing preset, everything sent?
    checks.append(_sent(f, n, o, key))

    # 11. Signal at this output's meter?
    failed_before = earlier_failed or any(c.result == FAIL for c in checks)
    checks.append(_output_signal(f, n, o, key, failed_before))
    return checks


def _route_input(f: Facts, n: int) -> int:
    """The input to suggest: the one with the most signal, else the summary's hint (the one that last fed it), else
    In 1."""
    if f.levels is not None:
        best, best_level = None, None
        for i in range(len(f.summary.inputs)):
            level = _reading(f.levels.inputs, i)
            if signal(level, f.th) == "yes" and (best_level is None or (level or 0) > best_level):
                best, best_level = i, level
        if best is not None:
            return best
    for p in f.summary.outputs[n].problems:
        for a in p.actions:
            if a.id == "route" and "input" in a.params:
                return int(a.params["input"])
    return 0


def _low_levels(f: Facts, n: int, o: OutputSummary, key: str) -> Check:
    main = next((r for r in o.total.routes if r.input == o.total.input), o.total.routes[0])
    parts = [
        (main.input_db, f"inputs.{main.input}.gain_db", f"{possessive(main.name)} input level"),
        (main.route_db, f"inputs.{main.input}.routing.{n}.gain_db", f"the {main.name} {ARROW} {o.name} route level"),
        (o.total.output, f"outputs.{n}.gain_db", f"{possessive(o.name)} output level"),
    ]
    value, path, words = min(parts, key=lambda p: p[0])
    target = min(0.0, o.total.ceiling)
    confirm = None
    if f.step_db > 0 and target - value > f.step_db:
        confirm = f"I confirm raising {words} by {plain(target - value, 1)} dB."
    fix = Fix(
        f"Set {words} to {plain(target, 1)} dB",
        "edit",
        f"Raise {words} from {plain(value, 1)} to {plain(target, 1)} dB.",
        f"{words} {plain(target, 1)} dB",
        values={path: target},
        confirm=confirm,
        big_raise=confirm is not None,
    )
    return Check(
        f"{key}-input",
        FAIL,
        f"{possessive(o.name)} levels are very low ({o.total.text} total)",
        f"{o.total.breakdown}.",
        (fix,),
        cause="its levels were very low",
        blocks=(f"{key}:level",),
    )


def _sent(f: Facts, n: int, o: OutputSummary, key: str) -> Check:
    if f.playing is None:
        return Check(f"{key}-sent", UNKNOWN, "Can't tell which device preset is playing")
    preset = f.playing + 1
    worst = o.sync.worst
    statuses = [s for p, s in f.statuses.items() if p.startswith(f"outputs.{n}.") or _route_into(p, n)]
    if worst == "failed" or "failed" in statuses:
        count = sum(1 for s in statuses if s == "failed") or 1
        fix = Fix("Retry now", "retry", "Send the settings that failed again.", "settings sent again")
        link = Fix("Device & sync", "link", href=f"/device?preset={preset}#sync")
        text = f"{count} setting{'s' if count != 1 else ''} on {o.name} didn't reach the device"
        return Check(f"{key}-sent", FAIL, text, fixes=(fix, link), cause="a setting didn't reach the device")
    if worst == "changed":
        link = Fix("Device & sync", "link", href=f"/device?preset={preset}#sync")
        text = f"Some settings on {o.name} changed on the device since the app sent them"
        return Check(f"{key}-sent", FAIL, text, fixes=(link,), cause="settings changed on the device")
    if worst == "unknown":
        count = sum(1 for s in statuses if s == "unknown") or 1
        link = Fix("Send now…", "link", href=f"/device?preset={preset}#sync")
        text = (
            f"{count} setting{'s' if count != 1 else ''} on {o.name} {'has' if count == 1 else 'have'}n't been sent or "
            "read yet: can't tell what the device holds"
        )
        return Check(f"{key}-sent", UNKNOWN, text, fixes=(link,))
    if worst == "pending":
        return Check(f"{key}-sent", UNKNOWN, f"Still sending {possessive(o.name)} settings: check again in a moment")
    return Check(f"{key}-sent", OK, f"Preset {preset} is playing and everything on {o.name} was sent")


def _route_into(path: str, n: int) -> bool:
    parts = path.split(".")
    return len(parts) >= 4 and parts[0] == "inputs" and parts[2] == "routing" and parts[3] == str(n)


def _output_signal(f: Facts, n: int, o: OutputSummary, key: str, failed_before: bool) -> Check:
    title = f"Signal at {possessive(o.name)} meter"
    if failed_before:
        return Check(f"{key}-meter", PENDING, title, "Checked after the fixes above.")
    level = _reading(f.levels.outputs, n) if f.levels else None
    state = signal(level, f.th)
    outside = "Check the cable, the amplifier's power, input and volume, and the speaker."
    if state is None:
        return Check(f"{key}-meter", UNKNOWN, f"{title}: can't tell (the meters aren't reporting)", outside)
    if state == "yes":
        return Check(f"{key}-meter", OK, f"The miniDSP is sending sound on {o.name} ({dbfs(level or 0.0)})", outside)
    if state == "unsure":
        return Check(f"{key}-meter", UNKNOWN, f"{title}: can't tell ({o.name} reads {dbfs(level or 0.0)})", outside)
    fed = [r for r in o.total.routes if signal(_reading(f.levels.inputs, r.input) if f.levels else None, f.th) == "yes"]
    detail = (
        f"{fed[0].name} has signal but {o.name} shows none. Its settings above look right to the app; open its page to "
        "look closer."
        if fed
        else f"Nothing that feeds {o.name} shows signal either."
    )
    link = Fix(f"Open {o.name}", "link", href=f"/outputs/{n + 1}")
    text = f"{possessive(o.name)} meter shows no signal"
    return Check(f"{key}-meter", FAIL, text, detail, (link,), cause="no signal reached it")


# --- distorted ------------------------------------------------------------------------------------------------------


def distorted_checks(f: Facts) -> list[Check]:
    checks: list[Check] = []
    names = {f"i{i.index}": i.name for i in f.summary.inputs} | {f"o{o.index}": o.name for o in f.summary.outputs}
    if f.clipped is None:
        checks.append(
            Check(
                "dist-clip",
                UNKNOWN,
                "CLIP lights: can't tell",
                "The CLIP lights live in the browser; open this page with JavaScript on.",
            )
        )
    elif f.clipped:
        which = join_words([names.get(k, k) for k in sorted(f.clipped)])
        checks.append(
            Check(
                "dist-clip",
                FAIL,
                f"CLIP light on: {which}",
                "The light latches when a meter reaches −1 dBFS. Reset it with the CLIP button and listen again.",
                blocks=tuple(f"{k}:level" for k in sorted(f.clipped) if k.startswith("o")),
            )
        )
    else:
        checks.append(Check("dist-clip", OK, "No CLIP light is on"))

    readings = [(i.name, _reading(f.levels.inputs, i.index) if f.levels else None) for i in f.summary.inputs]
    known = [(n, r) for n, r in readings if r is not None]
    hot = [(n, r) for n, r in known if r >= -1.0]
    if not known:
        checks.append(Check("dist-inputs", UNKNOWN, "Input peaks: can't tell (the meters aren't reporting)"))
    elif hot:
        n, r = hot[0]
        checks.append(
            Check(
                "dist-inputs",
                FAIL,
                f"{n} reads {dbfs(r)}: the signal arriving is too hot",
                "Lower the player's or AV receiver's level; the miniDSP can't undo clipping that happens before it.",
                cause="the input was too hot",
            )
        )
    else:
        loudest = max(known, key=lambda x: x[1])
        checks.append(Check("dist-inputs", OK, f"Inputs are below CLIP ({loudest[0]} reads {dbfs(loudest[1])})"))

    high = [o for o in f.summary.outputs if not o.unused and any(p.code == "high_total" for p in o.problems)]
    if high:
        for o in high:
            quieter = Fix(
                f"Make {o.name} quieter…", "task", task="louder", params={"output": o.index + 1, "dir": "down"}
            )
            levels = Fix("Set levels…", "task", task="set-levels")
            checks.append(
                Check(
                    f"dist-o{o.index}",
                    FAIL,
                    o.headroom.text.rstrip(".") if o.headroom.text else f"{o.name}: {o.total.text} total",
                    f"{o.name}: {o.total.text} total ({o.total.breakdown}).",
                    (quieter, levels),
                    cause="its total level was high",
                    blocks=(f"o{o.index}:level",),
                )
            )
    else:
        limit = plain(f.th.high_total_db, 1)
        checks.append(Check("dist-totals", OK, f"No output's total level is at or above +{limit} dB"))
    return checks


# --- running them ---------------------------------------------------------------------------------------------------


def run_checks(f: Facts, what: str, output: int | None = None) -> list[Group]:
    """The checks for "everything is silent" (``all``), "one output is silent" (``one``, ``output`` from 0) or
    "something sounds distorted" (``distorted``), grouped: the device first, then each output in question."""
    if what == "distorted":
        return [Group("", tuple(distorted_checks(f)))]
    device = device_checks(f)
    earlier = any(c.result == FAIL for c in device if c.id != "dev-signal")
    groups = [Group("", tuple(device))]
    if not f.connected:
        return groups
    if what == "one" and output is not None and 0 <= output < len(f.summary.outputs):
        targets = [output]
    else:
        targets = [o.index for o in f.summary.outputs if not o.unused]
    for n in targets:
        groups.append(Group(f.summary.outputs[n].name, tuple(output_checks(f, n, earlier_failed=earlier)), n))
    return groups


def all_checks(groups: Sequence[Group]) -> list[Check]:
    return [c for g in groups for c in g.checks]


def _check_output(f: Facts, check_id: str) -> str:
    """The output a check is about ("o1-route" -> "Sub"), "" for the device-wide ones."""
    head = check_id.split("-", 1)[0]
    if head.startswith("o") and head[1:].isdigit() and int(head[1:]) < len(f.summary.outputs):
        return f.summary.outputs[int(head[1:])].name
    return ""


def find_fix(groups: Sequence[Group], check_id: str, index: int) -> tuple[Check, Fix] | None:
    for check in all_checks(groups):
        if check.id == check_id and check.result == FAIL and 0 <= index < len(check.fixes):
            return check, check.fixes[index]
    return None


# --- the wizard -----------------------------------------------------------------------------------------------------


def facts(ctx: TaskCtx, params: Mapping[str, str]) -> Facts | None:
    session = ctx.session
    summary = ctx.summary(levels=True)
    if session is None or summary is None or summary.preset is None:
        return None
    from app.web.routes import connection  # local: routes is the pages' module

    _, label = connection(ctx.request.app.state.manager, session)
    master = session.master
    try:
        doc = session.desired(summary.preset)
        statuses = session.sync_status(summary.preset)
    except Exception:
        return None
    clip = params.get("clip")
    clipped = None if clip is None else frozenset(k for k in clip.split(",") if k and k != "none")
    settings = ctx.settings
    return Facts(
        summary=summary,
        doc=doc,
        profile=session.profile,
        connected=session.state == SessionState.CONNECTED,  # writes work while the meter stream reconnects
        conn_label=label,
        master_mute=master.mute if master is not None else None,
        volume=master.volume if master is not None else None,
        source=master.source if master is not None else None,
        playing=ctx.playing,
        levels=ctx.levels(),
        th=Thresholds.from_settings(settings),
        statuses=statuses,
        step_db=float(getattr(settings, "gain_step_confirm_db", 6.0)),
        clipped=clipped,
    )


def _what(params: Mapping[str, str], f: Facts | None) -> tuple[str, int | None]:
    """The question and the output (0-based): from the answers, else the first silent output, else "everything"."""
    output = int_param(params, "output")
    n = output - 1 if output is not None else None
    if f is not None and n is not None and not 0 <= n < len(f.summary.outputs):
        n = None
    what = params.get("what")
    if what not in WHATS:
        if n is None and f is not None:
            n = next((o.index for o in f.summary.outputs if o.badge == "silent" and not o.unused), None)
        what = "one" if n is not None else "all"
    if what == "one" and n is None:
        n = 0
    return what, n


def _fixed(params: Mapping[str, str]) -> list[dict[str, str]]:
    try:
        data = json.loads(params.get("fixed") or "[]")
    except ValueError:
        return []
    return (
        [d for d in data if isinstance(d, dict) and isinstance(d.get("done"), str)][:20]
        if isinstance(data, list)
        else []
    )


def _base_params(what: str, n: int | None, params: Mapping[str, str]) -> dict[str, str]:
    out = {"what": what}
    if n is not None and what == "one":
        out["output"] = str(n + 1)
    if params.get("clip") is not None:
        out["clip"] = params["clip"]
    return out


def _step_what(ctx: TaskCtx, params: Mapping[str, str], f: Facts | None) -> Step:
    what, n = _what(params, f)
    outputs = (
        [{"n": o.index + 1, "name": o.name, "badge": o.badge_word, "glyph": o.badge_glyph} for o in f.summary.outputs]
        if f
        else []
    )
    return Step(
        1,
        "What's wrong?",
        "tasks/diagnose/step_what.html",
        _base_params(what, n, params),
        {"what": what, "output_n": (n or 0) + 1, "outputs": outputs, "no_device": f is None},
    )


def _step_checks(
    ctx: TaskCtx,
    params: Mapping[str, str],
    f: Facts | None,
    *,
    message: str | None = None,
    error: str | None = None,
    fixed: list[dict[str, str]] | None = None,
    record: UndoRecord | None = None,
    open_fix: tuple[str, int] | None = None,
    applied: bool = False,
    toast: str | None = None,
) -> Step:
    what, n = _what(params, f)
    groups = run_checks(f, what, n) if f is not None else []
    checks = all_checks(groups)
    first_fail = next((c.id for c in checks if c.result == FAIL), None)
    base = _base_params(what, n, params)
    fixed = _fixed(params) if fixed is None else fixed
    if fixed:
        base["fixed"] = json.dumps(fixed, separators=(",", ":"))
    if record is None:
        record = UndoRecord.from_json(params.get("undo"))
    if record:
        base["undo"] = record.to_json()
    highlight = tuple(b for c in checks if c.result == FAIL for b in c.blocks)
    title = {"all": "Everything is silent", "distorted": "Something sounds distorted"}.get(what)
    if title is None and f is not None and n is not None:
        title = f"{f.summary.outputs[n].name} is silent"
    return Step(
        2,
        "Checks",
        "tasks/diagnose/step_checks.html",
        base,
        {
            "groups": groups,
            "first_fail": first_fail,
            "open_fix": open_fix,
            "question": title or "",
            "message": message,
            "error": error,
            "no_device": f is None,
            "fails": sum(1 for c in checks if c.result == FAIL),
        },
        highlight=highlight,
        applied=applied,
        toast=toast,
        undo=record if record else None,
    )


def _step_done(ctx: TaskCtx, params: Mapping[str, str], f: Facts | None, *, message: str | None = None) -> Step:
    what, n = _what(params, f)
    groups = run_checks(f, what, n) if f is not None else []
    remaining = [c for c in all_checks(groups) if c.result == FAIL]
    fixed = _fixed(params)
    base = _base_params(what, n, params)
    record = UndoRecord.from_json(params.get("undo"))
    if fixed:
        base["fixed"] = json.dumps(fixed, separators=(",", ":"))
    if record:
        base["undo"] = record.to_json()
    lines = []
    silent = what in ("one", "all")
    for item in fixed:
        who, cause = item.get("o") or "", item.get("cause") or ""
        if silent and who and cause:
            lines.append(f"{who} was silent because {cause}. Fixed: {item['done']}.")
        else:
            lines.append(f"Fixed: {item['done']}.")
    return Step(
        3,
        "Done",
        "tasks/diagnose/step_done.html",
        base,
        {"lines": lines, "remaining": remaining, "what": what, "message": message, "no_device": f is None},
        undo=record if record else None,
    )


async def render(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    f = facts(ctx, params)
    step = int_param(params, "step", 1)
    if step == 2:
        return _step_checks(ctx, params, f)
    if step == 3:
        return _step_done(ctx, params, f)
    return _step_what(ctx, params, f)


async def act(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    """``op=fix`` (``check``, ``fix``: which one), ``op=undo`` (the run's fixes) on the Checks or Done step."""
    session = ctx.session
    f = facts(ctx, params)
    if session is None or f is None:
        return _step_checks(ctx, params, f)
    op = params.get("op")
    if op == "undo":
        record = UndoRecord.from_json(params.get("undo"))
        if record is None:
            return _step_checks(ctx, params, f, message="Nothing to undo.", fixed=[], record=UndoRecord(0))
        result = await undo(session, record)
        ctx.forget()
        f = facts(ctx, params)
        if int_param(params, "step") == 3 and not result.error:
            return _step_checks(ctx, params, f, message=result.message, fixed=[], record=UndoRecord(0), applied=True)
        return _step_checks(
            ctx,
            params,
            f,
            message=None if result.error else result.message,
            error=result.error,
            fixed=[] if not result.error else None,
            record=UndoRecord(0) if not result.error else None,
            applied=not result.error,
        )
    if op != "fix":
        return await render(ctx, params)
    what, n = _what(params, f)
    groups = run_checks(f, what, n)
    found = find_fix(groups, params.get("check", ""), int_param(params, "fix", -1) or 0)
    if found is None:
        return _step_checks(ctx, params, f, message="That has already changed: here are the checks again.")
    check, fix = found
    open_fix = (check.id, check.fixes.index(fix))
    if fix.confirm and not confirmed(params):
        return _step_checks(ctx, params, f, error="Tick the box first to confirm.", open_fix=open_fix)
    preset = f.playing if f.playing is not None else (f.summary.preset or 0)
    record: UndoRecord | None = None
    try:
        if fix.kind == "edit":
            record = await apply_edits(session, preset, fix.values, confirm=fix.big_raise and confirmed(params))
        elif fix.kind == "master":
            record = await apply_master(session, preset, confirm=fix.big_raise and confirmed(params), **fix.master)
        elif fix.kind == "retry":
            session.writer.kick(preset)
        elif fix.kind == "source":
            choice = params.get("source")
            if choice not in fix.choices:
                return _step_checks(ctx, params, f, error="Choose a source first.", open_fix=open_fix)
            await session.set_source(choice, source="ui")
        else:
            return _step_checks(ctx, params, f)
    except ConfirmationRequired as exc:
        return _step_checks(ctx, params, f, error=confirm_error(exc), open_fix=open_fix)
    except (DesiredValidationError, MasterValidationError) as exc:
        return _step_checks(ctx, params, f, error=write_error(session, exc), open_fix=open_fix)
    except Exception as exc:  # the daemon is down, a switch is running, ...
        return _step_checks(
            ctx, params, f, error=f"It couldn't be sent: {exc or type(exc).__name__}.", open_fix=open_fix
        )
    ctx.forget()
    fixed = _fixed(params)
    who = _check_output(f, check.id)
    done = fix.done or fix.label
    if fix.kind == "source":
        done = f"Source {params.get('source')}"
    fixed.append({"done": done, "cause": check.cause, "o": who})
    previous = UndoRecord.from_json(params.get("undo"))
    if record is not None and previous and previous.preset == record.preset:
        record = previous.merged(record)
    elif record is None:
        record = previous
    message = f"Fixed: {done}. The checks ran again."
    return _step_checks(
        ctx,
        params,
        facts(ctx, params),
        message=message,
        fixed=fixed,
        record=record,
        applied=True,
        toast=f"Fixed: {done}.",
    )


WIZARD = Wizard(
    id="diagnose",
    label="Something wrong?",
    steps=STEPS,
    render=render,
    act=act,
    intro="Checks, in order, why an output is silent or sounds distorted, and fixes what it can.",
)

__all__ = ["WIZARD", "Check", "Facts", "Fix", "Group", "device_checks", "output_checks", "run_checks", "signal"]
