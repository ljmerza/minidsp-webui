"""The output and input pages' header block and chain tab bar (phase 9 design §8, §9).

The header block holds what the Home card says about the channel (name, badge, sentence, what feeds it, the headroom
line, the meter); under it, **the tab bar is the chain**: one chip per setting, each showing its current value
("Band 20–80 Hz", "Delay 8.75 ms"), and the selected chip's editor renders below. Every word comes from the summary
engine (`app/sync/summary.py`) through the flow diagram's blocks (`app/web/flow.py`), so the chips can't disagree with
Home or the diagram.

``static/js/chain.js`` swaps the panel in place when a chip is chosen (no reload, the URL follows); without JavaScript
the chips are plain links to ``?tab=<key>``.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.sync.summary import (
    ARROW,
    InputSummary,
    OutputSummary,
    PresetSummary,
    analyse_band,
    analyse_eq,
    eq_label,
    join_words,
    session_summary,
    signed,
    signed1,
)
from app.web import flow

# A low-pass at or below this makes the graphs open at 5–300 Hz (design §8.2), as does the Shakers (tactile) flag.
BASS_VIEW_HZ = 300.0
RAISE_STEP_DB = 1.0  # the Level tab's "Raise route In 1 → Shakers to +5 dB" button (design §8.2)

# Diagram block -> output page tab (the tab keys are the ``?tab=`` values, unchanged since phase 3).
BLOCK_TAB = {
    "level": "level",
    "band": "crossover",
    "eq": "peq",
    "delay": "delay",
    "polarity": "polarity",
    "compressor": "compressor",
    "fir": "fir",
}
TAB_NAMES = {
    "level": "Level",
    "crossover": "Band (crossover)",
    "peq": "EQ",
    "delay": "Delay",
    "polarity": "Polarity",
    "compressor": "Compressor (limiter)",
    "fir": "FIR",
    "response": "Response",
}
STATE_WORDS = {"caution": "check", "problem": "problem"}


@dataclass(frozen=True)
class Chip:
    key: str  # the tab key
    label: str  # what the chip shows: "Band 20–80 Hz"
    state: str  # on | default | caution | problem
    glyph: str  # ⛔ / ⚠ for problem / caution
    sync: str | None  # an unsent status (failed / changed / pending / waiting / unknown), None when sent
    sync_glyph: str
    sync_word: str
    href: str
    enabled: bool = True
    spoken: str = ""  # extra words for screen readers after the label ("check", "sending")


def _chip_label(key: str, label: str) -> str:
    """The diagram's block words, a little longer where the chip has room: "Delay 8.75 ms", "Compressor off"."""
    if key == "delay":
        return f"{label} ms"
    if key == "compressor":
        return "Compressor " + label.removeprefix("Comp.").strip()
    return label


def _spoken(state: str, sync_word: str) -> str:
    words = [w for w in (STATE_WORDS.get(state, ""), sync_word.lower()) if w]
    return ", ".join(words)


def output_chips(
    out: OutputSummary, tabs: Sequence[dict[str, Any]], link: Any, *, staged: bool | None = None
) -> list[Chip]:
    """One chip per output page tab, in the design's chain order, with the summary's value words.

    ``tabs``: `app.web.params.output_tabs` (key, label, enabled); ``link``: tab key -> URL."""
    blocks = {b.key: b for b in flow.output_blocks(out, staged=staged)}
    by_tab = {BLOCK_TAB[k]: b for k, b in blocks.items() if k in BLOCK_TAB}
    chips = []
    for tab in tabs:
        key = tab["key"]
        block = by_tab.get(key)
        if block is not None:
            chips.append(
                Chip(
                    key=key,
                    label=_chip_label(block.key, block.label),
                    state=block.state,
                    glyph=block.glyph,
                    sync=block.sync,
                    sync_glyph=block.sync_glyph,
                    sync_word=block.sync_word,
                    href=link(key),
                    enabled=tab["enabled"],
                    spoken=_spoken(block.state, block.sync_word),
                )
            )
            continue
        label = tab["label"]
        if key == "fir":
            label = "FIR off"  # a loaded FIR has its own block ("FIR on")
        chips.append(Chip(key, label, "default", "", None, "", "", link(key), tab["enabled"]))
    return chips


def input_chips(i: InputSummary, eq_text: str, link: Any) -> list[Chip]:
    """An input page's chain (design §9): Level · EQ."""
    level = f"Level {signed(i.level_db)}" + (" · muted" if i.muted else "")
    return [
        Chip("level", level, "on" if i.muted else "default", "", None, "", "", link("level")),
        Chip("peq", eq_text, "default" if eq_text == "EQ none" else "on", "", None, "", "", link("peq")),
    ]


def fed_by(out: OutputSummary) -> str:
    """ "Fed by In 1 at +4 dB", "Fed by In 1 at +4 dB and In 2 at 0 dB", "Fed by nothing"."""
    routes = out.total.routes
    if not routes:
        return "Fed by nothing"
    parts = [f"{r.name} at {signed(r.route_db)} dB" + (" (muted)" if r.muted else "") for r in routes]
    return "Fed by " + join_words(parts)


def badge_reason(out: OutputSummary) -> str | None:
    """The line under the badge (as on Home's card): why it is Silent, Overload or Check; left out when the headroom
    line says the same thing more concretely."""
    if not out.badge_reason or out.badge not in ("silent", "check", "overload"):
        return None
    problem = next((p for p in out.problems if p.text == out.badge_reason), None)
    if problem is not None and problem.code == "high_total" and out.headroom.level != "none":
        return None
    if out.badge_reason == out.headroom.text:
        return None
    return out.badge_reason


def is_bass(doc_output: Any, tactile: bool) -> bool:
    """Whether this output's graphs open at 5–300 Hz (design §8.2): marked Shakers (tactile), or a low-pass at or
    below 300 Hz."""
    if tactile:
        return True
    band = analyse_band(doc_output)
    return band.lp is not None and band.lp[0] <= BASS_VIEW_HZ


@dataclass(frozen=True)
class LevelView:
    """The Level tab's words and actions (design §8.2, the §4.3 rule)."""

    at_limit: bool
    raise_values: dict[str, float] = field(default_factory=dict)  # one press of "Raise route …" (empty: none left)
    raise_label: str = ""
    max_note: str = ""
    totals: tuple[str, ...] = ()
    no_route: str = ""


def level_view(out: OutputSummary, gain_min: float) -> LevelView:
    from app.web.home import level_step  # local: app.web.home imports the routes module

    total = out.total
    totals = tuple(
        f"Total from {r.name}: {r.name} level {signed(r.input_db)} + route {signed(r.route_db)} + output "
        f"{signed(total.output)} = {signed1(r.total_db)} dB"
        for r in total.routes
    )
    no_route = "" if total.routes else f"Nothing is routed to {out.name}, so it plays nothing."
    if not total.at_limit or not total.routes:
        return LevelView(False, totals=totals, no_route=no_route)
    step = level_step(total, out.index, +1, gain_min=gain_min, step=RAISE_STEP_DB)
    if step.target != "routes":
        return LevelView(
            True,
            max_note=f"{out.name} is at its maximum: the output and every route into it are at the "
            f"{signed(total.ceiling)} dB limit.",
            totals=totals,
        )
    raised = [r for r in total.routes if f"inputs.{r.input}.routing.{out.index}.gain_db" in step.values]
    if len(raised) == 1:
        r = raised[0]
        value = step.values[f"inputs.{r.input}.routing.{out.index}.gain_db"]
        label = f"Raise route {r.name} {ARROW} {out.name} to {signed(value)} dB"
    else:
        label = f"Raise the routes into {out.name} by {signed(RAISE_STEP_DB)} dB"
    return LevelView(True, raise_values=dict(step.values), raise_label=label, totals=totals)


def edited_summary(session: Any, preset: int) -> PresetSummary | None:
    """The summary of the preset being edited (not necessarily the playing one); None while the state isn't loaded."""
    try:
        return session_summary(session, preset)
    except Exception:  # the page must render even if a summary rule trips over odd state
        return None


def input_eq_label(session: Any, preset: int, index: int) -> str:
    return eq_label(analyse_eq(session.desired(preset).inputs[index].peq))


__all__ = [
    "BASS_VIEW_HZ",
    "TAB_NAMES",
    "Chip",
    "LevelView",
    "badge_reason",
    "edited_summary",
    "fed_by",
    "input_chips",
    "input_eq_label",
    "is_bass",
    "level_view",
    "output_chips",
]
