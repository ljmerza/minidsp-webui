"""Phase 3 filter UI routes: the PEQ editor, the crossover designer, the output Response tab and the Dashboard response
widget (docs 05 §1, §4, §5).

All JSON, driven by static/js/peq_editor.js and static/js/crossover.js. The pages render a component shell and the
component loads its state from here, so the output page, the input page and the Dashboard need no extra context.

- ``GET  /ui/devices/{serial}/presets/{p}/{inputs|outputs}/{n}/peq``: the bank's bands, labels and sync status.
- ``POST …/peq/{k}``: set one band from ``{design | raw, bypass?}`` (compiled and checked by the server).
- ``POST …/peq``: ``{action: bypass_all | reset_all}`` for every band the app manages.
- ``GET  …/outputs/{n}/crossover``: both groups, their labels and the biquads each uses.
- ``POST …/outputs/{n}/crossover/{g}``: ``{design | biquads, bypass?}``; ``biquads: []`` flattens the group.
- ``POST …/outputs/{n}/crossover``: ``{preset: sub_lp80 | tactile | flat}`` sets both groups in one edit.
- ``GET  …/outputs/{n}/response``: the output's full chain plus its contributions (input, PEQ, groups, FIR).
- ``GET  /ui/devices/{serial}/response``: the electrical sum of the un-muted outputs and each output on its own.

Edits go through `app.sync.filters` with ``source="ui"``, so a re-designed filter whose coefficients already match
the stored ones changes nothing and writes nothing, and the writer keeps the ordering rules (coefficients before a
PEQ un-bypass, never un-bypass unconfirmed coefficients).
"""

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Any, Literal

import numpy as np
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api.deps import require_same_origin
from app.dsp import response as resp
from app.dsp.recognize import describe_crossover
from app.model.config_doc import ConfigDoc, CrossoverGroup, PeqBand
from app.model.filters import Biquad, CrossoverDesign, FilterDesign
from app.state import RevisionConflict
from app.sync import filters as svc
from app.sync.session import (
    ConfirmationRequired,
    DesiredValidationError,
    DeviceSession,
    EditResult,
    MasterValidationError,
    StateUnavailable,
    UnknownPreset,
)
from app.sync.status import badge_statuses
from app.web.sync_ui import confirmation_text
from app.web.ui_common import channel_color, channel_label, device_preset, is_htmx, load_state, manager_of

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_same_origin)])

NO_STORE = {"Cache-Control": "no-store"}
SUM_COLOR = "#e3e7ec"  # graph.js COLORS.sum
CONTRIBUTION_COLORS = {
    "input": "#a78bfa",
    "peq": "#4ea1ff",
    "crossover.0": "#f472b6",
    "crossover.1": "#facc15",
    "fir": "#2dd4bf",
}
DASH = [6, 4]
DASHBOARD_POINTS = 256

BiquadIn = Biquad | Annotated[list[float], Field(min_length=5, max_length=5)]


# --- request bodies -------------------------------------------------------------------------------------------------


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit: bool = True  # a commit boundary (release, typed value, button)
    confirm: bool = False


class BandBody(_Body):
    design: FilterDesign | None = None
    raw: BiquadIn | None = None
    bypass: bool | None = None


class GroupBody(_Body):
    design: CrossoverDesign | None = None
    biquads: list[BiquadIn] | None = Field(default=None, max_length=16)
    bypass: bool | None = None


class BankAction(_Body):
    action: Literal["bypass_all", "reset_all"]


class CrossoverPresetBody(_Body):
    preset: Literal["sub_lp80", "tactile", "flat"]


# Crossover templates of the Band tab (docs 05 §4; plain labels, phase 9 design §8.1): group designs
# (None = leave the group alone) and bypass flags.
CROSSOVER_PRESETS: dict[str, dict[str, Any]] = {
    "sub_lp80": {
        "label": "Sub: below 80 Hz",
        "groups": (CrossoverDesign(kind="lp", family="lr", order=4, freq=80), None),
        "bypass": (False, True),
    },
    "tactile": {
        "label": "Shakers: 20–80 Hz",
        "groups": (
            CrossoverDesign(kind="lp", family="lr", order=4, freq=80),
            CrossoverDesign(kind="hp", family="lr", order=4, freq=20),
        ),
        "bypass": (False, False),
    },
    "flat": {"label": "Full range (off)", "groups": (None, None), "bypass": (True, True)},
}


# --- helpers --------------------------------------------------------------------------------------------------------


def _json(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status_code, headers=NO_STORE)


def _error(status_code: int, error: str, detail: str, **extra: Any) -> JSONResponse:
    return _json({"error": error, "detail": detail, **extra}, status_code)


async def _session(request: Request, serial: int, preset: int | None) -> DeviceSession | JSONResponse:
    session = manager_of(request).get(serial)
    if session is None:
        return _error(404, "unknown_device", f"Unknown device {serial}")
    if preset is not None and not 0 <= preset < session.profile.n_presets:
        return _error(404, "unknown_preset", f"There is no preset {preset + 1} on this device")
    problem = await load_state(session)
    if problem:
        return _error(503, "state_unavailable", problem)
    return session


async def _body[M: BaseModel](request: Request, model: type[M]) -> M | JSONResponse:
    try:
        payload = await request.json()
    except ValueError:
        return _error(400, "bad_request", "expected a JSON body")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        problems = [_validation_message(e) for e in exc.errors()]
        return _error(422, "invalid", "; ".join(problems), problems=problems)


def _validation_message(error: Any) -> str:
    where = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "invalid value")).removeprefix("Value error, ")
    return f"{where}: {message}" if where else message


async def _run_edit(
    session: DeviceSession, call: Callable[[], Awaitable[EditResult]], **on_error: Any
) -> EditResult | JSONResponse:
    """Run a session edit, mapping its refusals to the JSON errors the components show. ``on_error`` is added to
    error replies (the stored band or group, so the editor can put it back). A confirmation question names the
    settings in words (`confirmation_text`)."""
    try:
        return await call()
    except ConfirmationRequired as exc:
        return _error(409, "confirmation_required", confirmation_text(session, exc), **on_error)
    except UnknownPreset as exc:
        return _error(404, "unknown_preset", str(exc), **on_error)
    except DesiredValidationError as exc:
        problems = list(exc.problems)
        return _error(422, "invalid", "; ".join(problems) or str(exc), problems=problems, **on_error)
    except MasterValidationError as exc:
        return _error(422, "invalid", str(exc), **on_error)
    except RevisionConflict as exc:
        return _error(412, "revision_conflict", str(exc), **on_error)
    except StateUnavailable as exc:
        return _error(503, "state_unavailable", str(exc), **on_error)


def _statuses(session: DeviceSession, preset: int, prefix: str) -> dict[str, str]:
    # The status model's overlay (≠ changed on device, ⏸ waiting), as the header and the sync bar (design 11.1).
    return {p: s for p, s in badge_statuses(session, preset).items() if p.startswith(prefix)}


def band_source(band: PeqBand) -> str:
    """The PEQ table's Source column: designed, raw (unrecognized coefficients), flat or external."""
    if band.external:
        return "external"
    if band.design.type == "raw":
        return "raw"
    if band.design.type == "passthrough" and band.compiled.is_passthrough:
        return "flat"
    return "designed"


def band_state(band: PeqBand, index: int, path: str) -> dict[str, Any]:
    return {**svc.band_view(band), "index": index, "path": path, "source": band_source(band)}


def group_state(group: CrossoverGroup, index: int, path: str) -> dict[str, Any]:
    view = svc.group_view(group)
    if group.design is None:
        view["label"] = "Flat" if view["biquads_used"] == 0 else "Custom"
    return {**view, "index": index, "path": path}


def _edit_reply(session: DeviceSession, result: EditResult, prefix: str, **extra: Any) -> JSONResponse:
    summary = session.sync_summary(result.preset)
    return _json(
        {
            "preset": result.preset,
            "revision": result.revision,
            "changed": result.paths,
            "sync": _statuses(session, result.preset, prefix),
            "counts": summary["counts"],
            **extra,
        }
    )


def _channel_count(session: DeviceSession, side: str) -> int:
    return session.profile.n_inputs if side == "inputs" else session.profile.n_outputs


def _check_channel(session: DeviceSession, side: str, channel: int) -> JSONResponse | None:
    count = _channel_count(session, side)
    if not 0 <= channel < count:
        return _error(404, "unknown_channel", f"This device has no {side[:-1]} {channel + 1}")
    return None


# --- PEQ ------------------------------------------------------------------------------------------------------------


def peq_state(session: DeviceSession, preset: int, side: str, channel: int) -> dict[str, Any]:
    doc = session.desired(preset)
    kind = side[:-1]
    prefix = f"{side}.{channel}.peq"
    bands = getattr(doc, side)[channel].peq
    return {
        "serial": session.serial,
        "preset": preset,
        "side": side,
        "channel": channel,
        "prefix": prefix,
        "name": channel_label(session, kind, channel),
        "color": channel_color(session, kind, channel),
        "fs": session.profile.sample_rate,
        "bands": [band_state(b, k, f"{prefix}.{k}") for k, b in enumerate(bands)],
        "sync": _statuses(session, preset, f"{prefix}."),
    }


@router.get("/ui/devices/{serial}/presets/{preset}/{side}/{channel}/peq")
async def get_peq(request: Request, serial: int, preset: int, side: Literal["inputs", "outputs"], channel: int):
    session = await _session(request, serial, preset)
    if isinstance(session, Response):
        return session
    if bad := _check_channel(session, side, channel):
        return bad
    return _json(peq_state(session, preset, side, channel))


@router.post("/ui/devices/{serial}/presets/{preset}/{side}/{channel}/peq/{band}")
async def set_band(
    request: Request, serial: int, preset: int, side: Literal["inputs", "outputs"], channel: int, band: int
):
    """Set one band: ``{design}`` or ``{raw}`` (and/or ``bypass``). The reply carries the stored band, whose
    coefficients replace the editor's preview."""
    if not is_htmx(request):
        return _error(400, "bad_request", "HTMX request expected")
    session = await _session(request, serial, preset)
    if isinstance(session, Response):
        return session
    body = await _body(request, BandBody)
    if isinstance(body, Response):
        return body
    current = _current_band(session, preset, side, channel, band)
    result = await _run_edit(
        session,
        lambda: svc.set_peq_band(
            session,
            preset,
            side,
            channel,
            band,
            design=body.design,
            raw=body.raw,
            bypass=body.bypass,
            confirm=body.confirm,
            commit=body.commit,
            source="ui",
        ),
        **({"band": current} if current is not None else {}),
    )
    if isinstance(result, Response):
        return result
    path = f"{side}.{channel}.peq.{band}"
    stored = getattr(result.doc, side)[channel].peq[band]
    return _edit_reply(session, result, f"{path}.", band=band_state(stored, band, path))


def _current_band(session: DeviceSession, preset: int, side: str, channel: int, band: int) -> dict[str, Any] | None:
    try:
        path = svc.check_band_index(session.profile, side, channel, band)
        current = getattr(session.desired(preset), side)[channel].peq[band]
    except (DesiredValidationError, StateUnavailable, UnknownPreset):
        return None
    return band_state(current, band, path)


@router.post("/ui/devices/{serial}/presets/{preset}/{side}/{channel}/peq")
async def peq_bank_action(request: Request, serial: int, preset: int, side: Literal["inputs", "outputs"], channel: int):
    """``bypass_all`` bypasses every band; ``reset_all`` makes every band flat and bypassed. Bands managed by
    another tool (``external``) are left alone."""
    if not is_htmx(request):
        return _error(400, "bad_request", "HTMX request expected")
    session = await _session(request, serial, preset)
    if isinstance(session, Response):
        return session
    if bad := _check_channel(session, side, channel):
        return bad
    body = await _body(request, BankAction)
    if isinstance(body, Response):
        return body
    bands = getattr(session.desired(preset), side)[channel].peq
    patch_bands: dict[str, Any] = {}
    for k, b in enumerate(bands):
        if b.external:
            continue
        if body.action == "bypass_all":
            if not b.bypass:
                patch_bands[str(k)] = {"bypass": True}
        else:
            flat = PeqBand()
            if b != flat:
                patch_bands[str(k)] = flat.model_dump(mode="json")
    if patch_bands:
        what = "bypass all" if body.action == "bypass_all" else "reset all"
        name = channel_label(session, side[:-1], channel)
        result = await _run_edit(
            session,
            lambda: session.edit(
                preset,
                {side: {str(channel): {"peq": patch_bands}}},
                confirm=body.confirm,
                commit=True,
                source="ui",
                summary=f"{name} PEQ: {what}",
            ),
        )
        if isinstance(result, Response):
            return result
        changed = result.paths
    else:
        changed = []
    return _json({**peq_state(session, preset, side, channel), "changed": changed})


# --- crossover ------------------------------------------------------------------------------------------------------


def crossover_state(session: DeviceSession, preset: int, channel: int) -> dict[str, Any]:
    doc = session.desired(preset)
    prefix = f"outputs.{channel}.crossover"
    groups = doc.outputs[channel].crossover
    return {
        "serial": session.serial,
        "preset": preset,
        "channel": channel,
        "prefix": prefix,
        "name": channel_label(session, "output", channel),
        "color": channel_color(session, "output", channel),
        "fs": session.profile.sample_rate,
        "n_biquads": session.profile.crossover_biquads,
        "groups": [group_state(g, k, f"{prefix}.{k}") for k, g in enumerate(groups)],
        "presets": [{"key": key, "label": p["label"]} for key, p in CROSSOVER_PRESETS.items()],
        "sync": _statuses(session, preset, f"{prefix}."),
    }


@router.get("/ui/devices/{serial}/presets/{preset}/outputs/{channel}/crossover")
async def get_crossover(request: Request, serial: int, preset: int, channel: int):
    session = await _session(request, serial, preset)
    if isinstance(session, Response):
        return session
    if bad := _check_channel(session, "outputs", channel):
        return bad
    return _json(crossover_state(session, preset, channel))


@router.post("/ui/devices/{serial}/presets/{preset}/outputs/{channel}/crossover/{group}")
async def set_group(request: Request, serial: int, preset: int, channel: int, group: int):
    """Set one group: ``{design}`` or ``{biquads}`` (``[]`` flattens), and/or ``bypass``."""
    if not is_htmx(request):
        return _error(400, "bad_request", "HTMX request expected")
    session = await _session(request, serial, preset)
    if isinstance(session, Response):
        return session
    body = await _body(request, GroupBody)
    if isinstance(body, Response):
        return body
    current = _current_group(session, preset, channel, group)
    result = await _run_edit(
        session,
        lambda: svc.set_crossover_group(
            session,
            preset,
            channel,
            group,
            design=body.design,
            biquads=body.biquads,
            bypass=body.bypass,
            confirm=body.confirm,
            commit=body.commit,
            source="ui",
        ),
        **({"group": current} if current is not None else {}),
    )
    if isinstance(result, Response):
        return result
    path = f"outputs.{channel}.crossover.{group}"
    stored = result.doc.outputs[channel].crossover[group]
    return _edit_reply(session, result, f"{path}.", group=group_state(stored, group, path))


def _current_group(session: DeviceSession, preset: int, channel: int, group: int) -> dict[str, Any] | None:
    try:
        path = svc.check_group_index(session.profile, channel, group)
        current = session.desired(preset).outputs[channel].crossover[group]
    except (DesiredValidationError, StateUnavailable, UnknownPreset):
        return None
    return group_state(current, group, path)


@router.post("/ui/devices/{serial}/presets/{preset}/outputs/{channel}/crossover")
async def crossover_preset(request: Request, serial: int, preset: int, channel: int):
    """Apply one of `CROSSOVER_PRESETS` to both groups in a single edit."""
    if not is_htmx(request):
        return _error(400, "bad_request", "HTMX request expected")
    session = await _session(request, serial, preset)
    if isinstance(session, Response):
        return session
    if bad := _check_channel(session, "outputs", channel):
        return bad
    body = await _body(request, CrossoverPresetBody)
    if isinstance(body, Response):
        return body
    chosen = CROSSOVER_PRESETS[body.preset]
    groups = session.desired(preset).outputs[channel].crossover
    n_biquads = session.profile.crossover_biquads
    patch: dict[str, Any] = {}
    try:
        for g, current in enumerate(groups):
            design = chosen["groups"][g] if g < len(chosen["groups"]) else None
            bypass = chosen["bypass"][g] if g < len(chosen["bypass"]) else True
            new = svc.compile_group(
                current, n_biquads, design=design, bypass=bypass, where=f"outputs.{channel}.crossover.{g}"
            )
            if new != current:
                patch[str(g)] = new.model_dump(mode="json")
    except DesiredValidationError as exc:
        return _error(422, "invalid", "; ".join(exc.problems) or str(exc), problems=list(exc.problems))
    changed: list[str] = []
    if patch:
        name = channel_label(session, "output", channel)
        result = await _run_edit(
            session,
            lambda: session.edit(
                preset,
                {"outputs": {str(channel): {"crossover": patch}}},
                confirm=body.confirm,
                commit=True,
                source="ui",
                summary=f"{name} crossover: {chosen['label']}",
            ),
        )
        if isinstance(result, Response):
            return result
        changed = result.paths
    return _json({**crossover_state(session, preset, channel), "changed": changed})


# --- responses ------------------------------------------------------------------------------------------------------


def _round_list(values: Any, digits: int) -> list[float]:
    return [round(float(v), digits) for v in values]


def compact(response: resp.Response) -> dict[str, Any]:
    """`Response.to_dict` with values rounded to what a plot can show (a fifth of the JSON size)."""
    d = response.to_dict()
    return {
        "freqs": [float(f"{f:.6g}") for f in d["freqs"]],
        "magnitude_db": _round_list(d["magnitude_db"], 3),
        "phase_deg": _round_list(d["phase_deg"], 2),
        "phase_unwrapped_deg": _round_list(d["phase_unwrapped_deg"], 2),
        "group_delay_ms": _round_list(d["group_delay_ms"], 4),
        "float32_deviation_db": d["float32_deviation_db"],
        "warnings": d["warnings"],
    }


def parse_range(text: str | None, default: str) -> tuple[float, float]:
    """``sub`` / ``full`` / ``LOW-HIGH`` in Hz -> (low, high); ValueError with a readable message."""
    text = (text or default).strip()
    if text in resp.VIEWS:
        lo, hi = resp.VIEWS[text]
        return float(lo), float(hi)
    lo_text, sep, hi_text = text.partition("-")
    try:
        if not sep:
            raise ValueError
        lo, hi = float(lo_text), float(hi_text)
    except ValueError:
        raise ValueError("range must be sub, full or LOW-HIGH in Hz") from None
    if not (math.isfinite(lo) and math.isfinite(hi) and 0 < lo < hi <= 24_000):
        raise ValueError(f"range {lo:g}-{hi:g} Hz must satisfy 0 < low < high <= 24000 Hz")
    return lo, hi


def parse_indexes(text: str | None, count: int, what: str) -> list[int] | None:
    """``0,2`` -> [0, 2]; ``all`` -> every index; empty -> None. ValueError when malformed or out of range."""
    if text is None or not text.strip():
        return None
    if text.strip() == "all":
        return list(range(count))
    try:
        values = [int(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise ValueError(f"{what} must be comma-separated indexes or all") from None
    bad = [v for v in values if not 0 <= v < count]
    if bad:
        raise ValueError(f"{what} {bad} outside 0..{count - 1}")
    return list(dict.fromkeys(values))


async def _fir_taps(session: DeviceSession, doc: ConfigDoc, outputs: Sequence[int]) -> dict[int, Any]:
    """Stored taps of the outputs' active FIRs (others are plotted flat, with a warning)."""
    taps: dict[int, Any] = {}
    if session.repo is None:
        return taps
    for n in outputs:
        fir = doc.outputs[n].fir
        if not fir.bypass and fir.blob_id is not None:
            blob = await session.repo.get_fir_blob(fir.blob_id)
            if blob is not None and getattr(blob, "taps", None) is not None:
                taps[n] = blob.taps
    return taps


def _trace(
    trace_id: str,
    label: str,
    color: str,
    response: resp.Response,
    kind: str,
    *,
    bold: bool = False,
    dash: list[int] | None = None,
) -> dict[str, Any]:
    """One graph.js trace. ``kind``: total, part (a contribution), channel or sum."""
    trace: dict[str, Any] = {"id": trace_id, "label": label, "color": color, "kind": kind}
    trace["response"] = compact(response)
    if bold:
        trace["bold"] = True
    if dash:
        trace["dash"] = dash
    return trace


def output_traces(
    doc: ConfigDoc,
    n: int,
    freqs: np.ndarray,
    fs: float,
    *,
    inputs: list[int] | None,
    taps: dict[int, Any],
    name: str,
    color: str,
    input_names: Sequence[str],
) -> dict[str, Any]:
    """The Response tab: output ``n``'s full chain (bold) and its contributions (thin, dashed)."""
    notes: list[str] = []
    out = doc.outputs[n]
    if out.mute:
        notes.append(f"{name} is muted; the plot shows it as if it were un-muted")
        doc = doc.model_copy(deep=True)
        doc.outputs[n].mute = False
    total = resp.output_response(doc, n, freqs, fs, inputs=inputs, fir_taps=taps)
    stages = resp.output_stages(doc, n, freqs, fs, inputs=inputs, fir_taps=taps.get(n))
    feeding = range(len(doc.inputs)) if inputs is None else inputs
    routed = [i for i in feeding if doc.inputs[i].routing[n].enabled]
    if not routed:
        notes.append(f"no selected input is routed to {name}, so it is silent")
    traces = [_trace("total", f"{name} (total)", color, total, "total", bold=True)]
    label_in = " + ".join(input_names[i] for i in routed) or "no input"

    def contribution(key: str, label: str, tf: resp.Tf) -> None:
        part = resp.Response.from_tf(tf, freqs, fs)
        traces.append(_trace(key, label, CONTRIBUTION_COLORS[key], part, "part", dash=DASH))

    contribution("input", f"Input: {label_in} (routing, input gain and PEQ)", stages["input"])
    if any(not b.bypass for b in out.peq):
        contribution("peq", "Output PEQ", stages["peq"])
    for g, group in enumerate(out.crossover):
        if not group.bypass:
            tf = resp.crossover_tf([group], freqs, fs)
            contribution(f"crossover.{g}", f"Crossover group {g + 1}: {_group_label(group)}", tf)
    if not out.fir.bypass:
        contribution("fir", "FIR", stages["fir"])
    return {"traces": traces, "warnings": list(dict.fromkeys([*total.warnings, *notes]))}


def _group_label(group: CrossoverGroup) -> str:
    if group.design is not None:
        return describe_crossover(group.design)
    return "flat" if all(bq.is_passthrough for bq in group.compiled) else "custom"


@router.get("/ui/devices/{serial}/presets/{preset}/outputs/{channel}/response")
async def output_response(
    request: Request,
    serial: int,
    preset: int,
    channel: int,
    span: Annotated[str | None, Query(alias="range")] = None,
    inputs: Annotated[str | None, Query()] = None,
):
    session = await _session(request, serial, preset)
    if isinstance(session, Response):
        return session
    if bad := _check_channel(session, "outputs", channel):
        return bad
    profile = session.profile
    try:
        lo, hi = parse_range(span, "full")
        feeding = parse_indexes(inputs, profile.n_inputs, "inputs")
    except ValueError as exc:
        return _error(422, "invalid", str(exc))
    if feeding is None:
        feeding = [0]
    doc = session.desired(preset)
    taps = await _fir_taps(session, doc, [channel])
    freqs = resp.freq_grid(lo, hi)
    input_names = [channel_label(session, "input", i) for i in range(profile.n_inputs)]
    name = channel_label(session, "output", channel)
    color = channel_color(session, "output", channel)
    result = await asyncio.to_thread(
        output_traces,
        doc,
        channel,
        freqs,
        profile.sample_rate,
        inputs=feeding,
        taps=taps,
        name=name,
        color=color,
        input_names=input_names,
    )
    return _json({"serial": serial, "preset": preset, "output": channel, "inputs": feeding, **result})


def system_traces(
    doc: ConfigDoc, outputs: list[int], freqs: np.ndarray, fs: float, *, taps: dict[int, Any], names, colors
) -> dict[str, Any]:
    """The Dashboard widget: each output (thin) and, with more than one, their electrical sum (bold). Like the
    Response tab's default, only Input 1 feeds the outputs."""
    traces = []
    warnings: list[str] = []
    for n in outputs:
        single = resp.output_response(doc, n, freqs, fs, fir_taps=taps)
        traces.append(_trace(f"o{n}", names[n], colors[n], single, "channel"))
        warnings.extend(single.warnings)
        if doc.inputs and not doc.inputs[0].routing[n].enabled:
            warnings.append(
                f"Input 1 is not routed to {names[n]}, so it is plotted as silent (this graph feeds Input 1)"
            )
    if len(outputs) > 1:
        total = resp.system_response(doc, outputs, freqs, fs, fir_taps=taps)
        traces.append(_trace("sum", "Sum", SUM_COLOR, total, "sum", bold=True))
        warnings.extend(total.warnings)
    return {"traces": traces, "warnings": list(dict.fromkeys(warnings))}


@router.get("/ui/devices/{serial}/response")
async def system_response(
    request: Request,
    serial: int,
    preset: int | None = None,
    outputs: Annotated[str | None, Query()] = None,
    span: Annotated[str | None, Query(alias="range")] = None,
):
    """Electrical response of a preset (default: the active one) for the Dashboard: the un-muted outputs by default."""
    session = await _session(request, serial, None)
    if isinstance(session, Response):
        return session
    profile = session.profile
    if preset is None:
        active = device_preset(session)
        preset = active if active is not None and 0 <= active < profile.n_presets else 0
    elif not 0 <= preset < profile.n_presets:
        return _error(404, "unknown_preset", f"There is no preset {preset + 1} on this device")
    doc = session.desired(preset)
    try:
        lo, hi = parse_range(span, "sub")
        selected = parse_indexes(outputs, profile.n_outputs, "outputs")
    except ValueError as exc:
        return _error(422, "invalid", str(exc))
    if selected is None:
        selected = [n for n, ch in enumerate(doc.outputs) if not ch.mute]
    names = [channel_label(session, "output", n) for n in range(profile.n_outputs)]
    colors = [channel_color(session, "output", n) for n in range(profile.n_outputs)]
    taps = await _fir_taps(session, doc, selected)
    freqs = resp.freq_grid(lo, hi, DASHBOARD_POINTS)
    result = await asyncio.to_thread(
        system_traces, doc, selected, freqs, profile.sample_rate, taps=taps, names=names, colors=colors
    )
    title = " + ".join(names[n] for n in selected) if selected else "no un-muted output"
    return _json({"serial": serial, "preset": preset, "outputs": selected, "title": title, **result})


__all__ = [
    "CROSSOVER_PRESETS",
    "band_source",
    "compact",
    "crossover_state",
    "parse_indexes",
    "parse_range",
    "peq_state",
    "router",
]
