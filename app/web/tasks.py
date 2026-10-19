"""Guided tasks (phase 9 design §14): the wizard shell every guided task shares.

A wizard is a few steps rendered on the server. The same step templates (``tasks/<id>/step_*.html``) render in two
places:

- inside the ``<dialog>`` of every page (``tasks/_dialog.html``; a full-screen sheet on phones), swapped in with HTMX
  from ``/ui/tasks/<id>``. static/js/tasks.js opens it from any link to ``/tasks/<id>`` and from a ``?task=<id>`` deep
  link, so a wizard opened from a card leaves you where you were; and
- as a normal page at ``/tasks/<id>?step=n&…`` (refresh, deep links and the browser's Back button work there).

Answers live in hidden form fields and the URL; nothing new is stored. Every step states its outcome in words and
nothing is sent until **Apply** (or a check's **Apply fix**). Writes go through the session's own edit and master
methods, the ones the editors' endpoints use, so the level limits and the big-step guard behave as everywhere else; a
big raise is confirmed inside the wizard with a checkbox and then sent with ``confirm``.

**Undo** is field-level (`UndoRecord`): a wizard records the before- and after-values of exactly the paths it wrote.
Undo writes the before-values back, except for paths that changed again since (another tab, Home Assistant, MQTT),
which it leaves alone and lists. It is offered for `UNDO_WINDOW_S` seconds, on the wizard's last step and in a toast.

The wizards themselves live in ``app/web/task_*.py`` and are registered in ``app/web/__init__.py``.
"""

import contextlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from app.api.deps import require_same_origin
from app.model.config_doc import PathError, get_path
from app.sync.session import (
    ConfirmationRequired,
    DesiredValidationError,
    DeviceSession,
    MasterValidationError,
    StateUnavailable,
    UnknownPreset,
)
from app.sync.summary import Levels, PresetSummary, live_levels, session_summary
from app.web import devices, shell
from app.web.params import patch_from_values
from app.web.routes import page_context
from app.web.templating import templates

# Writes refuse cross-site browser requests, as the editors' endpoints do (app/api/deps.py).
router = APIRouter(include_in_schema=False, dependencies=[Depends(require_same_origin)])

UNDO_WINDOW_S = 30.0  # how long Undo is offered (design §14.1)
UNDO_GRACE_S = 15.0  # a late click (a slow phone, a toast read to the end) still counts
TASK_TOPICS = ("master", "device", "sync", "desired", "verify")
EPS = 0.01


# --- undo -------------------------------------------------------------------------------------------------------------


@dataclass
class UndoRecord:
    """What a wizard wrote: preset paths and master fields, each with its value before and after the write."""

    preset: int
    edits: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # path -> (before, after)
    master: dict[str, tuple[Any, Any]] = field(default_factory=dict)  # "volume" / "mute" -> (before, after)
    at: float = field(default_factory=time.time)

    def __bool__(self) -> bool:
        return bool(self.edits or self.master)

    def merged(self, other: "UndoRecord") -> "UndoRecord":
        """Two writes of one wizard run as one record: the first before-value and the last after-value of each path."""
        edits = dict(self.edits)
        for path, (before, after) in other.edits.items():
            edits[path] = (edits[path][0], after) if path in edits else (before, after)
        master = dict(self.master)
        for name, (before, after) in other.master.items():
            master[name] = (master[name][0], after) if name in master else (before, after)
        return UndoRecord(self.preset, edits, master, max(self.at, other.at))

    def seconds_left(self, now: float | None = None) -> float:
        return max(0.0, UNDO_WINDOW_S - ((now if now is not None else time.time()) - self.at))

    def to_json(self) -> str:
        data = {
            "p": self.preset,
            "e": {k: list(v) for k, v in self.edits.items()},
            "m": {k: list(v) for k, v in self.master.items()},
            "t": round(self.at, 1),
        }
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str | None) -> "UndoRecord | None":
        """None for anything that isn't a record (a hand-edited URL, a truncated field)."""
        if not text:
            return None
        try:
            data = json.loads(text)
            preset = int(data["p"])
            edits = {str(k): (v[0], v[1]) for k, v in dict(data.get("e") or {}).items()}
            master = {str(k): (v[0], v[1]) for k, v in dict(data.get("m") or {}).items() if k in ("volume", "mute")}
            at = float(data.get("t", 0))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            return None
        if preset < 0 or not math.isfinite(at):
            return None
        return cls(preset, edits, master, at)


def same_value(a: Any, b: Any) -> bool:
    """Whether a setting still holds what the wizard wrote (numbers within 0.01; objects compared field by field)."""
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b
    if isinstance(a, int | float) and isinstance(b, int | float):
        return abs(float(a) - float(b)) < EPS
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(same_value(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(same_value(x, y) for x, y in zip(a, b, strict=True))
    return a == b


_MISSING = object()


def undo_plan(changes: Mapping[str, tuple[Any, Any]], current: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(what to write back, what to leave alone): a path is restored to its before-value only while it still holds
    the value the wizard wrote; one that changed since (elsewhere) is skipped."""
    restore: dict[str, Any] = {}
    skipped: list[str] = []
    for path, (before, after) in changes.items():
        now = current.get(path, _MISSING)
        if now is not _MISSING and same_value(now, after):
            if not same_value(now, before):
                restore[path] = before
        else:
            skipped.append(path)
    return restore, skipped


def values_of(session: DeviceSession, preset: int, paths: Sequence[str]) -> dict[str, Any]:
    """Desired values of ``paths`` as JSON-ready data; paths that don't exist are left out."""
    doc = session.desired(preset)
    out: dict[str, Any] = {}
    for path in paths:
        try:
            value = get_path(doc, path)
        except PathError:
            continue
        out[path] = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return out


def master_values(session: DeviceSession) -> dict[str, Any]:
    master = session.master
    return {"volume": master.volume if master else None, "mute": master.mute if master else None}


async def apply_edits(session: DeviceSession, preset: int, values: Mapping[str, Any], *, confirm: bool) -> UndoRecord:
    """One committed edit of ``values`` (path -> value) through the session, as the editors' endpoint makes it.
    Raises what `DeviceSession.edit` raises (`ConfirmationRequired` for a big raise without ``confirm``)."""
    paths = list(values)
    before = values_of(session, preset, paths)
    await session.edit(preset, patch_from_values(dict(values)), confirm=confirm, commit=True, source="ui")
    after = values_of(session, preset, paths)
    return UndoRecord(preset, {p: (before.get(p), after.get(p)) for p in paths if p in after})


async def apply_master(session: DeviceSession, preset: int, *, confirm: bool, **values: Any) -> UndoRecord:
    """Mute all / Volume through the session's master methods (Volume keeps its big-step guard)."""
    before = master_values(session)
    if "mute" in values:
        await session.set_mute(bool(values["mute"]), source="ui")
    if "volume" in values:
        await session.set_volume(float(values["volume"]), confirm=confirm, source="ui")
    after = master_values(session)
    return UndoRecord(preset, master={k: (before[k], after[k]) for k in values if k in after})


@dataclass(frozen=True)
class UndoResult:
    restored: list[str]  # plain names of what was put back
    skipped: list[str]  # plain names of what changed elsewhere and was left as is
    error: str | None = None

    @property
    def message(self) -> str:
        if self.error:
            return self.error
        parts = []
        if self.restored:
            parts.append("Undone: " + _join(self.restored) + (" is" if len(self.restored) == 1 else " are") + " back.")
        elif not self.skipped:
            parts.append("Nothing to undo: everything is already as it was.")
        if self.skipped:
            verb = "was" if len(self.skipped) == 1 else "were"
            parts.append(f"{_join(self.skipped)} {verb} changed elsewhere, so left as is.")
        return " ".join(parts)


def possessive(name: str) -> str:
    """ "Sub's", "Shakers'"."""
    return f"{name}'" if name.endswith(("s", "S")) else f"{name}'s"


def _join(items: Sequence[str]) -> str:
    items = list(items)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def describe(session: DeviceSession | None, path: str) -> str:
    from app.web.sync_ui import describe_path  # local: sync_ui builds on the editing pages

    if path == "volume":
        return "Volume"
    if path == "mute":
        return "Mute all"
    return describe_path(session, path)


async def undo(session: DeviceSession, record: UndoRecord, *, now: float | None = None) -> UndoResult:
    """Write the record's before-values back, field by field, skipping what changed since (design §14.1)."""
    now = time.time() if now is None else now
    if now - record.at > UNDO_WINDOW_S + UNDO_GRACE_S:
        return UndoResult([], [], "Undo is only offered for 30 seconds; change it back by hand if you still want to.")
    restored: list[str] = []
    skipped: list[str] = []
    try:
        if record.edits:
            current = values_of(session, record.preset, list(record.edits))
            restore, skip = undo_plan(record.edits, current)
            skipped += skip
            if restore:
                # Back to values the user had a moment ago, so the big-step guard doesn't ask again.
                await session.edit(record.preset, patch_from_values(restore), confirm=True, commit=True, source="ui")
                restored += list(restore)
        if record.master:
            restore, skip = undo_plan(record.master, master_values(session))
            skipped += skip
            if "volume" in restore and restore["volume"] is not None:
                await session.set_volume(float(restore["volume"]), confirm=True, source="ui")
                restored.append("volume")
            if "mute" in restore and restore["mute"] is not None:
                await session.set_mute(bool(restore["mute"]), source="ui")
                restored.append("mute")
    except (DesiredValidationError, MasterValidationError, StateUnavailable, UnknownPreset) as exc:
        return UndoResult([], [], f"Undo failed: {exc}")
    names = _dedupe([describe(session, p) for p in restored])
    return UndoResult(names, _dedupe([describe(session, p) for p in skipped]))


def _dedupe(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


def write_error(session: DeviceSession, exc: Exception) -> str:
    """A refused write in words (the level limits and the other checks speak in paths)."""
    if isinstance(exc, DesiredValidationError):
        problems = [describe_problem(session, p) for p in exc.problems] or [str(exc)]
        return "The device refused it: " + "; ".join(problems) + "."
    return f"It couldn't be sent: {exc}." if str(exc) else "It couldn't be sent."


def describe_problem(session: DeviceSession, problem: str) -> str:
    path, sep, reason = problem.partition(":")
    return f"{describe(session, path.strip())}:{reason}" if sep else problem


# --- the shell --------------------------------------------------------------------------------------------------------


@dataclass
class Step:
    """One rendered step: ``template`` inside the shell, its ``params`` (every answer so far; the hidden fields and the
    step's URL), and the flow diagram's blocks it changes (``highlight``: "o1:level", design §5.2)."""

    number: int
    title: str
    template: str
    params: dict[str, str] = field(default_factory=dict)
    ctx: dict[str, Any] = field(default_factory=dict)
    highlight: tuple[str, ...] = ()
    applied: bool = False  # this step follows a write: the page under the dialog refreshes
    toast: str | None = None  # after a write: shown with Undo once the dialog closes
    undo: UndoRecord | None = None


@dataclass
class TaskCtx:
    request: Request
    session: DeviceSession | None
    mode: str  # "dialog" or "page"
    serial: int | None = None
    _summary: PresetSummary | None = None

    @property
    def root(self) -> str:
        return self.request.scope.get("root_path", "").rstrip("/")

    @property
    def settings(self) -> Any:
        return getattr(self.session, "settings", None) or getattr(self.request.app.state, "settings", None)

    def task_href(self, task_id: str, **params: Any) -> str | None:
        """Root-prefixed link to another guided task (its wizard, else the page where that job is done today)."""
        href = shell.task_href(self.request, task_id)
        if href is None:
            return None
        query = {k: v for k, v in params.items() if v is not None}
        if href.startswith("/tasks/") and query:
            href += "?" + urlencode(query)
        return self.root + href

    @property
    def playing(self) -> int | None:
        master = self.session.master if self.session is not None else None
        preset = master.preset if master is not None else None
        if self.session is None or not isinstance(preset, int) or not 0 <= preset < self.session.profile.n_presets:
            return None
        return preset

    def summary(self, *, levels: bool = False) -> PresetSummary | None:
        """The playing preset's summary (with the live meters for ``levels``)."""
        if self.session is None:
            return None
        if levels:
            return session_summary(self.session, levels=self.levels())
        if self._summary is None:
            self._summary = session_summary(self.session)
        return self._summary

    def levels(self) -> Levels | None:
        return live_levels(self.session) if self.session is not None else None

    def forget(self) -> None:
        """Drop the cached summary after a write."""
        self._summary = None


Handler = Callable[[TaskCtx, Mapping[str, str]], Awaitable[Step]]


@dataclass(frozen=True)
class Wizard:
    id: str
    label: str
    steps: tuple[str, ...]  # short step names for the progress line ("Output · Amount · How · Check · Done")
    render: Handler  # GET: the step the params ask for
    act: Handler  # POST: apply, a check's fix, undo
    intro: str = ""  # one line on the Guided tasks page


WIZARDS: dict[str, Wizard] = {}


def register(wizard: Wizard) -> None:
    """Serve a wizard: its page ``/tasks/<id>`` (which also lights up its links, `shell.task_href`)."""
    WIZARDS[wizard.id] = wizard

    async def page(request: Request, serial: int | None = None) -> Response:
        return await _page(request, wizard, serial)

    page.__name__ = f"task_page_{wizard.id.replace('-', '_')}"
    router.add_api_route(f"/tasks/{wizard.id}", page, methods=["GET"], response_class=HTMLResponse)


def _params(request: Request, form: Mapping[str, Any] | None = None) -> dict[str, str]:
    data = dict(request.query_params)
    if form is not None:
        data.update({k: v for k, v in form.items() if isinstance(v, str)})
    return data


async def _ctx(request: Request, params: Mapping[str, str], serial: int | None) -> TaskCtx:
    session = devices.pick_session(request, serial)
    mode = "dialog" if params.get("mode") == "dialog" else "page"
    if session is not None and hasattr(session, "ensure_loaded"):
        with contextlib.suppress(Exception):  # the steps say so when the state isn't there
            await session.ensure_loaded()
    return TaskCtx(request, session, mode, serial)


def step_query(step: Step, serial: int | None) -> str:
    """The step's answers as a query string (the canonical ``/tasks/<id>?…`` and the ``?task=`` deep link)."""
    query = {"step": str(step.number), **{k: v for k, v in step.params.items() if v not in (None, "")}}
    if serial is not None:
        query["serial"] = str(serial)
    query.pop("mode", None)
    return urlencode(query)


def shell_context(ctx: TaskCtx, wizard: Wizard, step: Step) -> dict[str, Any]:
    query = step_query(step, ctx.serial)
    undo_left = step.undo.seconds_left() if step.undo else 0.0
    return {
        "w": wizard,
        "s": step,
        "mode": ctx.mode,
        "total": len(wizard.steps),
        "page_url": f"{ctx.root}/tasks/{wizard.id}",
        "fragment_url": f"{ctx.root}/ui/tasks/{wizard.id}",
        "step_url": f"{ctx.root}/tasks/{wizard.id}?{query}",
        "step_query": query,
        "serial_param": ctx.serial,
        "session": ctx.session,
        "undo_json": step.undo.to_json() if step.undo and undo_left > 0 else None,
        "undo_left": int(undo_left),
        "announce": f"{wizard.label}: step {step.number} of {len(wizard.steps)}, {step.title}",
        "task_href": ctx.task_href,
        **step.ctx,
    }


def _render_fragment(ctx: TaskCtx, wizard: Wizard, step: Step, status_code: int = 200) -> Response:
    data = shell_context(ctx, wizard, step)
    response = templates.TemplateResponse(ctx.request, "tasks/_step.html", data, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    if ctx.mode == "page":
        # The page's URL follows its step, so refresh resumes there and Back goes to the previous step.
        response.headers["HX-Push-Url"] = data["step_url"]
    return response


async def _page(request: Request, wizard: Wizard, serial: int | None) -> Response:
    params = _params(request)
    params.pop("mode", None)
    ctx = await _ctx(request, params, serial)
    step = await wizard.render(ctx, params)
    page = page_context(request, ctx.session, TASK_TOPICS)
    page.update(shell_context(ctx, wizard, step), title=wizard.label, tasks_page=True)
    response = templates.TemplateResponse(request, "tasks/page.html", page)
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/tasks", response_class=HTMLResponse, name="tasks_page")
async def tasks_index(request: Request, serial: int | None = None) -> Response:
    """Guided tasks (design §3.4 "Everyday"): every task with a line on what it does."""
    from app.web.home import tasks_context  # local: home builds on the routes module too

    session = devices.pick_session(request, serial)
    ctx = page_context(request, session, TASK_TOPICS)
    ctx.update(title="Guided tasks", **tasks_context(request))
    for entry in ctx["guided"]:
        wizard = WIZARDS.get(entry["id"])
        entry["intro"] = wizard.intro if wizard else ""
        if entry["href"].startswith("#"):
            entry["href"] = ctx["root"] + (shell.task_href(request, entry["id"]) or "/")
    response = templates.TemplateResponse(request, "tasks/index.html", ctx)
    response.headers["Cache-Control"] = "no-store"
    return response


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


class UndoBody(BaseModel):
    record: str


@router.post("/ui/tasks/undo")
async def undo_endpoint(request: Request, serial: int | None = None) -> Response:
    """The toast's Undo after a wizard closed: ``{"record": "<UndoRecord JSON>"}`` -> ``{"message", "restored",
    "skipped"}``."""
    if not _is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = devices.pick_session(request, serial)
    if session is None:
        return JSONResponse({"error": "unknown_device", "message": "No device."}, status_code=404)
    try:
        body = UndoBody.model_validate(await request.json())
    except Exception:
        return JSONResponse({"error": "bad_request", "message": "expected {record}"}, status_code=400)
    record = UndoRecord.from_json(body.record)
    if record is None:
        return JSONResponse({"error": "bad_request", "message": "Nothing to undo."}, status_code=400)
    result = await undo(session, record)
    status = 409 if result.error else 200
    content = {"message": result.message, "restored": result.restored, "skipped": result.skipped}
    return JSONResponse(content, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/ui/tasks/{task_id}", response_class=HTMLResponse)
async def task_fragment(request: Request, task_id: str, serial: int | None = None) -> Response:
    wizard = WIZARDS.get(task_id)
    if wizard is None:
        return PlainTextResponse("Unknown task", status_code=404)
    params = _params(request)
    ctx = await _ctx(request, params, serial)
    return _render_fragment(ctx, wizard, await wizard.render(ctx, params))


@router.post("/ui/tasks/{task_id}", response_class=HTMLResponse)
async def task_action(request: Request, task_id: str, serial: int | None = None) -> Response:
    """A step's write (Apply, a check's Apply fix, Undo). HTMX only, as every other write of the UI."""
    if not _is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    wizard = WIZARDS.get(task_id)
    if wizard is None:
        return PlainTextResponse("Unknown task", status_code=404)
    form = await request.form()
    params = _params(request, form)
    ctx = await _ctx(request, params, serial)
    if ctx.session is None:
        return _render_fragment(ctx, wizard, await wizard.render(ctx, params))
    step = await wizard.act(ctx, params)
    return _render_fragment(ctx, wizard, step)


def confirmed(params: Mapping[str, str]) -> bool:
    return params.get("confirm") in ("1", "true", "on", "yes")


def confirm_error(exc: ConfirmationRequired) -> str:
    return f"This needs your confirmation: {exc}."


def int_param(params: Mapping[str, str], name: str, default: int | None = None) -> int | None:
    raw = params.get(name)
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return default


def float_param(params: Mapping[str, str], name: str, default: float | None = None) -> float | None:
    raw = params.get(name)
    try:
        value = float(str(raw).replace("−", "-"))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


__all__ = [
    "UNDO_WINDOW_S",
    "WIZARDS",
    "Step",
    "TaskCtx",
    "UndoRecord",
    "UndoResult",
    "Wizard",
    "apply_edits",
    "apply_master",
    "possessive",
    "register",
    "router",
    "same_value",
    "undo",
    "undo_plan",
    "values_of",
]
