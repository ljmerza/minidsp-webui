"""Save or load a setup (profiles) (phase 9 design §14.8).

**Save** checks the preset for problems first (the summary engine's problems and cautions, with [Fix first] and
[Save anyway]), then asks for a name (an empty field, no placeholder that looks filled in) and saves it. Saving writes
nothing to the device.

**Load** never loads in one tap: which profile (each with what it does), into which preset (each saying what it holds
now), when (store it, or store it and play it now), then one check that lists the changes by name, with a required
checkbox per big raise ("I confirm raising Shakers output level by 12 dB"). Load sends exactly what was confirmed
(`Confirmed`); if the state changed meanwhile so that the load would ask again, the check re-renders with the new
list instead of a second dialog. The chosen preset is kept afterwards (P11).

Undo of a load puts back the preset's revision from just before it, but only while the preset still holds exactly
what the load stored: if it changed since (another tab, Home Assistant), Undo refuses rather than overwrite that.
"""

import contextlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.daemon.client import DaemonError
from app.model.config_doc import ConfigDoc
from app.state import ProfileNameTaken, RevisionConflict
from app.sync import profiles as svc
from app.sync.session import (
    BULK_TIMEOUT_S,
    BigRaise,
    ConfirmationRequired,
    Confirmed,
    DesiredValidationError,
    MasterValidationError,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    StateUnavailable,
    parse_doc,
)
from app.sync.summary import CAUTION, PROBLEM, session_summary, summarize
from app.web.tasks import UNDO_GRACE_S, UNDO_WINDOW_S, Step, TaskCtx, Wizard, float_param, int_param, write_error

STEPS_SAVE = ("Save or load", "Check", "Name", "Saved")
STEPS_LOAD = ("Save or load", "Profile", "Preset", "When", "Check", "Result")
SAVE, LOAD = "save", "load"
STORE, PLAY = "store", "play"
CHANGE_LINES = 3  # change lines shown before "N more ▸" (the rest fold away)
MAX_NAME = 80
MAX_NOTE = 500

LOAD_ERRORS = (
    svc.ProfileError,
    svc.ProfilesUnavailable,
    ProfileNameTaken,
    MasterValidationError,  # includes ConfirmationRequired, DesiredValidationError, UnknownPreset
    RevisionConflict,
    StateUnavailable,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    DaemonError,
)


# --- answers ----------------------------------------------------------------------------------------------------------


@dataclass
class Answers:
    do: str  # save | load
    source: int  # the preset to save (0-based)
    name: str
    note: str
    profile: int | None
    target: int | None  # the preset to load into (0-based)
    when: str  # store | play


def _preset(params: Mapping[str, str], key: str, n_presets: int) -> int | None:
    value = int_param(params, key)
    return value - 1 if value is not None and 1 <= value <= n_presets else None


def answers(params: Mapping[str, str], n_presets: int, playing: int | None) -> Answers:
    profile = int_param(params, "profile")
    do = params.get("do")
    if do not in (SAVE, LOAD):
        do = LOAD if profile is not None else SAVE
    source = _preset(params, "preset", n_presets)
    return Answers(
        do=do,
        source=source if source is not None else (playing or 0),
        name=(params.get("name") or "").strip()[:MAX_NAME],
        note=(params.get("note") or "").strip()[:MAX_NOTE],
        profile=profile if profile is not None and profile > 0 else None,
        target=_preset(params, "target", n_presets),
        when=PLAY if params.get("when") == PLAY else STORE,
    )


def _params(a: Answers) -> dict[str, str]:
    out = {"do": a.do}
    if a.do == SAVE:
        out["preset"] = str(a.source + 1)
        if a.name:
            out["name"] = a.name
        if a.note:
            out["note"] = a.note
    else:
        if a.profile is not None:
            out["profile"] = str(a.profile)
        if a.target is not None:
            out["target"] = str(a.target + 1)
        out["when"] = a.when
    return out


def _steps(a: Answers) -> tuple[str, ...]:
    return STEPS_SAVE if a.do == SAVE else STEPS_LOAD


def _repo(ctx: TaskCtx) -> Any:
    session = ctx.session
    if session is not None and getattr(session, "repo", None) is not None:
        return session.repo
    return getattr(ctx.request.app.state, "repo", None)


# --- pure helpers -----------------------------------------------------------------------------------------------------


def confirm_value(item: Mapping[str, Any]) -> str:
    """A raise as the value of its checkbox: ``r|<path>|<delta_db>|<kind>``."""
    return f"r|{item['path']}|{float(item['delta_db']):g}|{item.get('kind', 'gain')}"


def confirmed_from(params: Mapping[str, str]) -> Confirmed:
    """The raises and FIR loads the user ticked (``ok_0``, ``ok_1``, … with `confirm_value` / ``f|<output>``)."""
    raises: list[BigRaise] = []
    firs: set[int] = set()
    for key, value in params.items():
        if not key.startswith("ok_") or not isinstance(value, str):
            continue
        parts = value.split("|")
        if parts[0] == "r" and len(parts) == 4:
            with contextlib.suppress(ValueError):
                raises.append(BigRaise(parts[1], float(parts[2]), parts[3] or "gain"))
        elif parts[0] == "f" and len(parts) == 2 and parts[1].isdigit():
            firs.add(int(parts[1]))
    return Confirmed(raises=tuple(raises), fir_loads=frozenset(firs))


def change_rank(path: str, raised: set[str] | frozenset[str] = frozenset()) -> int:
    """The order of a load's change lines: big raises, then output levels, delays, mutes and polarity, then inputs and
    routes, then filters (a list led by filter coefficient rows hides what matters)."""
    if path in raised:
        return 0
    parts = path.split(".")
    if parts[0] == "outputs" and parts[2:] and parts[2] in ("gain_db", "delay_ms", "mute", "invert"):
        return 1
    if parts[0] == "inputs" and (parts[2:3] == ["gain_db"] or parts[2:3] == ["mute"]):
        return 2
    if parts[2:3] == ["routing"]:
        return 3
    return 4


def health_items(summary: Any) -> list[dict[str, str]]:
    """The problems and cautions to look at before saving (info notes aren't problems)."""
    glyph = {PROBLEM: "⛔", CAUTION: "⚠"}
    return [
        {"glyph": glyph[p.level], "text": p.text, "level": p.level}
        for p in summary.problems
        if p.level in (PROBLEM, CAUTION)
    ]


def what_it_does(session: Any, doc: ConfigDoc) -> list[str]:
    """One summary-engine sentence per output in use: "Shakers: Plays 20–80 Hz (steep), fed by In 1. …"."""
    summary = summarize(
        doc,
        session.profile,
        getattr(session, "channel_meta", {}),
        getattr(session, "settings", None),
        {},
        None,
    )
    return [f"{o.name}: {o.sentence}" for o in summary.outputs if not o.unused]


# --- steps ----------------------------------------------------------------------------------------------------------


def _step_choose(ctx: TaskCtx, a: Answers, error: str | None = None) -> Step:
    session = ctx.session
    presets = []
    if session is not None:
        playing = ctx.playing
        presets = [
            {"n": p + 1, "label": f"Preset {p + 1}" + (" (playing)" if p == playing else "")}
            for p in range(session.profile.n_presets)
        ]
    return Step(
        1,
        "Save or load?",
        "tasks/profiles/step_choose.html",
        _params(a),
        {"do": a.do, "preset_choices": presets, "source": a.source + 1, "no_device": session is None, "error": error},
        steps=_steps(a),
    )


def _step_health(ctx: TaskCtx, a: Answers) -> Step:
    session = ctx.session
    assert session is not None
    summary = session_summary(session, a.source)
    items = health_items(summary) if summary is not None else []
    fix_href = ctx.task_href("diagnose") if a.source == ctx.playing else f"{ctx.root}/routing"
    return Step(
        2,
        "Check",
        "tasks/profiles/step_health.html",
        _params(a),
        {"items": items, "preset_n": a.source + 1, "fix_href": fix_href, "playing": a.source == ctx.playing},
        steps=STEPS_SAVE,
    )


def _step_name(ctx: TaskCtx, a: Answers, error: str | None = None) -> Step:
    return Step(
        3,
        "Name",
        "tasks/profiles/step_name.html",
        _params(a),
        {"name": a.name, "note": a.note, "preset_n": a.source + 1, "error": error},
        steps=STEPS_SAVE,
    )


async def _step_saved(ctx: TaskCtx, a: Answers, profile_id: int, *, applied: bool = False) -> Step:
    session = ctx.session
    assert session is not None
    name = a.name
    same: list[int] = []
    with contextlib.suppress(Exception):
        view = await svc.get_profile(_repo(ctx), profile_id)
        name = view.profile.name
        same = svc.matching_presets(session, svc.profile_doc(view.profile))
    text = f"Saved “{name}” from preset {a.source + 1}."
    if same:
        text += " Same as " + ", ".join(f"preset {p + 1}" for p in same) + " right now."
    params = {**_params(a), "saved": str(profile_id)}
    load = {"do": LOAD, "profile": str(profile_id)}
    return Step(
        4,
        "Saved",
        "tasks/profiles/step_saved.html",
        params,
        {"message": text, "load_href": ctx.task_href("profiles", **load)},
        applied=applied,
        toast=text if applied else None,
        steps=STEPS_SAVE,
    )


async def _profiles(ctx: TaskCtx) -> list[dict[str, Any]]:
    """Every saved profile with what it does, whether it can be loaded here, and which presets it matches."""
    from app.web import profiles as profiles_ui  # local: it imports the editing pages' helpers

    session = ctx.session
    listing = await profiles_ui.list_context(ctx.request, session)
    if listing.get("profiles_error"):
        raise svc.ProfilesUnavailable(listing["profiles_error"])
    views = {v.profile.id: v.profile for v in await svc.list_profiles(_repo(ctx))}
    items = []
    for p in listing.get("profiles", []):
        lines: list[str] = []
        profile = views.get(p["id"])
        if profile is not None and p["valid"] and session is not None:
            with contextlib.suppress(Exception):
                lines = what_it_does(session, svc.profile_doc(profile))
        same = sorted({*p.get("matches", []), *(s["preset"] for s in p.get("loaded", []) if s.get("same"))})
        same_text = ("same as preset " + ", ".join(str(n + 1) for n in same)) if same else ""
        items.append({**p, "lines": lines, "same": same, "same_text": same_text})
    return items


async def _step_profile(ctx: TaskCtx, a: Answers, error: str | None = None) -> Step:
    items: list[dict[str, Any]] = []
    problem = None
    try:
        items = await _profiles(ctx)
    except (svc.ProfilesUnavailable, StateUnavailable) as exc:
        problem = str(exc)
    chosen = a.profile if any(p["id"] == a.profile for p in items) else None
    if chosen is None:
        chosen = next((p["id"] for p in items if p["loadable"]), None)
    playing = ctx.playing
    compare = f"{ctx.root}/profiles?b=preset:{playing if playing is not None else 0}&a=profile:"
    return Step(
        2,
        "Which profile?",
        "tasks/profiles/step_profile.html",
        _params(a),
        {
            "items": items,
            "chosen": chosen,
            "compare": compare,
            "error": error or problem,
            "save_href": ctx.task_href("profiles", do=SAVE),
        },
        steps=STEPS_LOAD,
    )


async def _preset_rows(ctx: TaskCtx, profile_id: int | None) -> list[dict[str, Any]]:
    """Each preset with what it holds now: "1 ▶ playing · Same as Music", "2 · ⏸ 216 waiting"."""
    session = ctx.session
    assert session is not None
    playing = ctx.playing
    same_as: dict[int, list[str]] = {}
    this: set[int] = set()
    with contextlib.suppress(Exception):
        for p in await _profiles(ctx):
            for preset in p["same"]:
                same_as.setdefault(preset, []).append(p["name"])
                if p["id"] == profile_id:
                    this.add(preset)
    rows = []
    for p in range(session.profile.n_presets):
        bits = []
        if p == playing:
            bits.append("▶ playing")
        if p in this:
            bits.append("already holds this profile")
        elif same_as.get(p):
            bits.append("Same as " + ", ".join(same_as[p]))
        waiting = 0
        with contextlib.suppress(Exception):
            waiting = int(session.sync_summary(p)["counts"].get("pending", 0))
        if waiting and p != playing:
            bits.append(f"⏸ {waiting} waiting")
        rows.append({"n": p + 1, "text": " · ".join([str(p + 1), *bits]), "playing": p == playing})
    return rows


async def _profile_name(ctx: TaskCtx, profile_id: int | None) -> str | None:
    if profile_id is None:
        return None
    try:
        return (await svc.get_profile(_repo(ctx), profile_id)).profile.name
    except (svc.ProfileError, svc.ProfilesUnavailable):
        return None


async def _step_target(ctx: TaskCtx, a: Answers, error: str | None = None) -> Step:
    rows = await _preset_rows(ctx, a.profile)
    target = a.target if a.target is not None else ctx.playing
    return Step(
        3,
        "Into which preset?",
        "tasks/profiles/step_target.html",
        _params(a),
        {"rows": rows, "target": (target or 0) + 1, "name": await _profile_name(ctx, a.profile), "error": error},
        steps=STEPS_LOAD,
    )


def _when_ctx(ctx: TaskCtx, a: Answers) -> dict[str, Any]:
    session = ctx.session
    assert session is not None and a.target is not None
    return {
        "target_n": a.target + 1,
        "is_playing": a.target == ctx.playing,
        "mute_wrap": bool(getattr(session.settings, "mute_wrap_bulk_apply", True)),
    }


async def _step_when(ctx: TaskCtx, a: Answers) -> Step:
    return Step(
        4,
        "When?",
        "tasks/profiles/step_when.html",
        _params(a),
        {**_when_ctx(ctx, a), "when": a.when, "name": await _profile_name(ctx, a.profile)},
        steps=STEPS_LOAD,
    )


async def check_context(ctx: TaskCtx, a: Answers) -> dict[str, Any]:
    """What loading would change, in words, with a line per big raise to confirm (design §14.8 step 4)."""
    from app.web import profiles as profiles_ui  # local: it imports the editing pages' helpers
    from app.web.sync_ui import raise_rows

    session = ctx.session
    assert session is not None and a.profile is not None and a.target is not None
    info = await svc.load_preview(session, a.profile, a.target)
    changes = profiles_ui.diff_rows(session, await svc.load_diff(session, a.profile, a.target))
    raised = {r["path"] for r in info["raises"]}
    changes.sort(key=lambda c: change_rank(c["path"], raised))
    lines = [f"{c['label']} {c['a']} → {c['b']}" for c in changes]
    raises = raise_rows(session, info["raises"])
    confirms = [
        {"value": confirm_value(r), "text": f"I confirm raising {r['label']} by {float(r['delta_db']):g} dB."}
        for r in raises
    ]
    names = getattr(session, "channel_meta", {})
    for n in info["fir_loads"]:
        meta = names.get(("output", n))
        name = meta.name if meta is not None and meta.name else f"Out {n + 1}"
        confirms.append(
            {"value": f"f|{n}", "text": f"I understand {name} goes silent for a moment while its FIR filter loads."}
        )
    activate = a.when == PLAY and a.target != ctx.playing
    if info["active"]:
        when = f"Preset {a.target + 1} is playing, so it is sent now" + (
            " (outputs are muted for a few seconds while it lands)." if info["mute_wrap"] else "."
        )
    elif activate:
        when = f"Then the device switches to preset {a.target + 1}; sound drops for a few seconds."
    else:
        when = f"Preset {a.target + 1} isn't playing: it waits and is sent when you switch to it."
    return {
        "name": info["name"],
        "target_n": a.target + 1,
        "count": info["changes"],
        "first": lines[:CHANGE_LINES],
        "rest": lines[CHANGE_LINES:],
        "confirms": confirms,
        "unknown": info["unknown"],
        "refusals": info["problems"],
        "when_text": when,
    }


async def _step_check(ctx: TaskCtx, a: Answers, *, error: str | None = None, message: str | None = None) -> Step:
    data: dict[str, Any] = {"error": error, "message": message}
    try:
        data.update(await check_context(ctx, a))
    except LOAD_ERRORS as exc:
        data["error"] = _load_error(ctx, exc)
        data["broken"] = True
    return Step(5, "Check", "tasks/profiles/step_check.html", _params(a), data, steps=STEPS_LOAD)


def _result_text(ctx: TaskCtx, name: str, target: int) -> tuple[str, bool]:
    """("✓ Movies is playing on preset 1.", waiting?)."""
    if target == ctx.playing:
        return f"“{name}” is playing on preset {target + 1}.", False
    return f"“{name}” is in preset {target + 1} (waiting until you switch to it).", True


async def _step_result(
    ctx: TaskCtx,
    a: Answers,
    params: Mapping[str, str],
    *,
    message: str | None = None,
    note: str = "",
    error: str | None = None,
    applied: bool = False,
    toast: str | None = None,
    undo: dict[str, str] | None = None,
) -> Step:
    assert a.target is not None
    name = await _profile_name(ctx, a.profile) or "The profile"
    text, waiting = _result_text(ctx, name, a.target)
    extra = {k: params[k] for k in ("rev", "prev", "at") if params.get(k)} if undo is None else undo
    at = float(extra.get("at", 0) or 0)
    left = int(max(0.0, UNDO_WINDOW_S - (time.time() - at))) if extra.get("rev") else 0
    again = {"do": LOAD, "target": str(a.target + 1), "when": a.when}
    return Step(
        6,
        "Result",
        "tasks/profiles/step_result.html",
        {**_params(a), **extra},
        {
            "result": text + note,
            "waiting": waiting,
            "target_n": a.target + 1,
            "message": message,
            "error": error,
            "undo_left": left,
            "again_href": ctx.task_href("profiles", **again),
        },
        applied=applied,
        toast=toast,
        steps=STEPS_LOAD,
    )


# --- render / act -----------------------------------------------------------------------------------------------------


async def render(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    session = ctx.session
    n_presets = session.profile.n_presets if session is not None else 4
    a = answers(params, n_presets, ctx.playing)
    # Opened from a profile button (?profile=) or "+ Save current as…" (?do=save): straight to that path's first step.
    start = 2 if params.get("do") in (SAVE, LOAD) or params.get("profile") else 1
    step = int_param(params, "step", start) or start
    if session is None or step <= 1 or not getattr(session, "loaded", False):
        return _step_choose(ctx, a)
    if a.do == SAVE:
        if step == 2:
            return _step_health(ctx, a)
        saved = int_param(params, "saved")
        if step >= 4 and saved is not None:
            return await _step_saved(ctx, a, saved)
        return _step_name(ctx, a)
    if step == 2 or a.profile is None:
        return await _step_profile(ctx, a)
    if step == 3 or a.target is None:
        return await _step_target(ctx, a)
    if step == 4:
        return await _step_when(ctx, a)
    if step == 5 or not params.get("rev"):
        return await _step_check(ctx, a)
    return await _step_result(ctx, a, params)


def _load_error(ctx: TaskCtx, exc: Exception) -> str:
    from app.web.sync_ui import humanize_paths

    session = ctx.session
    if isinstance(exc, DesiredValidationError) and session is not None:
        return write_error(session, exc)
    if isinstance(exc, svc.ProfileNotFound):
        return "That profile doesn't exist any more."
    if isinstance(exc, PresetSwitchInProgress):
        return "The device is switching presets; try again in a moment."
    if isinstance(exc, DaemonError):
        return f"The device did not accept it: {exc}."
    return humanize_paths(session, str(exc)).rstrip(".") + "." if str(exc) else "It couldn't be loaded."


async def _save(ctx: TaskCtx, a: Answers) -> Step:
    session = ctx.session
    assert session is not None
    if not a.name:
        return _step_name(ctx, a, error="Type a name first.")
    try:
        profile = await svc.save_preset(session, a.source, a.name, description=a.note or None)
    except ProfileNameTaken:
        return _step_name(ctx, a, error=f"There is already a profile named “{a.name}”. Choose another name.")
    except (svc.ProfileError, svc.ProfilesUnavailable, StateUnavailable, DesiredValidationError) as exc:
        return _step_name(ctx, a, error=f"It couldn't be saved: {str(exc).rstrip('.')}.")
    return await _step_saved(ctx, a, profile.id, applied=True)


async def _load(ctx: TaskCtx, a: Answers, params: Mapping[str, str]) -> Step:
    from app.web.profiles import outcome_note  # local: it imports the editing pages' helpers

    session = ctx.session
    assert session is not None and a.profile is not None and a.target is not None
    confirmed = confirmed_from(params)
    activate = a.when == PLAY and a.target != ctx.playing
    try:
        # A history row of what the preset holds now (none is written if the newest row already holds it), so Undo has
        # something to go back to even for a preset that was never edited.
        before = await session.commit(a.target, force=True)
        result = await svc.load_profile(
            session, a.profile, a.target, activate=activate, confirmed=confirmed, timeout=BULK_TIMEOUT_S
        )
    except ConfirmationRequired:
        # Nothing ticked, or the settings changed since the list was made so a raise grew: ask again, here.
        if confirmed.raises or confirmed.fir_loads:
            message = "The settings changed since this list was made, so here it is again. Confirm the lines below."
        else:
            message = "Tick the box first to confirm the raise."
        return await _step_check(ctx, a, error=message)
    except LOAD_ERRORS as exc:
        return await _step_check(ctx, a, error=_load_error(ctx, exc))
    ctx.forget()
    note, _level = outcome_note(session, a.target, result.edit.flush)
    undo: dict[str, str] = {}
    if result.edit.paths and before and before != result.edit.revision:
        undo = {"rev": str(result.edit.revision), "prev": str(before), "at": f"{time.time():.1f}"}
    name = result.profile.name
    text, _waiting = _result_text(ctx, name, a.target)
    return await _step_result(
        ctx, a, params, note=note, applied=True, toast=text, undo=undo, message=None if undo else "Nothing changed."
    )


async def undo_load(
    ctx: TaskCtx, target: int, rev: int, prev: int, at: float, *, now: float | None = None
) -> tuple[bool, str]:
    """Put back the preset's revision ``prev`` (from just before the load, which made ``rev``), while the preset still
    holds exactly what the load stored."""
    session = ctx.session
    assert session is not None
    now = time.time() if now is None else now
    if now - at > UNDO_WINDOW_S + UNDO_GRACE_S:
        return False, "Undo is only offered for 30 seconds. The History tab of Profiles can still go back."
    try:
        stored = await svc.revision(session, target, rev)
        before = await svc.revision(session, target, prev)
    except (svc.ProfileError, svc.ProfilesUnavailable, StateUnavailable):
        return False, "There is nothing to go back to. The History tab of Profiles lists what there is."
    current = await session.get_desired(target)
    if session.revisions.get(target, 0) != rev or current != parse_doc(stored.desired):
        return False, (
            f"Preset {target + 1} was changed since, so Undo left it as is rather than overwrite that. "
            "The History tab of Profiles can still go back."
        )
    try:
        # Back to values the preset had a moment ago, so the big-step guard doesn't ask again.
        await svc.revert_to(session, target, before.revision, confirm=True, timeout=BULK_TIMEOUT_S)
    except LOAD_ERRORS as exc:
        return False, f"Undo failed: {_load_error(ctx, exc)}"
    return True, f"Undone: preset {target + 1} holds what it had before."


async def act(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    """``op=save`` (Name), ``op=apply`` (Check's Load), ``op=undo`` and ``op=switch`` (Result)."""
    session = ctx.session
    assert session is not None
    a = answers(params, session.profile.n_presets, ctx.playing)
    op = params.get("op")
    if op == "save" and a.do == SAVE:
        return await _save(ctx, a)
    if a.do != LOAD or a.profile is None or a.target is None:
        return await render(ctx, params)
    if op == "apply":
        return await _load(ctx, a, params)
    if op == "undo":
        rev, prev = int_param(params, "rev"), int_param(params, "prev")
        if rev is None or prev is None:
            return await _step_result(ctx, a, params, message="Nothing to undo.", undo={})
        ok, text = await undo_load(ctx, a.target, rev, prev, float_param(params, "at", 0.0) or 0.0)
        ctx.forget()
        return await _step_result(
            ctx, a, params, message=text if ok else None, error=None if ok else text, applied=ok, undo={}
        )
    if op == "switch":
        try:
            await session.activate_preset(a.target, flush=True, timeout=BULK_TIMEOUT_S, source="ui")
        except LOAD_ERRORS as exc:
            return await _step_result(ctx, a, params, error=_load_error(ctx, exc))
        ctx.forget()
        return await _step_result(ctx, a, params, message=f"Switched to preset {a.target + 1}.", applied=True)
    return await render(ctx, params)


WIZARD = Wizard(
    id="profiles",
    label="Save or load a setup",
    steps=STEPS_LOAD,
    render=render,
    act=act,
    intro="Saves what a preset holds as a profile (after a check for problems), or loads one with one clear confirm.",
)

__all__ = ["WIZARD", "answers", "check_context", "confirm_value", "confirmed_from", "health_items", "undo_load"]
