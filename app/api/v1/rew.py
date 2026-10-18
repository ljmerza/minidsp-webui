"""REW biquad text (docs 06): preview a file, import it into a PEQ bank or crossover, export a bank or crossover.

- ``POST /import/rew`` parses without changing anything: the filters with their recognized designs, warnings and
  errors.
- ``POST /devices/{serial}/presets/{p}/import/rew`` places the filters (see `app.formats.rew` for the CLI-compatible
  rules) and queues the writes. Imported bands and groups are un-bypassed; the writer sends a PEQ un-bypass only once
  the new coefficients are confirmed on the device. Files with errors, filters that don't fit and boosts above the
  limit are refused (422); a large boost, or a crossover import that turns an active group into pure pass-through,
  needs ``confirm`` (409 otherwise).
- ``GET /devices/{serial}/presets/{p}/export/rew?bank=outputs.0.peq`` returns the bank (or crossover) as REW text.

Indexes are 0-based like the rest of the API; ``pick`` counts filters in the file from 1.
"""

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import get_session
from app.api.v1.presets import Commit, IfMatch, edit_json, parse_if_match, translate_errors
from app.dsp.recognize import describe_filter, recognize_biquad
from app.formats.rew import (
    Bank,
    Index,
    ParsedRew,
    RewImportError,
    RewIssue,
    bank_biquads,
    bank_patch,
    cleared_groups,
    export_rew,
    parse_bank,
    parse_rew,
    place_crossover,
    place_peq,
)
from app.model.filters import Biquad
from app.sync.filters import band_view, group_view
from app.sync.session import ConfirmationRequired, DesiredValidationError, DeviceSession

router = APIRouter(tags=["import/export"])

Session = Annotated[DeviceSession, Depends(get_session)]
MAX_REW_CHARS = 100_000
BankQuery = Annotated[str, Query(description="inputs.N.peq, outputs.N.peq or outputs.N.crossover")]
IndexQuery = Annotated[str, Query(description="PEQ band or crossover group: 0-based index or 'all'")]
BiquadQuery = Annotated[str, Query(description="Crossover biquad: 0-based index or 'all' (ignored for PEQ)")]


class RewPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_REW_CHARS)


class RewImport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_REW_CHARS)
    bank: str = Field(description="inputs.N.peq, outputs.N.peq or outputs.N.crossover")
    index: int | Literal["all"] = Field(default="all", description="PEQ band or crossover group (0-based) or 'all'")
    biquad: int | Literal["all"] = Field(default="all", description="Crossover biquad (0-based) or 'all'")
    pick: int | None = Field(default=None, ge=1, description="Import only this filter of the file (1 = the first)")
    unbypass: bool = Field(default=True, description="Enable the imported bands/groups (as the CLI does)")
    confirm: bool = Field(
        default=False,
        description="Allow filter boosts above the big-step threshold, and clearing an active crossover group",
    )


def _issue(issue: RewIssue) -> dict[str, Any]:
    return {"line": issue.line, "text": issue.text, "message": issue.message}


def _filter_json(number: int, line: int, biquad: Biquad) -> dict[str, Any]:
    design = recognize_biquad(biquad)
    return {
        "number": number,
        "line": line,
        "biquad": biquad.model_dump(mode="json"),
        "design": design.model_dump(mode="json", exclude_none=True),
        "label": describe_filter(design),
    }


def parsed_json(parsed: ParsedRew) -> dict[str, Any]:
    return {
        "ok": parsed.ok,
        "filters": [_filter_json(f.number, f.line, f.biquad) for f in parsed.filters],
        "warnings": [_issue(w) for w in parsed.warnings],
        "errors": [_issue(e) for e in parsed.errors],
    }


def _index(text: str, name: str) -> Index:
    text = text.strip().lower()
    if text == "all":
        return "all"
    if text.isascii() and text.isdigit():
        return int(text)
    raise DesiredValidationError(f"{name} must be a 0-based index or 'all'")


def _bank(text: str, session: DeviceSession) -> Bank:
    try:
        return parse_bank(text, session.profile)
    except RewImportError as exc:
        raise DesiredValidationError(str(exc), exc.problems) from None


@router.post("/import/rew")
async def preview_rew(body: RewPreview) -> dict:
    """Parse REW biquad text: each filter with its recognized design, plus warnings (ignored lines) and errors."""
    return await asyncio.to_thread(parsed_json, parse_rew(body.text))


@router.post("/devices/{serial}/presets/{preset}/import/rew")
@translate_errors
async def import_rew(
    preset: int,
    body: RewImport,
    session: Session,
    commit: Commit = True,
    if_match: IfMatch = None,
) -> dict:
    """Import REW biquads into a PEQ bank (``index`` = band or ``all``, which clears the remaining bands) or an
    output's crossover (``index`` = group, ``biquad`` = slot; ``all``/``all`` holds up to 8 biquads)."""
    bank = _bank(body.bank, session)
    parsed = parse_rew(body.text)
    if not parsed.ok:
        raise DesiredValidationError(
            "the REW file has errors", [f"line {e.line}: {e.message}" if e.line else e.message for e in parsed.errors]
        )
    biquads = parsed.biquads
    if body.pick is not None:
        if body.pick > len(biquads):
            raise DesiredValidationError(f"pick {body.pick}: the file has {len(biquads)} filter(s)")
        biquads = [biquads[body.pick - 1]]
    doc = await session.get_desired(preset)
    try:
        if bank.kind == "peq":
            changed, warnings = await asyncio.to_thread(
                place_peq, doc, session.profile, bank, biquads, body.index, unbypass=body.unbypass
            )
            views = {f"{bank.path}.{k}": band_view(band) for k, band in changed.items()}
        else:
            changed, warnings = await asyncio.to_thread(
                place_crossover,
                doc,
                session.profile,
                bank,
                biquads,
                body.index,
                body.biquad,
                unbypass=body.unbypass,
            )
            views = {f"{bank.path}.{g}": group_view(group) for g, group in changed.items()}
            if cleared := cleared_groups(doc, bank, changed):
                names = ", ".join(str(g + 1) for g in cleared)
                text = f"the import clears crossover group {names} of {bank.path} to pass-through, removing its filters"
                if not body.confirm:
                    raise ConfirmationRequired(text)
                warnings.append(text)
    except RewImportError as exc:
        raise DesiredValidationError(str(exc), exc.problems) from None
    summary = f"import REW: {len(biquads)} filter(s) into {bank.path}"
    result = await session.edit(
        preset,
        bank_patch(bank, changed),
        confirm=body.confirm,
        expected_revision=parse_if_match(if_match),
        source="import",
        commit=commit,
        summary=summary,
    )
    if result.paths:
        session.log_event(
            "info",
            f"imported {len(biquads)} REW filter(s) into {bank.path} of preset {preset + 1}",
            kind="import",
            preset=preset,
        )
    return {
        **edit_json(session, result),
        "imported": views,
        "warnings": [_issue(w) for w in parsed.warnings] + [{"line": 0, "text": "", "message": w} for w in warnings],
    }


@router.get("/devices/{serial}/presets/{preset}/export/rew", response_class=PlainTextResponse)
@translate_errors
async def export_rew_text(
    preset: int,
    session: Session,
    bank: BankQuery,
    index: IndexQuery = "all",
    biquad: BiquadQuery = "all",
    include_bypassed: Annotated[
        bool, Query(description="Write a bypassed band/group's coefficients instead of pass-through")
    ] = False,
    download: Annotated[bool, Query(description="Send as a file attachment")] = False,
) -> PlainTextResponse:
    """A PEQ bank or crossover of the preset's desired state as REW biquad text (numbered from 1, in the order an
    import of the same target reads them back)."""
    target = _bank(bank, session)
    doc = await session.get_desired(preset)
    try:
        biquads = bank_biquads(
            doc,
            session.profile,
            target,
            _index(index, "index"),
            _index(biquad, "biquad"),
            include_bypassed=include_bypassed,
        )
    except RewImportError as exc:
        raise DesiredValidationError(str(exc), exc.problems) from None
    headers = {}
    if download:
        name = f"minidsp-preset-{preset + 1}-{target.side[:-1]}-{target.channel + 1}-{target.kind}.txt"
        headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return PlainTextResponse(export_rew(biquads), headers=headers)
