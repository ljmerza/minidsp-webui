"""REW biquad text: parse it, place the filters in a PEQ bank or crossover, and export a bank or crossover as one.

REW's miniDSP export (and Multi-Sub-Optimizer's) lists numbered biquads in the device convention (a1/a2 negated vs
RBJ, docs 04 §1), at the device's 96 kHz, so the coefficients are used exactly as written::

    biquad1,
    b0=0.999194903557377,
    b1=-1.9973354686174658,
    b2=0.9981886333563846,
    a1=1.9973354686174658,
    a2=-0.9973835369137615,
    biquad2,
    …

The parser splits on commas and newlines, so one filter per line works too. Keys are case-insensitive and may have
spaces around ``=``. A BOM, CRLF line ends and ``#`` comments are accepted; other lines are ignored with a warning.
Filters are used in file order (as the CLI does); the ``biquadN`` numbers are only reported.

Placement follows ``minidsp … peq|crossover … import`` (minidsp-rs ``bin/minidsp/handlers.rs``):

- PEQ ``all``: filter *k* goes to band *k*, the remaining bands are cleared to pass-through. One band ``K``: the first
  filter goes to band K.
- Crossover group ``G|all`` × biquad ``K|all``: the slots are filled group by group, then slot by slot, in file order.
  Slots left over in a targeted range are cleared (the CLI clears the whole group there, which is a bug).
- Every band or group the import touches is un-bypassed. The writer sends a PEQ un-bypass only once the new
  coefficients are confirmed on the device, so no un-bypass ever exposes old coefficients.

More filters than target slots is refused, unless the extra ones are all pass-through (REW lists unused slots that
way); those are dropped with a warning.

The exporter writes REW's own layout (comma after every value but the last), with the shortest float text that
round-trips, so exporting an imported file reproduces it byte for byte.
"""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from app.dsp.recognize import recognize_biquad, recognize_crossover
from app.dsp.stability import biquad_problem
from app.model.config_doc import ConfigDoc, CrossoverGroup, PeqBand
from app.model.device_profile import DeviceProfile
from app.model.filters import PASSTHROUGH, Biquad

KEYS = ("b0", "b1", "b2", "a1", "a2")
# More than any bank or crossover can hold (2 x 4 crossover slots, 10 PEQ bands); bounds parsing work.
MAX_FILTERS = 64

type Index = int | Literal["all"]

_HEADER = re.compile(r"biquad\s*(\d{1,9})\s*:?", re.IGNORECASE)  # bounded: int() of a huge number raises
_VALUE = re.compile(r"(b0|b1|b2|a1|a2)\s*=\s*(\S+)", re.IGNORECASE)
_FILTER_SETTINGS = re.compile(r"^filter\s+\d+\s*:", re.IGNORECASE)
_BANK = re.compile(r"(inputs|outputs)\.(\d+)\.(peq|crossover)")


# --- parsing -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RewFilter:
    """One biquad of the file. ``number`` is the N of ``biquadN``; ``line`` is the 1-based line of that header."""

    number: int
    biquad: Biquad
    line: int


@dataclass(frozen=True)
class RewIssue:
    line: int
    text: str
    message: str


@dataclass
class ParsedRew:
    filters: list[RewFilter] = field(default_factory=list)
    warnings: list[RewIssue] = field(default_factory=list)
    errors: list[RewIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.filters)

    @property
    def biquads(self) -> list[Biquad]:
        return [f.biquad for f in self.filters]


@dataclass
class _Block:
    number: int
    line: int
    text: str
    values: dict[str, float] = field(default_factory=dict)


def _number(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_rew(text: str) -> ParsedRew:
    """Parse REW biquad text. Never raises: problems are reported as errors (a filter with any error is dropped,
    and the caller should refuse the file) or warnings (ignored lines)."""
    out = ParsedRew()
    block: _Block | None = None
    numbers: list[int] = []
    settings_hint = False

    def close(current: _Block | None) -> None:
        if current is None:
            return
        missing = [k for k in KEYS if k not in current.values]
        if missing:
            out.errors.append(
                RewIssue(current.line, current.text, f"biquad{current.number}: missing {', '.join(missing)}")
            )
            return
        biquad = Biquad(**current.values)
        if problem := biquad_problem(biquad):
            out.errors.append(RewIssue(current.line, current.text, f"biquad{current.number}: {problem}"))
            return
        out.filters.append(RewFilter(current.number, biquad, current.line))

    for lineno, raw_line in enumerate(text.removeprefix("\ufeff").splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if _FILTER_SETTINGS.match(line):
            if not settings_hint:
                settings_hint = True
                out.errors.append(
                    RewIssue(
                        lineno,
                        raw_line.strip(),
                        "this is a REW filter-settings list, not biquad coefficients: in REW, export the filters "
                        "as biquad coefficients (miniDSP format, 96 kHz) instead",
                    )
                )
            continue
        for token in (t.strip() for t in line.split(",")):
            if not token:
                continue
            if header := _HEADER.fullmatch(token):
                close(block)
                if len(numbers) >= MAX_FILTERS:
                    out.errors.append(RewIssue(lineno, raw_line.strip(), f"more than {MAX_FILTERS} filters"))
                    return out
                number = int(header.group(1))
                if number in numbers:
                    out.warnings.append(
                        RewIssue(
                            lineno, raw_line.strip(), f"biquad{number} appears twice; filters are used in file order"
                        )
                    )
                numbers.append(number)
                block = _Block(number, lineno, raw_line.strip())
            elif value := _VALUE.fullmatch(token):
                key, number_text = value.group(1).lower(), value.group(2)
                if block is None:
                    out.errors.append(RewIssue(lineno, raw_line.strip(), f"{key} before any biquadN line"))
                elif key in block.values:
                    out.errors.append(RewIssue(lineno, raw_line.strip(), f"biquad{block.number}: {key} given twice"))
                elif (parsed := _number(number_text)) is None:
                    out.errors.append(
                        RewIssue(
                            lineno, raw_line.strip(), f"biquad{block.number}: {key} is not a number: {number_text!r}"
                        )
                    )
                else:
                    block.values[key] = parsed
            else:
                out.warnings.append(RewIssue(lineno, raw_line.strip(), "not part of a biquad; ignored"))
                break
    close(block)
    if not out.filters and not out.errors:
        out.errors.append(RewIssue(0, "", "no biquads found (expected biquad1, b0=…, b1=…, b2=…, a1=…, a2=…)"))
    return out


# --- export --------------------------------------------------------------------------------------------------------


def _fmt(value: float) -> str:
    return repr(float(value) + 0.0)  # + 0.0 turns -0.0 into 0.0


def export_rew(biquads: Sequence[Biquad]) -> str:
    """REW biquad text for ``biquads``, numbered from 1."""
    lines: list[str] = []
    for n, bq in enumerate(biquads, start=1):
        lines.append(f"biquad{n},")
        lines.extend(f"{k}={_fmt(v)}," for k, v in zip(KEYS, bq.as_tuple(), strict=True))
    if lines:
        lines[-1] = lines[-1].removesuffix(",")
    return "\n".join(lines) + "\n" if lines else ""


# --- targets -------------------------------------------------------------------------------------------------------


class RewImportError(ValueError):
    """The filters can't be placed where asked."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems)


@dataclass(frozen=True)
class Bank:
    """A PEQ bank (``inputs.N.peq`` / ``outputs.N.peq``) or an output's crossover (``outputs.N.crossover``)."""

    side: Literal["inputs", "outputs"]
    channel: int
    kind: Literal["peq", "crossover"]

    @property
    def path(self) -> str:
        return f"{self.side}.{self.channel}.{self.kind}"


def parse_bank(text: str, profile: DeviceProfile) -> Bank:
    match = _BANK.fullmatch(text.strip())
    if match is None:
        raise RewImportError("unknown bank; expected inputs.N.peq, outputs.N.peq or outputs.N.crossover")
    side, channel, kind = match.group(1), int(match.group(2)), match.group(3)
    if kind == "crossover" and side != "outputs":
        raise RewImportError("only outputs have a crossover")
    count = profile.n_inputs if side == "inputs" else profile.n_outputs
    if not 0 <= channel < count:
        raise RewImportError(f"{side[:-1]} {channel} outside 0..{count - 1}")
    return Bank(side, channel, kind)  # type: ignore[arg-type]


def _range(index: Index, count: int, what: str) -> list[int]:
    if index == "all":
        return list(range(count))
    if not isinstance(index, int) or not 0 <= index < count:
        raise RewImportError(f"{what} {index} outside 0..{count - 1}")
    return [index]


def _fit(biquads: Sequence[Biquad], slots: int, where: str) -> tuple[list[Biquad], list[str]]:
    """``biquads`` cut to ``slots``, refusing to drop anything but pass-through."""
    if len(biquads) <= slots:
        return list(biquads), []
    extra = biquads[slots:]
    if any(not bq.is_passthrough for bq in extra):
        raise RewImportError(
            f"{len(biquads)} filters don't fit in {where} ({slots} slot{'s' * (slots != 1)})",
            ["import into 'all' or pick one filter of the file"] if slots == 1 else [],
        )
    return list(biquads[:slots]), [f"{len(extra)} trailing pass-through filter(s) dropped: {where} is full"]


def peq_slots(profile: DeviceProfile, bank: Bank) -> int:
    return profile.peq_per_input if bank.side == "inputs" else profile.peq_per_output


def place_peq(
    doc: ConfigDoc,
    profile: DeviceProfile,
    bank: Bank,
    biquads: Sequence[Biquad],
    index: Index = "all",
    *,
    unbypass: bool = True,
) -> tuple[dict[int, PeqBand], list[str]]:
    """The PEQ bands an import changes (band index -> new band) and any warnings. Designs are recognized."""
    if bank.kind != "peq":
        raise RewImportError(f"{bank.path} is not a PEQ bank")
    bands = getattr(doc, bank.side)[bank.channel].peq
    targets = _range(index, peq_slots(profile, bank), "PEQ band")
    where = f"{bank.path} (all bands)" if index == "all" else f"{bank.path}.{index}"
    fitted, warnings = _fit(biquads, len(targets), where)
    if not fitted:
        raise RewImportError("no filters to import")
    external = [f"{bank.path}.{k}" for k in targets if bands[k].external]
    if external:
        raise RewImportError("managed by another tool and read-only here", external)
    out: dict[int, PeqBand] = {}
    for n, k in enumerate(targets):
        compiled = fitted[n] if n < len(fitted) else PASSTHROUGH
        out[k] = bands[k].model_copy(
            update={
                "compiled": compiled,
                "design": recognize_biquad(compiled, profile.sample_rate),
                "bypass": False if unbypass else bands[k].bypass,
            }
        )
    return out, warnings


def place_crossover(
    doc: ConfigDoc,
    profile: DeviceProfile,
    bank: Bank,
    biquads: Sequence[Biquad],
    group: Index = "all",
    biquad: Index = "all",
    *,
    unbypass: bool = True,
) -> tuple[dict[int, CrossoverGroup], list[str]]:
    """The crossover groups an import changes (group index -> new group) and any warnings. ``group="all",
    biquad="all"`` takes up to groups x biquads filters (8 on the 2x4 HD, e.g. a Multi-Sub-Optimizer export)."""
    if bank.kind != "crossover":
        raise RewImportError(f"{bank.path} is not a crossover")
    groups = doc.outputs[bank.channel].crossover
    group_ids = _range(group, profile.crossover_groups, "crossover group")
    slot_ids = _range(biquad, profile.crossover_biquads, "crossover biquad")
    slots = [(g, k) for g in group_ids for k in slot_ids]
    where = f"{bank.path} group {group} biquad {biquad}"
    fitted, warnings = _fit(biquads, len(slots), where)
    if not fitted:
        raise RewImportError("no filters to import")
    compiled = {g: list(groups[g].compiled) for g in group_ids}
    for n, (g, k) in enumerate(slots):
        compiled[g][k] = fitted[n] if n < len(fitted) else PASSTHROUGH
    out = {
        g: groups[g].model_copy(
            update={
                "compiled": compiled[g],
                "design": recognize_crossover(compiled[g], profile.sample_rate),
                "bypass": False if unbypass else groups[g].bypass,
            }
        )
        for g in group_ids
    }
    return out, warnings


def cleared_groups(doc: ConfigDoc, bank: Bank, changed: dict[int, CrossoverGroup]) -> list[int]:
    """Groups that filter audio now (enabled, not all pass-through) and that the import turns into pure pass-through:
    e.g. a 2-biquad file imported into both groups wipes a protective high-pass in the second one."""
    groups = doc.outputs[bank.channel].crossover

    def active(group: CrossoverGroup) -> bool:
        return not group.bypass and any(bq != PASSTHROUGH for bq in group.compiled)

    return [g for g, new in sorted(changed.items()) if active(groups[g]) and not active(new)]


def bank_patch(bank: Bank, changed: dict[int, PeqBand] | dict[int, CrossoverGroup]) -> dict:
    """A merge patch (for `DeviceSession.edit`) writing the design, coefficients and bypass of ``changed``."""
    fields = {"design", "compiled", "bypass"}
    items = {str(k): item.model_dump(mode="json", include=fields) for k, item in changed.items()}
    return {bank.side: {str(bank.channel): {bank.kind: items}}}


def bank_biquads(
    doc: ConfigDoc,
    profile: DeviceProfile,
    bank: Bank,
    index: Index = "all",
    biquad: Index = "all",
    *,
    include_bypassed: bool = False,
) -> list[Biquad]:
    """The biquads to export, in import order (so importing the export restores them). A bypassed band or group
    exports as pass-through, its effective response, unless ``include_bypassed``."""
    channel = getattr(doc, bank.side)[bank.channel]
    if bank.kind == "peq":
        bands = channel.peq
        return [
            bands[k].compiled if include_bypassed or not bands[k].bypass else PASSTHROUGH
            for k in _range(index, peq_slots(profile, bank), "PEQ band")
        ]
    groups = channel.crossover
    return [
        groups[g].compiled[k] if include_bypassed or not groups[g].bypass else PASSTHROUGH
        for g in _range(index, profile.crossover_groups, "crossover group")
        for k in _range(biquad, profile.crossover_biquads, "crossover biquad")
    ]
