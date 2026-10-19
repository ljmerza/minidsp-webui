"""One status model for what the device holds (phase 9 design §11.1, PR 7).

Every place that says whether the device has the app's settings (the header status word, the Device & sync page and
its table, the summary engine's sync facts) reads it from here, so they can't disagree.

A setting's status is the writer's state overlaid with the last device check:

- the writer says **Sent** (the app sent it and the device accepted it), **Sending** ⟳ (a write is due; on a preset that
  isn't playing it is **Waiting for preset N** ⏸ instead), **Failed** ⚠ or **Unknown** ? (never sent or read);
- the last check turns a Sent setting into **Changed on device** ≠ when the device reported something else. It stays ≠
  until the app sends it again, the device's value is used, or the value is forgotten (it becomes Unknown).

**Can't check** ⊘ is only a property of a check result (routing, on/off switches and so on can't be read back); it is
never a status of a setting. Nothing here claims the device *matches*: "✓ All sent" means sent and accepted.

`status_view` is the session helper; `path_states`, `open_changes`, `headline` and `check_summary` are pure.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SENT = "sent"
SENDING = "sending"
WAITING = "waiting"
FAILED = "failed"
UNKNOWN = "unknown"
CHANGED = "changed"
CANT_CHECK = "cant_check"

# Headline priority, most important first (design §3.1): Failed > Changed on device > Sending > Waiting > Unknown >
# All sent.
ORDER = (FAILED, CHANGED, SENDING, WAITING, UNKNOWN, SENT)

GLYPHS = {
    SENT: "✓",
    SENDING: "⟳",
    WAITING: "⏸",
    FAILED: "⚠",
    UNKNOWN: "?",
    CHANGED: "≠",
    CANT_CHECK: "⊘",
}
WORDS = {
    SENT: "Sent",
    SENDING: "Sending",
    WAITING: "Waiting for preset {n}",
    FAILED: "Failed",
    UNKNOWN: "Unknown",
    CHANGED: "Changed on device",
    CANT_CHECK: "Can't check",
}
# The glossary entry (app/web/glossary.py) of each status.
TERMS = {
    SENT: "st-sent",
    SENDING: "st-sending",
    WAITING: "st-waiting",
    FAILED: "st-failed",
    UNKNOWN: "st-unknown",
    CHANGED: "st-changed",
    CANT_CHECK: "st-cant-check",
}
# CSS modifier of each status (``sync-<css>``): the existing classes keep their internal names.
CSS = {
    SENT: "synced",
    SENDING: "pending",
    WAITING: "waiting",
    FAILED: "failed",
    UNKNOWN: "unknown",
    CHANGED: "changed",
    CANT_CHECK: "cant-check",
}
# What the device can't report back, in words (verify.py's UNVERIFIABLE_* reasons, grouped).
CANT_CHECK_WHAT = "routing, on/off switches, compressor timing and FIR"


def word(state: str, preset: int | None = None) -> str:
    """The on-screen word of a status ("Waiting for preset 2")."""
    text = WORDS[state]
    return text.format(n=preset + 1 if preset is not None else "N")


def _plural(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


# --- pure helpers -----------------------------------------------------------------------------------------------------


def path_states(writer: Mapping[str, str], *, waiting: bool = False, changed: Collection[str] = ()) -> dict[str, str]:
    """Each path's status from the writer's (synced / pending / failed / unknown), with ``changed`` overlaid on the
    sent ones. ``waiting``: the preset isn't playing, so its pending writes wait instead of being sent."""
    out: dict[str, str] = {}
    for path, status in writer.items():
        if status == "failed":
            out[path] = FAILED
        elif status == "pending":
            out[path] = WAITING if waiting else SENDING
        elif status == "unknown":
            out[path] = UNKNOWN
        else:
            out[path] = CHANGED if path in changed else SENT
    return out


def _report_dict(report: Any) -> Mapping[str, Any] | None:
    if report is None:
        return None
    if isinstance(report, Mapping):
        return report
    return {
        "preset": getattr(report, "preset", None),
        "paths": [i.path for i in getattr(report, "drifted", ())],
        "accepted": getattr(report, "accepted", ()),
        "marked_unknown": getattr(report, "marked_unknown", ()),
        "writes_at": getattr(report, "writes_at", None),
    }


def _covered(path: str, settled: Collection[str]) -> bool:
    return any(path == s or path.startswith(s + ".") for s in settled)


def open_changes(report: Any, preset: int | None, sent_seq: Mapping[str, int] | None = None) -> set[str]:
    """The paths the last check found changed on the device that are still open: not since used (accepted), forgotten
    (marked unknown) or sent again (``sent_seq[path]`` above the report's ``writes_at``).

    ``report`` is a `VerifyReport`, a dict with its ``preset``, ``paths`` (drifted), ``accepted``, ``marked_unknown``
    and ``writes_at``, or None. A report of another preset has nothing open for ``preset``."""
    data = _report_dict(report)
    if not data:
        return set()
    report_preset = data.get("preset")
    if preset is not None and report_preset is not None and report_preset != preset:
        return set()
    settled = set(data.get("accepted") or ()) | set(data.get("marked_unknown") or ())
    writes_at = data.get("writes_at")
    sent = sent_seq or {}
    out = set()
    for path in data.get("paths") or ():
        if _covered(path, settled):
            continue
        if writes_at is not None and sent.get(path, -1) > writes_at:
            continue
        out.add(path)
    return out


@dataclass(frozen=True)
class Progress:
    """A batch of writes in flight: ``done`` of ``total`` sent, and whether outputs are held muted meanwhile."""

    done: int
    total: int
    outputs_muted: bool = False


def counts_of(states: Mapping[str, str]) -> dict[str, int]:
    counts = dict.fromkeys(ORDER, 0)
    for state in states.values():
        counts[state] = counts.get(state, 0) + 1
    return counts


def headline(counts: Mapping[str, int], *, preset: int, progress: Progress | None = None) -> tuple[str, str]:
    """``(state, text)``: the one status that matters most and its words, e.g. ``("failed", "2 failed")``,
    ``("changed", "3 changed on device")``, ``("sending", "Sending 120 of 184 · outputs muted")``,
    ``("waiting", "216 waiting for preset 2")``, ``("unknown", "12 unknown")`` or ``("sent", "All sent")``."""
    for state in ORDER:
        n = counts.get(state, 0)
        if not n or state == SENT:
            continue
        if state == FAILED:
            return state, f"{n} failed"
        if state == CHANGED:
            return state, f"{n} changed on device"
        if state == SENDING:
            total = max(progress.total if progress else 0, n)
            text = f"Sending {total - n} of {total}" if total > 1 else "Sending 1 setting"
            if progress is not None and progress.outputs_muted:
                text += " · outputs muted"
            return state, text
        if state == WAITING:
            return state, f"{n} waiting for preset {preset + 1}"
        return state, f"{n} unknown"
    return SENT, "All sent"


@dataclass(frozen=True)
class CheckSummary:
    """The last device check of a preset, in words."""

    preset: int
    checked_at: str  # ISO, UTC
    matched: int  # readable settings that hold what the app sent
    changed: int  # still-open "changed on device" settings
    differ: int  # unknown settings whose device value differs from the app's
    cant_check: int
    source: str = "ui"
    found: int = 0  # settings the check found changed on the device (open or not); used ones read as matching

    @property
    def time(self) -> str:
        """``HH:MM`` UTC of the check (the event log uses UTC too)."""
        try:
            return datetime.fromisoformat(self.checked_at).strftime("%H:%M")
        except ValueError:
            return self.checked_at

    @property
    def result(self) -> str:
        """ "92 changed on device; 100 can't be checked", "everything the device can report matches (60 settings);
        100 can't be checked" and so on. It never says "in sync": routing and switches can't be read back."""
        if self.changed:
            head = f"{self.changed} changed on device"
        elif self.found:
            # Not "matches": the device wasn't read again after they were sent or forgotten.
            head = f"{self.found} changed on device, all sent or forgotten since"
        elif self.differ:
            verb = "differs" if self.differ == 1 else "differ"
            head = f"nothing changed on device; {_plural(self.differ, 'unknown setting')} {verb} from the app's values"
        elif self.matched:
            head = f"everything the device can report matches ({_plural(self.matched, 'setting')})"
        else:
            head = "nothing to compare: no setting has been sent yet"
        if self.cant_check:
            head += f"; {self.cant_check} can't be checked"
        return head

    @property
    def cant_check_text(self) -> str:
        """ "100 settings can't be checked: routing, …" (empty when every setting could be read)."""
        if not self.cant_check:
            return ""
        return f"{_plural(self.cant_check, 'setting')} can't be checked: {CANT_CHECK_WHAT}."


def check_summary(report: Any, preset: int | None, changed: Collection[str]) -> CheckSummary | None:
    """The `CheckSummary` of ``report`` (a `VerifyReport`) for ``preset``; None without one, or for another preset.
    ``changed`` are its open changes (`open_changes`)."""
    if report is None or not hasattr(report, "counts"):
        return None
    if preset is not None and report.preset != preset:
        return None
    counts = report.counts
    return CheckSummary(
        preset=report.preset,
        checked_at=report.checked_at,
        matched=counts.get("ok", 0),
        changed=len(changed),
        differ=len(report.mismatched),
        cant_check=counts.get("unverifiable", 0),
        source=report.source,
        found=len(report.drifted),
    )


# --- the view ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusView:
    """What the device holds of one preset, by the app's account (see the module docstring)."""

    preset: int
    playing: bool  # the device's playing preset
    states: dict[str, str]  # path -> sent / sending / waiting / failed / unknown / changed
    counts: dict[str, int]
    state: str  # the headline status
    text: str  # the headline words, without the glyph
    progress: Progress | None = None
    changed: frozenset[str] = frozenset()
    reasons: dict[str, str] = field(default_factory=dict)  # failed path -> why
    error: str | None = None  # the writer's last error
    check: CheckSummary | None = None

    @property
    def glyph(self) -> str:
        return GLYPHS[self.state]

    @property
    def css(self) -> str:
        return CSS[self.state]

    @property
    def total(self) -> int:
        return len(self.states)

    @property
    def problems(self) -> int:
        """Settings that aren't simply sent."""
        return self.total - self.counts.get(SENT, 0)

    def as_json(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "playing": self.playing,
            "state": self.state,
            "headline": f"{self.glyph} {self.text}",
            "counts": self.counts,
            "progress": None
            if self.progress is None
            else {
                "done": self.progress.done,
                "total": self.progress.total,
                "outputs_muted": self.progress.outputs_muted,
            },
            "changed": sorted(self.changed),
        }


_LAST = object()


def status_view(session: Any, preset: int | None = None, last_verify: Any = _LAST) -> StatusView | None:
    """The `StatusView` of ``preset`` (default: the playing one) of a `DeviceSession`; None until its state is loaded.
    ``last_verify`` defaults to the session's last device check."""
    if session is None or not getattr(session, "loaded", False):
        return None
    master = getattr(session, "master", None)
    playing = master.preset if master is not None else None
    if preset is None:
        preset = playing if isinstance(playing, int) else 0
    if not 0 <= preset < session.profile.n_presets:
        return None
    from app.sync import verify  # local imports: both import the session module, which doesn't import this one
    from app.sync.session import StateUnavailable

    if last_verify is _LAST:
        last_verify = verify.last_report(session.serial)
    try:
        writer_states = session.sync_status(preset)
        summary = session.sync_summary(preset)
    except StateUnavailable:
        return None
    state = session.writer.state(preset)
    is_playing = playing == preset
    changed = open_changes(last_verify, preset, state.sent_seq)
    # Only a sent setting can be "changed on device"; one the app doesn't know (forgotten after a reconnect, say) is
    # Unknown.
    changed = {p for p in changed if writer_states.get(p) == "synced"}
    waiting = bool(summary.get("staged")) or not is_playing
    states = path_states(writer_states, waiting=waiting, changed=changed)
    counts = counts_of(states)
    progress = None
    if counts[SENDING]:
        total = max(state.batch_total, counts[SENDING])
        progress = Progress(total - counts[SENDING], total, bool(state.wrap))
    head_state, text = headline(counts, preset=preset, progress=progress)
    return StatusView(
        preset=preset,
        playing=is_playing,
        states=states,
        counts=counts,
        state=head_state,
        text=text,
        progress=progress,
        changed=frozenset(changed),
        reasons=dict(summary.get("failed") or {}),
        error=summary.get("error"),
        check=check_summary(last_verify, preset, changed),
    )


# The editing pages' per-setting badges, the sync bar and the /meters cards keep the writer's keys (synced / pending /
# failed / unknown, as in the API and the CSS classes) plus the overlay's "changed" and "waiting".
BADGE_KEYS = {
    SENT: "synced",
    SENDING: "pending",
    WAITING: "waiting",
    FAILED: "failed",
    UNKNOWN: "unknown",
    CHANGED: "changed",
}


def badge_statuses(session: Any, preset: int, view: StatusView | None = None) -> dict[str, str]:
    """Each path's status for the editing pages' badges, from `status_view`: the writer's status with "changed" (≠,
    still open from the last check) and "waiting" (⏸, a preset that isn't playing) overlaid. Falls back to the writer's
    own statuses when there is no view (raises `StateUnavailable` as `DeviceSession.sync_status` does)."""
    if view is None:
        view = status_view(session, preset)
    if view is None or view.preset != preset:
        return dict(session.sync_status(preset))
    return {path: BADGE_KEYS[state] for path, state in view.states.items()}


def changed_paths(session: Any, preset: int) -> set[str]:
    """The open "changed on device" paths of ``preset`` (`status_view`'s overlay); empty when unknown."""
    view = status_view(session, preset)
    return set(view.changed) if view is not None else set()


__all__ = [
    "BADGE_KEYS",
    "CANT_CHECK",
    "CHANGED",
    "FAILED",
    "GLYPHS",
    "ORDER",
    "SENDING",
    "SENT",
    "UNKNOWN",
    "WAITING",
    "WORDS",
    "CheckSummary",
    "Progress",
    "StatusView",
    "badge_statuses",
    "changed_paths",
    "check_summary",
    "counts_of",
    "headline",
    "open_changes",
    "path_states",
    "status_view",
    "word",
]
