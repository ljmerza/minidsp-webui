"""What the Routing page shows besides its switches (phase 9 design §10.1 / §10.2, friction P18 and P21).

Every word comes from the summary engine (`app/sync/summary.py`), the same one Home's cards, the flow diagram and the
problems pill read, so the Total row can't disagree with them:

- each output's head carries its health badge ("⛔ Silent", "🔇 Muted", "✓ Ready");
- the Total row says what each output plays in the end ("+16.0 dB from In 1", with "route +4 + output +12" under it),
  "nothing" with a [Route In 1 → Sub] fix when no input feeds it, and "Muted" (with the level it would have) for a
  muted output;
- a note per input that feeds nothing;
- the rule text says what the app really enforces: route levels and the output level are each limited on their own
  by the output's level limit, not as a sum.

The page re-fetches this part (``/ui/devices/{serial}/presets/{preset}/routing/summary``) on ``desired`` / ``sync`` /
``verify`` events and swaps the pieces in place (out-of-band), so the switches the user is holding never move.
"""

from collections.abc import Sequence
from typing import Any

from app.sync.summary import OutputSummary, PresetSummary, join_words, signed
from app.web import flow


def _sources(o: OutputSummary) -> str:
    """ "from In 1" · "from In 1 and In 2"; empty when nothing feeds the output."""
    names = [r.name for r in o.total.routes]
    return f"from {join_words(names)}" if names else ""


def output_total(o: OutputSummary, in_names: Sequence[str], out_names: Sequence[str]) -> dict[str, Any]:
    """One output's head badge and Total cell."""
    total = o.total
    fixes: list[flow.Fix] = []
    for problem in o.problems:
        if problem.code == "no_route":
            fixes = [fx for fx in flow.fixes(problem, "", in_names, out_names) if fx.values]
            break
    if not total.routes:
        state = "silent"
        text = "nothing"
        detail = f"No input is routed to {o.name}, so it is silent."
    elif o.muted:
        state = "muted"
        text = "Muted"
        detail = f"Would be {total.text} {_sources(o)} ({total.breakdown})."
    else:
        state = "on"
        text = total.text
        detail = total.breakdown
    return {
        "index": o.index,
        "name": o.name,
        "badge": o.badge,
        "badge_glyph": o.badge_glyph,
        "badge_word": o.badge_word,
        "state": state,
        "text": text,
        "sources": _sources(o) if state == "on" else "",
        "detail": detail,
        "fixes": fixes,
        # Why a high total is a caution: "Peaks above about −16 dBFS on In 1 will overload Shakers."
        "headroom": o.headroom.text if state == "on" and o.headroom.level in ("caution", "problem") else "",
        "headroom_level": o.headroom.level,
        "routed": bool(total.routes),
        # The phone folds the route switches of muted and not-in-use outputs (design §10.2); the head and Total stay.
        "fold": o.badge in ("muted", "not_in_use"),
    }


def input_notes(summary: PresetSummary) -> list[dict[str, Any]]:
    """ "In 2 isn't sent to any output." for every input that feeds nothing (not for inputs marked not in use)."""
    notes = []
    for i in summary.inputs:
        if i.sends_to or i.unused:
            continue
        notes.append({"index": i.index, "name": i.name, "text": f"{i.name} isn't sent to any output."})
    return notes


def rule_text(ceilings: Sequence[float], out_names: Sequence[str]) -> str:
    """What the app enforces for route plus output level (P21): each is capped on its own, so totals add up."""
    if not ceilings:
        return ""
    if len(set(ceilings)) == 1:
        c = ceilings[0]
        return (
            f"Each route level and each output level is limited on its own to the output's level limit "
            f"({signed(c)} dB), not as a sum: route {signed(c)} + output {signed(c)} gives a total of up to "
            f"{signed(2 * c)} dB, plus the input level."
        )
    limits = join_words([f"{name} {signed(c)} dB" for name, c in zip(out_names, ceilings, strict=False)])
    return (
        "Each route level and each output level is limited on its own to that output's level limit "
        f"({limits}), not as a sum, so a total can reach twice the limit, plus the input level."
    )


def routing_context(
    summary: PresetSummary | None, ceilings: Sequence[float], in_names: Sequence[str], out_names: Sequence[str]
) -> dict[str, Any]:
    """The Routing page's summary parts: ``route_outputs`` (per output), ``route_notes`` and ``route_rule``."""
    outputs = [output_total(o, in_names, out_names) for o in summary.outputs] if summary is not None else []
    return {
        "route_outputs": outputs,
        "route_notes": input_notes(summary) if summary is not None else [],
        "route_rule": rule_text(ceilings, out_names),
    }


__all__ = ["input_notes", "output_total", "routing_context", "rule_text"]
