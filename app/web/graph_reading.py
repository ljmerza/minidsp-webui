"""The one-line reading under a response graph (phase 9 design §12.3 and §16 a11y 9): what each plotted output does,
in words, so the graph has a text equivalent.

    Shakers: 20–80 Hz (steep), 1 EQ cut at 40 Hz. Sub: silent here (In 1 isn't routed to it).

The band and EQ words come from the summary engine (`app/sync/summary.py`), so the reading says the same as the output
cards. Pure: no I/O.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.model.config_doc import ConfigDoc
from app.sync.summary import BASS_BELOW_HZ, analyse_band, analyse_eq, eq_clause, hz, hz_range, join_words, steepness


def band_words(band: Any) -> str:
    """``20–80 Hz (steep)``, ``bass below 80 Hz (steep)``, ``everything above 20 Hz (gentle)``, ``full range``."""
    if band.empty:
        return "silent (a band filter is all zeros)"
    if band.nothing_passes:
        return "nothing passes (its high-pass is above its low-pass)"
    if band.custom:
        return "a custom band filter"
    hp_word = steepness(band.hp[1]) if band.hp else None
    lp_word = steepness(band.lp[1]) if band.lp else None
    slopes = f"{hp_word} below, {lp_word} above" if hp_word and lp_word and hp_word != lp_word else hp_word or lp_word
    if band.hp and band.lp:
        return f"{hz_range(band.hp[0], band.lp[0])} ({slopes})"
    if band.lp:
        what = "bass below" if band.lp[0] <= BASS_BELOW_HZ else "everything below"
        return f"{what} {hz(band.lp[0])} ({slopes})"
    if band.hp:
        return f"everything above {hz(band.hp[0])} ({slopes})"
    return "full range"


def output_phrase(doc: ConfigDoc, n: int, name: str, inputs: Sequence[int] | None, input_names: Sequence[str]) -> str:
    """``Shakers: 20–80 Hz (steep), 1 EQ cut at 40 Hz`` for output ``n`` as the graph draws it, fed by ``inputs``
    (None: every input)."""
    out = doc.outputs[n]
    feeding = list(range(len(doc.inputs))) if inputs is None else [i for i in inputs if 0 <= i < len(doc.inputs)]
    routed = [i for i in feeding if doc.inputs[i].routing[n].enabled]
    if not routed:
        sources = join_words([input_names[i] if i < len(input_names) else f"In {i + 1}" for i in feeding])
        verb = "isn't" if len(feeding) == 1 else "aren't"
        return f"{name}: silent here ({sources or 'no input'} {verb} routed to it)"
    if all(doc.inputs[i].mute for i in routed):
        return f"{name}: silent here (its input is muted)"
    words = [band_words(analyse_band(out))]
    eq = eq_clause(analyse_eq(out.peq))
    if eq:
        words.append(eq)
    if out.mute:
        words.append("muted (drawn as if un-muted)")
    return f"{name}: {', '.join(words)}"


def graph_reading(
    doc: ConfigDoc,
    outputs: Sequence[int],
    names: Sequence[str],
    *,
    inputs: Sequence[int] | None = (0,),
    input_names: Sequence[str] = (),
) -> str:
    """The reading of a graph of ``outputs``: one phrase per output, as sentences. Empty when nothing is plotted."""
    phrases = [output_phrase(doc, n, names[n], inputs, input_names) for n in outputs if 0 <= n < len(doc.outputs)]
    return " ".join(f"{p}." for p in phrases)


def tactile_outputs(channel_meta: Mapping[tuple[str, int], Any] | None, outputs: Sequence[int]) -> list[int]:
    """The plotted outputs marked Shakers (tactile) in Settings: their graphs hide the sum line by default."""
    meta = channel_meta or {}
    return [n for n in outputs if bool(getattr(meta.get(("output", n)), "tactile", False))]
