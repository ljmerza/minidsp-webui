"""The global shell's header parts (phase 9 design §3): the problems pill, the "Help me…" menu and the phone's short
status word.

Everything here is read-only and pure apart from asking the router which pages exist; the header template
(``partials/header.html``) renders it on every page and on every header refresh.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from starlette.routing import Match

from app.sync.summary import CAUTION, INFO, PROBLEM, PresetSummary, Problem, session_summary
from app.web.flow import Fix, fixes


@dataclass(frozen=True, slots=True)
class GuidedTask:
    id: str  # the /tasks/<id> page and the ?task= deep link (design §14.1)
    label: str
    fallback: str  # where the job is done today, until the wizard exists


# The seven guided tasks (design §14), in the order of the Help me… menu. Until a wizard's page is served (PR 11-13),
# its entry opens the page where that job is done today.
TASKS: tuple[GuidedTask, ...] = (
    GuidedTask("diagnose", "Something wrong?", "/"),
    GuidedTask("louder", "Make one louder", "/outputs"),
    GuidedTask("eq-cut", "Fix a boom", "/outputs/1?tab=peq"),
    GuidedTask("line-up", "Line up shakers", "/outputs/1?tab=delay"),
    GuidedTask("set-levels", "Set levels", "/meters"),
    GuidedTask("profiles", "Save or load a setup", "/profiles"),
    GuidedTask("adopt", "Set up this device", "/device#adopt"),
)
TASK_BY_ID = {t.id: t for t in TASKS}


def _served(request: Request, path: str) -> bool:
    scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in request.app.routes)


def task_href(request: Request, task_id: str) -> str | None:
    """App-relative link of a guided task: its wizard page once served, else today's page for that job."""
    task = TASK_BY_ID.get(task_id)
    if task is None:
        return None
    path = f"/tasks/{task.id}"
    return path if _served(request, path) else task.fallback


def help_menu(request: Request) -> list[dict[str, str]]:
    """The header's Help me… entries (design §3.1): the seven guided tasks, then the Glossary (app-relative links)."""
    entries = [{"label": t.label, "href": task_href(request, t.id) or t.fallback} for t in TASKS]
    entries.append({"label": "Glossary", "href": "/help"})
    return entries


def meters_href(request: Request) -> str:
    """Where the header's CLIP light points: the meters (``/meters`` once Home takes ``/``, PR 9)."""
    return "/meters" if _served(request, "/meters") else "/#levels-title"


# --- problems pill (design §3.3) -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PillItem:
    level: str  # problem | caution | info
    glyph: str
    text: str
    fixes: tuple[Fix, ...]


GLYPH = {PROBLEM: "⛔", CAUTION: "⚠", INFO: "ⓘ"}


@dataclass(frozen=True, slots=True)
class ProblemsView:
    preset: int
    count: int  # problems and cautions; info notes aren't counted
    items: tuple[PillItem, ...]  # problems, then cautions
    notes: tuple[PillItem, ...]  # info notes ("Notes (not problems)")
    announce: str  # what the polite live region says when this changes

    @property
    def label(self) -> str:
        if not self.count:
            return "No problems"
        return f"{self.count} problem{'s' if self.count != 1 else ''}"


def _item(problem: Problem, request: Request, root: str, in_names: Sequence[str], out_names: Sequence[str]) -> PillItem:
    found = list(fixes(problem, root, in_names, out_names))
    for action in problem.actions:
        if action.task and not action.href:
            href = task_href(request, action.task)
            if href is not None:
                found.append(Fix(action.id, action.label, href=root + href))
    return PillItem(problem.level, GLYPH.get(problem.level, "ⓘ"), problem.text, tuple(found))


def problems_view(request: Request, session: Any, summary: PresetSummary | None = None) -> ProblemsView | None:
    """The header's problems pill for the playing preset; None until the device state is loaded."""
    if summary is None:
        summary = session_summary(session) if session is not None else None
    if summary is None or summary.preset is None:
        return None
    root = request.scope.get("root_path", "").rstrip("/")
    in_names = [i.name for i in summary.inputs]
    out_names = [o.name for o in summary.outputs]
    items = tuple(_item(p, request, root, in_names, out_names) for p in summary.problems if p.level != INFO)
    notes = tuple(_item(p, request, root, in_names, out_names) for p in summary.problems if p.level == INFO)
    if items:
        lead = "; ".join(i.text.rstrip(".") for i in items[:3])
        more = f"; and {len(items) - 3} more" if len(items) > 3 else ""
        announce = f"{len(items)} problem{'s' if len(items) != 1 else ''}: {lead}{more}."
    else:
        announce = "No problems."
    return ProblemsView(summary.preset, len(items), items, notes, announce)


# --- the phone's status word (design §3.2) ---------------------------------------------------------------------------


def short_status(view: Any) -> str:
    """The status headline in a few characters for the 48 px phone header ("Sent", "3 changed", "120/184")."""
    if view is None:
        return ""
    counts = getattr(view, "counts", {}) or {}
    progress = getattr(view, "progress", None)
    state = getattr(view, "state", "")
    if state == "failed":
        return f"{counts.get('failed', 0)} failed"
    if state == "changed":
        return f"{counts.get('changed', 0)} changed"
    if state == "sending":
        if progress is not None and getattr(progress, "total", 0):
            return f"{progress.done}/{progress.total}"
        return "Sending"
    if state == "waiting":
        return f"{counts.get('waiting', 0)} waiting"
    if state == "unknown":
        return f"{counts.get('unknown', 0)} unknown"
    return "Sent"


__all__ = [
    "TASKS",
    "GuidedTask",
    "PillItem",
    "ProblemsView",
    "help_menu",
    "meters_href",
    "problems_view",
    "short_status",
    "task_href",
]
