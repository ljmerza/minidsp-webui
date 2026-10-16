"""PEQ band and crossover group edits: design parameters or raw biquads -> compiled coefficients -> session edit.

The ConfigDoc keeps both the design intent and the compiled biquads. This module turns a request into a new band or
group, then hands it to `DeviceSession.edit`, which validates, stages and queues the device writes. The writer
enforces the ordering rules (PEQ un-bypass in a later call, never un-bypass unconfirmed coefficients).

- A design is compiled with `app.dsp.design`. When the result matches what the band or group already holds (within
  the recognition tolerance, in any section order for a group), the stored coefficients are kept. Re-designing
  identical parameters therefore changes no device path and causes no writes.
- Raw biquads are stored exactly as given, after a stability check (float64 and float32). The design is recovered by
  recognition: an LR4 low-pass at 80 Hz entered as two BW2 sections shows as "LR4 LP 80 Hz", and anything else is
  ``raw`` (band) or custom (group, design None).
- Crossover groups hold exactly ``profile.crossover_biquads`` biquads: designs and raw lists that need more are
  refused, shorter ones are padded with pass-through.
"""

from collections.abc import Sequence
from typing import Any, Literal

from app.dsp.design import DesignError, design_crossover, design_filter
from app.dsp.design import raw as raw_biquad
from app.dsp.recognize import (
    annotate_designs,
    coeffs_match,
    describe_crossover,
    describe_filter,
    recognize_biquad,
    recognize_crossover,
    responses_match,
)
from app.model.config_doc import ConfigDoc, CrossoverGroup, PeqBand
from app.model.device_profile import DeviceProfile
from app.model.filters import PASSTHROUGH, Biquad, CrossoverDesign, FilterDesign
from app.sync.session import DesiredValidationError, DeviceSession, EditResult

Side = Literal["inputs", "outputs"]
type BiquadInput = Biquad | Sequence[float]


class FilterEditError(DesiredValidationError):
    """A band or group request was refused (bad index, impossible design, unstable biquad, too many sections)."""


def _to_biquad(value: BiquadInput, where: str) -> Biquad:
    try:
        return raw_biquad(value)
    except (DesignError, ValueError, TypeError) as exc:
        raise FilterEditError("invalid biquad", [f"{where}: {exc}"]) from None


def _same_cascade(a: Sequence[Biquad], b: Sequence[Biquad]) -> bool:
    """Same biquads in any order (each within the recognition tolerance) and the same response."""
    if len(a) != len(b):
        return False
    remaining = list(b)
    for bq in a:
        hit = next((k for k, other in enumerate(remaining) if coeffs_match(other, bq)), None)
        if hit is None:
            return False
        remaining.pop(hit)
    return responses_match(list(a), list(b))


# --- pure compilation ---------------------------------------------------------------------------------------------


def compile_band(
    current: PeqBand,
    *,
    design: FilterDesign | None = None,
    raw: BiquadInput | None = None,
    bypass: bool | None = None,
    where: str = "band",
) -> PeqBand:
    """The band after the request. ``bypass`` None keeps the current flag; with neither design nor raw only the
    flag changes."""
    if design is not None and raw is not None:
        raise FilterEditError("give either a design or raw coefficients, not both")
    if design is not None and design.type == "raw":
        raw = design.raw
    if raw is not None:
        compiled = _to_biquad(raw, f"{where}.raw")
        new_design = recognize_biquad(compiled)
    elif design is not None:
        try:
            compiled = design_filter(design)
        except DesignError as exc:
            raise FilterEditError("the filter can't be built", [f"{where}.design: {exc}"]) from None
        if _same_cascade([compiled], [current.compiled]):
            compiled = current.compiled
        new_design = design
    else:
        if bypass is None:
            raise FilterEditError("nothing to change: give a design, raw coefficients or bypass")
        compiled, new_design = current.compiled, current.design
    return current.model_copy(
        update={"design": new_design, "compiled": compiled, "bypass": current.bypass if bypass is None else bypass}
    )


def compile_group(
    current: CrossoverGroup,
    n_biquads: int,
    *,
    design: CrossoverDesign | None = None,
    biquads: Sequence[BiquadInput] | None = None,
    bypass: bool | None = None,
    where: str = "group",
) -> CrossoverGroup:
    """The group after the request. ``biquads=[]`` flattens the group to pass-through (design None). ``bypass``
    None keeps the current flag; with neither design nor biquads only the flag changes."""
    if design is not None and biquads is not None:
        raise FilterEditError("give either a design or raw biquads, not both")
    if biquads is not None:
        if len(biquads) > n_biquads:
            raise FilterEditError(
                "too many biquads", [f"{where}.compiled: {len(biquads)} given, the group holds {n_biquads}"]
            )
        given = [_to_biquad(bq, f"{where}.compiled.{k}") for k, bq in enumerate(biquads)]
        compiled = given + [PASSTHROUGH] * (n_biquads - len(given))
        new_design = recognize_crossover(compiled)
    elif design is not None:
        try:
            compiled = design_crossover(design, n_biquads)
        except DesignError as exc:
            raise FilterEditError("the crossover can't be built", [f"{where}.design: {exc}"]) from None
        if _same_cascade(compiled, current.compiled):
            compiled = list(current.compiled)
        new_design = design
    else:
        if bypass is None:
            raise FilterEditError("nothing to change: give a design, biquads or bypass")
        compiled, new_design = list(current.compiled), current.design
    return current.model_copy(
        update={"design": new_design, "compiled": compiled, "bypass": current.bypass if bypass is None else bypass}
    )


def recognize_doc(doc: ConfigDoc) -> ConfigDoc:
    """Attach editable designs to imported or adopted coefficients (never changes coefficients or bypass flags)."""
    return annotate_designs(doc)


# --- views --------------------------------------------------------------------------------------------------------


def band_view(band: PeqBand) -> dict[str, Any]:
    return {**band.model_dump(mode="json"), "label": describe_filter(band.design)}


def group_view(group: CrossoverGroup) -> dict[str, Any]:
    used = sum(1 for bq in group.compiled if not bq.is_passthrough)
    return {**group.model_dump(mode="json"), "label": describe_crossover(group.design), "biquads_used": used}


# --- addressing ---------------------------------------------------------------------------------------------------


def _channel_count(profile: DeviceProfile, side: str) -> int:
    if side == "inputs":
        return profile.n_inputs
    if side == "outputs":
        return profile.n_outputs
    raise FilterEditError(f"unknown channel kind {side!r}; expected inputs or outputs")


def check_band_index(profile: DeviceProfile, side: str, channel: int, band: int) -> str:
    """The band's path; raises `FilterEditError` for an index outside the profile."""
    count = _channel_count(profile, side)
    if not 0 <= channel < count:
        raise FilterEditError(f"{side[:-1]} {channel} outside 0..{count - 1}")
    bands = profile.peq_per_input if side == "inputs" else profile.peq_per_output
    if not 0 <= band < bands:
        raise FilterEditError(f"PEQ band {band} outside 0..{bands - 1}")
    return f"{side}.{channel}.peq.{band}"


def check_group_index(profile: DeviceProfile, channel: int, group: int) -> str:
    """The group's path; raises `FilterEditError` for an index outside the profile."""
    if not 0 <= channel < profile.n_outputs:
        raise FilterEditError(f"output {channel} outside 0..{profile.n_outputs - 1}")
    if not 0 <= group < profile.crossover_groups:
        raise FilterEditError(f"crossover group {group} outside 0..{profile.crossover_groups - 1}")
    return f"outputs.{channel}.crossover.{group}"


async def get_band(session: DeviceSession, preset: int, side: str, channel: int, band: int) -> PeqBand:
    check_band_index(session.profile, side, channel, band)
    doc = await session.get_desired(preset)
    return getattr(doc, side)[channel].peq[band]


async def get_group(session: DeviceSession, preset: int, channel: int, group: int) -> CrossoverGroup:
    check_group_index(session.profile, channel, group)
    doc = await session.get_desired(preset)
    return doc.outputs[channel].crossover[group]


# --- edits --------------------------------------------------------------------------------------------------------


def _changed_fields(coefficients: bool, bypass: bool | None) -> set[str]:
    """The fields a request rewrites. The band or group is read before the session's edit lock is taken, so a
    field the request leaves alone must stay out of the patch: otherwise a concurrent edit to it (a bypass toggle
    racing a design commit) would be silently reverted."""
    fields = {"design", "compiled"} if coefficients else set()
    if bypass is not None:
        fields.add("bypass")
    return fields


async def set_peq_band(
    session: DeviceSession,
    preset: int,
    side: str,
    channel: int,
    band: int,
    *,
    design: FilterDesign | None = None,
    raw: BiquadInput | None = None,
    bypass: bool | None = None,
    **edit_kwargs: Any,
) -> EditResult:
    """Set one PEQ band from a design or raw coefficients (and/or its bypass flag). ``edit_kwargs`` go to
    `DeviceSession.edit` (confirm, expected_revision, source, commit, summary)."""
    path = check_band_index(session.profile, side, channel, band)
    current = await get_band(session, preset, side, channel, band)
    if current.external:
        raise FilterEditError(f"{path} is managed by another tool and is read-only here")
    new = compile_band(current, design=design, raw=raw, bypass=bypass, where=path)
    fields = _changed_fields(design is not None or raw is not None, bypass)
    patch = {side: {str(channel): {"peq": {str(band): new.model_dump(mode="json", include=fields)}}}}
    return await session.edit(preset, patch, **edit_kwargs)


async def set_crossover_group(
    session: DeviceSession,
    preset: int,
    channel: int,
    group: int,
    *,
    design: CrossoverDesign | None = None,
    biquads: Sequence[BiquadInput] | None = None,
    bypass: bool | None = None,
    **edit_kwargs: Any,
) -> EditResult:
    """Set one crossover group from a design or raw biquads (and/or its bypass flag). ``edit_kwargs`` go to
    `DeviceSession.edit`."""
    path = check_group_index(session.profile, channel, group)
    current = await get_group(session, preset, channel, group)
    new = compile_group(
        current, session.profile.crossover_biquads, design=design, biquads=biquads, bypass=bypass, where=path
    )
    fields = _changed_fields(design is not None or biquads is not None, bypass)
    patch = {"outputs": {str(channel): {"crossover": {str(group): new.model_dump(mode="json", include=fields)}}}}
    return await session.edit(preset, patch, **edit_kwargs)
