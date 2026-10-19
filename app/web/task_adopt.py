"""Set up this device (adopt) (phase 9 design §14.7): tell the app what the miniDSP holds, step by step.

It wraps the existing adopt (`DeviceSession.adopt`, the same call as the Device & sync form and the API): nothing is
written until the last step, and only with Overwrite. The four ways in:

- **With this app before, and I have a backup file**: a backup (Settings → Backup) or a profile export, as JSON. The
  chosen presets' documents come from it.
- **With minidsp commands**: the command script, read by the existing parser (ignored lines are listed).
- **With miniDSP's own software, or I don't know**: Don't know yet (nothing is assumed), then Check device reads what
  the device can report; the Device & sync page can then use those values (with its own confirm and Undo).
- **It's brand new or factory reset**: the defaults. **The adopt guard stays**: what will be remembered is listed and
  "My device really is at factory defaults" must be ticked (the session refuses ``trust`` of the defaults without
  ``confirm_defaults``). A channel with a custom name is a sign it was set up before, so the wizard says so.

An uploaded file or pasted script can be far too big for a URL, so it is kept in memory under a short random token
(`_STASH`) while the wizard runs; after a restart the wizard asks for it again.
"""

import contextlib
import json
import secrets
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from starlette.datastructures import UploadFile

from app.daemon.client import DaemonError
from app.model.config_doc import ConfigDoc, default_config_doc
from app.model.payload import device_values
from app.sync import profiles as profiles_svc
from app.sync.session import (
    BULK_TIMEOUT_S,
    AdoptMode,
    ConfirmationRequired,
    DesiredValidationError,
    MasterValidationError,
    PresetSwitchInProgress,
    StateUnavailable,
    factory_defaults_summary,
    parse_doc,
)
from app.sync.summary import summarize
from app.web.tasks import Step, TaskCtx, Wizard, int_param

STEPS = ("Why", "How", "Presets", "Preview", "Remember or write", "Done")
BACKUP, SCRIPT, SOFTWARE, FACTORY = "backup", "script", "software", "factory"
HOWS = (BACKUP, SCRIPT, SOFTWARE, FACTORY)
TRUST, PUSH = AdoptMode.TRUST.value, AdoptMode.PUSH.value
MAX_FILE_BYTES = 900_000  # within the default request limit (app/api/limits.py); bigger backups: Settings → Backup
MAX_SCRIPT_BYTES = 200_000
STASH_TTL_S = 3600.0
STASH_MAX = 16


# --- the stash ------------------------------------------------------------------------------------------------------


@dataclass
class Source:
    """What the user gave: a script, or documents per preset (``docs``; key None: the same one for any preset)."""

    how: str
    label: str = ""
    script: str | None = None
    docs: dict[int | None, dict[str, Any]] = field(default_factory=dict)
    at: float = field(default_factory=time.time)


_STASH: dict[str, Source] = {}


def stash(source: Source, *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    for key in [k for k, v in _STASH.items() if now - v.at > STASH_TTL_S]:
        del _STASH[key]
    while len(_STASH) >= STASH_MAX:
        del _STASH[min(_STASH, key=lambda k: _STASH[k].at)]
    token = secrets.token_urlsafe(12)
    source.at = now
    _STASH[token] = source
    return token


def unstash(token: str | None, *, now: float | None = None) -> Source | None:
    now = time.time() if now is None else now
    source = _STASH.get(token or "")
    if source is None or now - source.at > STASH_TTL_S:
        return None
    return source


# --- pure helpers -----------------------------------------------------------------------------------------------------


def _label(kind: str, index: int) -> str:
    return f"{'In' if kind == 'input' else 'Out'} {index + 1}"


def custom_names(channel_meta: Mapping[tuple[str, int], Any] | None) -> list[tuple[str, str]]:
    """``[("Out 1", "Shakers"), …]``: the channels given a name of their own in Settings, outputs first."""
    found = []
    for (kind, index), meta in sorted((channel_meta or {}).items(), key=lambda kv: (kv[0][0] != "output", kv[0])):
        name = (getattr(meta, "name", None) or "").strip()
        if name and name != _label(kind, index):
            found.append((_label(kind, index), name))
    return found


def named_warning(channel_meta: Mapping[tuple[str, int], Any] | None) -> str | None:
    """The factory-defaults path's warning (design §14.7): a custom name means the device was probably set up before.
    ``You named Out 1 “Shakers”, so it was probably set up before. Choose another option unless you've reset it.``"""
    names = custom_names(channel_meta)
    if not names:
        return None
    shown = [f"{label} “{name}”" for label, name in names[:3]]
    if len(names) > 3:
        shown.append(f"{len(names) - 3} more")
    joined = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    return f"You named {joined}, so it was probably set up before. Choose another option unless you've reset it."


def docs_from_json(data: Any, serial: int, product_name: str | None) -> tuple[dict[int | None, dict[str, Any]], str]:
    """The documents of a backup (``minidsp-webui-backup``: this device's presets, or the only device's) or of a
    profile export (one document for any preset). Raises ValueError with a reason in words."""
    from app.sync.backup import BackupError, parse_backup

    if not isinstance(data, dict):
        raise ValueError("this isn't a backup or profile file from this app (expected a JSON object)")
    if data.get("format") == profiles_svc.EXPORT_FORMAT:
        config = data.get("config")
        if not isinstance(config, dict):
            raise ValueError("the profile file has no settings in it")
        if data.get("product_name") and product_name and data["product_name"] != product_name:
            raise ValueError(f"the profile is for a {data['product_name']}; this device is a {product_name}")
        return {None: config}, f"profile “{data.get('name') or 'unnamed'}”"
    try:
        backup = parse_backup(data)
    except BackupError as exc:
        reason = "; ".join(exc.problems[:3]) if exc.problems else str(exc)
        raise ValueError(f"the file can't be used: {reason}") from None
    devices = [d for d in backup.devices if d.serial == serial] or [
        d for d in backup.devices if not product_name or d.product_name in (None, product_name)
    ]
    if not devices:
        raise ValueError("the backup has no device of this model in it")
    if len(devices) > 1:
        raise ValueError("the backup holds several devices of this model; set up from Settings → Backup instead")
    device = devices[0]
    docs: dict[int | None, dict[str, Any]] = {p.preset: p.desired for p in device.presets}
    if not docs:
        raise ValueError("the backup has no presets for this device")
    return docs, f"backup of device {device.serial}"


# --- answers ----------------------------------------------------------------------------------------------------------


@dataclass
class Answers:
    how: str
    token: str | None
    factory_ok: bool
    presets: list[int]  # 0-based
    choice: str  # trust | push


def _presets(params: Mapping[str, str], n_presets: int, playing: int | None) -> list[int]:
    chosen: set[int] = set()
    raw = params.get("presets")
    if raw is not None:
        for part in raw.split(","):
            if part.strip().isdigit() and 1 <= int(part) <= n_presets:
                chosen.add(int(part) - 1)
    for n in range(1, n_presets + 1):
        if params.get(f"p_{n}") in ("1", "on", "true"):
            chosen.add(n - 1)
    if not chosen and raw is None and not any(k.startswith("p_") for k in params):
        chosen.add(playing or 0)
    return sorted(chosen)


def answers(params: Mapping[str, str], n_presets: int, playing: int | None) -> Answers:
    how = params.get("how") if params.get("how") in HOWS else BACKUP
    return Answers(
        how=how,
        token=params.get("token") or None,
        factory_ok=params.get("factory_ok") in ("1", "on", "true"),
        presets=_presets(params, n_presets, playing),
        choice=PUSH if params.get("choice") == PUSH else TRUST,
    )


def _params(a: Answers) -> dict[str, str]:
    out = {"how": a.how}
    if a.token and a.how in (BACKUP, SCRIPT):
        out["token"] = a.token
    if a.factory_ok and a.how == FACTORY:
        out["factory_ok"] = "1"
    out["presets"] = ",".join(str(p + 1) for p in a.presets)
    if a.how != SOFTWARE:
        out["choice"] = a.choice
    return out


# --- steps ------------------------------------------------------------------------------------------------------------


def _step_why(ctx: TaskCtx, a: Answers) -> Step:
    session = ctx.session
    adoption = getattr(session, "adoption", None) if session is not None else None
    needs = bool(getattr(session, "needs_adoption", False)) if session is not None else False
    return Step(
        1,
        "Why set it up?",
        "tasks/adopt/step_why.html",
        _params(a),
        {"needs": needs, "adoption": adoption, "no_device": session is None},
    )


def _step_how(ctx: TaskCtx, a: Answers, error: str | None = None, errors: Sequence[str] = ()) -> Step:
    session = ctx.session
    assert session is not None
    source = unstash(a.token) if a.how in (BACKUP, SCRIPT) else None
    return Step(
        2,
        "How was it set up?",
        "tasks/adopt/step_how.html",
        _params(a),
        {
            "how": a.how,
            "error": error,
            "errors": list(errors),
            "have": source.label if source is not None and source.how == a.how else None,
            "script": source.script if source is not None and source.script else "",
            "warning": named_warning(getattr(session, "channel_meta", {})),
            "defaults_summary": factory_defaults_summary(session.profile),
            "factory_ok": a.factory_ok,
            "multipart": True,
        },
    )


def _step_presets(ctx: TaskCtx, a: Answers, error: str | None = None) -> Step:
    session = ctx.session
    assert session is not None
    playing = ctx.playing
    source = unstash(a.token)
    have = None
    if source is not None and a.how == BACKUP and None not in source.docs:
        have = sorted(k for k in source.docs if k is not None)
    rows = [
        {
            "n": p + 1,
            "label": f"Preset {p + 1}" + (" (playing)" if p == playing else ""),
            "checked": p in a.presets,
            "missing": have is not None and p not in have,
        }
        for p in range(session.profile.n_presets)
    ]
    return Step(3, "Which presets?", "tasks/adopt/step_presets.html", _params(a), {"rows": rows, "error": error})


def _doc_for(ctx: TaskCtx, a: Answers, source: Source | None, preset: int) -> tuple[ConfigDoc | None, list[Any]]:
    """The document a preset would be set up from, and the script's ignored lines."""
    session = ctx.session
    assert session is not None
    if a.how in (SOFTWARE, FACTORY):
        return default_config_doc(session.profile), []
    if source is None:
        return None, []
    if source.script is not None:
        parsed = session.parse_cli_script(source.script)
        return parsed.apply(default_config_doc(session.profile)), parsed.warnings
    raw = source.docs.get(preset, source.docs.get(None))
    return (parse_doc(raw) if raw is not None else None), []


def _need_source(ctx: TaskCtx, a: Answers) -> Step | None:
    """Back to step 2 when the file or script it needs is gone (expired, another worker, a restart)."""
    if a.how in (BACKUP, SCRIPT) and unstash(a.token) is None:
        what = "backup file" if a.how == BACKUP else "script"
        return _step_how(ctx, a, error=f"The {what} isn't here any more (it is kept for an hour). Give it again.")
    if a.how == FACTORY and not a.factory_ok:
        return _step_how(ctx, a, error="Tick “My device really is at factory defaults” first, or choose another way.")
    return None


def _step_preview(ctx: TaskCtx, a: Answers) -> Step:
    session = ctx.session
    assert session is not None
    if (back := _need_source(ctx, a)) is not None:
        return back
    if not a.presets:
        return _step_presets(ctx, a, error="Choose at least one preset.")
    source = unstash(a.token)
    previews = []
    for p in a.presets:
        lines: list[str] = []
        problem = None
        try:
            doc, warnings = _doc_for(ctx, a, source, p)
        except Exception as exc:
            doc, warnings, problem = None, [], str(exc) or type(exc).__name__
        if doc is None and problem is None:
            problem = f"The file has nothing for preset {p + 1}."
        if doc is not None and a.how != SOFTWARE:
            summary = summarize(doc, session.profile, getattr(session, "channel_meta", {}), session.settings, {}, None)
            lines = [f"{o.name}: {o.sentence}" for o in summary.outputs]
        total = len(device_values(default_config_doc(session.profile), session.profile))
        covered = None
        if source is not None and source.script is not None:
            covered = len(session.parse_cli_script(source.script).device_paths)
        previews.append(
            {
                "n": p + 1,
                "lines": lines,
                "warnings": warnings,
                "problem": problem,
                "covered": covered,
                "total": total,
                "playing": p == ctx.playing,
            }
        )
    return Step(
        4,
        "Preview",
        "tasks/adopt/step_preview.html",
        _params(a),
        {"how": a.how, "previews": previews, "broken": any(x["problem"] for x in previews)},
    )


def _step_choice(ctx: TaskCtx, a: Answers, error: str | None = None) -> Step:
    session = ctx.session
    assert session is not None
    if (back := _need_source(ctx, a)) is not None:
        return back
    if not a.presets:
        return _step_presets(ctx, a, error="Choose at least one preset.")
    names = ", ".join(str(p + 1) for p in a.presets)
    return Step(
        5,
        "Remember or write?",
        "tasks/adopt/step_choice.html",
        _params(a),
        {
            "how": a.how,
            "choice": a.choice,
            "error": error,
            "preset_words": f"preset{'s' if len(a.presets) > 1 else ''} {names}",
            "mute_wrap": bool(getattr(session.settings, "mute_wrap_bulk_apply", True)),
            "warning": named_warning(getattr(session, "channel_meta", {})) if a.how == FACTORY else None,
            "defaults_summary": factory_defaults_summary(session.profile) if a.how == FACTORY else [],
        },
    )


def _step_done(
    ctx: TaskCtx, a: Answers, lines: Sequence[str] = (), check: str | None = None, error: str | None = None
) -> Step:
    session = ctx.session
    adoption = getattr(session, "adoption", None) if session is not None else None
    return Step(
        6,
        "Done",
        "tasks/adopt/step_done.html",
        {**_params(a), "done": "1"},
        {
            "lines": list(lines),
            "check": check,
            "error": error,
            "adoption": adoption,
            "home_href": f"{ctx.root}/",
            "device_href": f"{ctx.root}/device#verify",
        },
        applied=bool(lines),
        toast=lines[0] if lines else None,
    )


async def render(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    session = ctx.session
    n_presets = session.profile.n_presets if session is not None else 4
    a = answers(params, n_presets, ctx.playing)
    step = int_param(params, "step", 1) or 1
    if session is None or step <= 1:
        return _step_why(ctx, a)
    if step == 2:
        return _step_how(ctx, a)
    if step == 3:
        if (back := _need_source(ctx, a)) is not None:
            return back
        return _step_presets(ctx, a)
    if step == 4:
        return _step_preview(ctx, a)
    if step == 5 or params.get("done") != "1":
        return _step_choice(ctx, a)
    return _step_done(ctx, a)


# --- actions --------------------------------------------------------------------------------------------------------


async def _read_upload(upload: Any, limit: int) -> bytes | None:
    if not isinstance(upload, UploadFile) or not upload.filename:
        return None
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"the file is larger than {limit // 1000:,} kB")
    return data


async def _take_source(ctx: TaskCtx, a: Answers, params: Mapping[str, str]) -> Step:
    """Step 2's Next: check what was given and keep it for the next steps."""
    session = ctx.session
    assert session is not None
    form = await ctx.request.form()  # cached by Starlette: the shell read it already
    if a.how == BACKUP:
        try:
            data = await _read_upload(form.get("backup_file"), MAX_FILE_BYTES)
            if data is None:
                kept = unstash(a.token)
                if kept is not None and kept.how == BACKUP:
                    return _step_presets(ctx, a)
                return _step_how(ctx, a, error="Choose the backup file first.")
            docs, label = docs_from_json(
                json.loads(data.decode("utf-8-sig")), session.serial, session.profile.product_name
            )
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            reason = str(exc) if not isinstance(exc, json.JSONDecodeError) else "it isn't valid JSON"
            return _step_how(ctx, a, error=f"That file can't be used: {reason}.")
        filename = getattr(form.get("backup_file"), "filename", "") or "the file"
        a.token = stash(Source(BACKUP, f"{filename} ({label})", docs=docs))
        if None not in docs:
            in_file = [p for p in a.presets if p in docs]
            a.presets = in_file or sorted(k for k in docs if k is not None)[:1]
        return _step_presets(ctx, a)
    if a.how == SCRIPT:
        try:
            data = await _read_upload(form.get("script_file"), MAX_SCRIPT_BYTES)
        except ValueError as exc:
            return _step_how(ctx, a, error=f"That file can't be used: {exc}.")
        text = data.decode("utf-8-sig", errors="replace") if data is not None else (params.get("script") or "")
        if len(text.encode()) > MAX_SCRIPT_BYTES:
            return _step_how(ctx, a, error=f"The script is larger than {MAX_SCRIPT_BYTES // 1000} kB.")
        if not text.strip():
            return _step_how(ctx, a, error="Paste the commands or choose the file first.")
        parsed = session.parse_cli_script(text)
        if parsed.errors:
            lines = [f"line {e.line}: {e.message} ({e.text})" for e in parsed.errors[:10]]
            a.token = stash(Source(SCRIPT, "your script", script=text))
            return _step_how(ctx, a, error="Some lines can't be read. Fix them and try again.", errors=lines)
        a.token = stash(Source(SCRIPT, f"your script ({len(parsed.ops)} settings)", script=text))
        return _step_presets(ctx, a)
    if a.how == FACTORY and not a.factory_ok:
        return _step_how(ctx, a, error="Tick “My device really is at factory defaults” first, or choose another way.")
    return _step_presets(ctx, a)


def _result_line(result: Any) -> str:
    known = f"{result.known} setting{'s' if result.known != 1 else ''} known"
    if result.read_back:
        known += f" ({result.read_back} read from the device)"
    written = f"{result.writes} written"
    return f"Preset {result.preset + 1}: {known}, {result.unknown} unknown, {written}."


async def _check_device(ctx: TaskCtx) -> str:
    """Check device, as its button on Device & sync does; the result in words."""
    from app.sync import verify
    from app.sync.status import status_view

    session = ctx.session
    assert session is not None
    if verify.memory_map_for(session.profile.product_name) is None:
        return "Check device isn't available for this model."
    try:
        report = await verify.verify_device(session, source="ui")
    except (verify.VerifyError, PresetSwitchInProgress, StateUnavailable, DaemonError) as exc:
        return f"Check device couldn't run: {str(exc).rstrip('.') or type(exc).__name__}."
    view = status_view(session, report.preset, report)
    if view is not None and view.check is not None:
        return f"Check device (preset {report.preset + 1}): {view.check.result}."
    return f"Check device (preset {report.preset + 1}): done."


async def _apply(ctx: TaskCtx, a: Answers, params: Mapping[str, str]) -> Step:
    session = ctx.session
    assert session is not None
    if (back := _need_source(ctx, a)) is not None:
        return back
    if not a.presets:
        return _step_presets(ctx, a, error="Choose at least one preset.")
    mode = AdoptMode.UNKNOWN.value if a.how == SOFTWARE else a.choice
    push_ok = params.get("push_ok") in ("1", "on", "true")
    if mode == PUSH and not push_ok:
        return _step_choice(ctx, a, error="Tick the box first: Overwrite writes every setting to the device.")
    source = unstash(a.token)
    lines: list[str] = []
    for p in a.presets:
        kwargs: dict[str, Any] = {}
        if source is not None and source.script is not None:
            kwargs["script"] = source.script
        elif source is not None and a.how == BACKUP:
            raw = source.docs.get(p, source.docs.get(None))
            if raw is None:
                return _step_presets(ctx, a, error=f"The file has nothing for preset {p + 1}.")
            kwargs["doc"] = parse_doc(raw)
        try:
            result = await session.adopt(
                mode,
                preset=p,
                confirm=mode == PUSH and push_ok,
                # The factory-defaults guard: only with the box ticked on step 2 (the session refuses it otherwise).
                confirm_defaults=a.how == FACTORY and a.factory_ok,
                timeout=BULK_TIMEOUT_S,
                **kwargs,
            )
        except ConfirmationRequired as exc:
            return _step_choice(ctx, a, error=f"This needs your confirmation: {exc}.")
        except (DesiredValidationError, MasterValidationError, StateUnavailable, DaemonError) as exc:
            problems = getattr(exc, "problems", None) or [str(exc)]
            done = f" {' '.join(lines)}" if lines else ""
            return _step_choice(ctx, a, error=f"Nothing more was set up: {'; '.join(problems[:3])}.{done}")
        line = _result_line(result)
        if result.read_error:
            line += f" It couldn't be read back ({result.read_error}), so it was remembered without reading it."
        if result.flush is not None and not result.flush.ok:
            line += " Overwriting didn't finish; Device & sync shows what is left."
        if result.warnings:
            line += f" {len(result.warnings)} script line(s) were ignored."
        lines.append(line)
    ctx.forget()
    check = await _check_device(ctx)
    with contextlib.suppress(Exception):
        if a.token:
            _STASH.pop(a.token, None)
    return _step_done(ctx, a, lines, check)


async def act(ctx: TaskCtx, params: Mapping[str, str]) -> Step:
    """``op=source`` (step 2's Next: take the file or script) and ``op=apply`` (step 5: set it up)."""
    session = ctx.session
    assert session is not None
    a = answers(params, session.profile.n_presets, ctx.playing)
    op = params.get("op")
    if op == "source":
        return await _take_source(ctx, a, params)
    if op == "apply":
        return await _apply(ctx, a, params)
    return await render(ctx, params)


WIZARD = Wizard(
    id="adopt",
    label="Set up this device",
    steps=STEPS,
    render=render,
    act=act,
    intro="Tells the app what your miniDSP holds today. Nothing is written unless you choose Overwrite at the end.",
)

__all__ = ["WIZARD", "custom_names", "docs_from_json", "named_warning", "stash", "unstash"]
