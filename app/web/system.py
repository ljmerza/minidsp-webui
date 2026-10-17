"""System page (docs 05 §6): an overlay of the selected outputs with their electrical sum, and the integration panel
(phase difference of a pair of outputs, Shakers vs Sub by default, over 20-100 Hz with |Δφ| > 120° highlighted).

- ``GET /system``: the page (``?preset=`` is 1-based like the other editing pages; default: the active preset).
- ``GET /ui/devices/{serial}/presets/{p}/system``: graph.js traces plus the integration analysis, driven by
  static/js/system.js. Query: ``outputs`` (indexes, ``all`` or ``none``; default: the un-muted outputs),
  ``range`` (``sub``, ``full`` or ``LOW-HIGH``), ``inputs`` (default Input 1), ``pair`` (``a,b`` or ``none``).

Read-only: nothing here edits the preset.
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.api.v1.dsp import _fir_taps
from app.api.v1.system import (
    CANCEL_THRESHOLD_DEG,
    INTEGRATION_RANGE,
    SystemResult,
    default_pair,
    integration,
    parse_pair,
    system_view,
)
from app.dsp import response as resp
from app.sync.session import DeviceSession
from app.web.filters import SUM_COLOR, compact, parse_indexes, parse_range
from app.web.params import page_ref, preset_link
from app.web.ui_common import (
    channel_color,
    channel_label,
    edit_page_context,
    load_state,
    manager_of,
    not_found_page,
    pick_session,
    preset_tabs,
    render,
    resolve_preset,
    url,
)

router = APIRouter(include_in_schema=False)

NO_STORE = {"Cache-Control": "no-store"}
SYSTEM_POINTS = 384


def _json(content: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse(content, status_code=status_code, headers=NO_STORE)


def _error(status_code: int, error: str, detail: str) -> JSONResponse:
    return _json({"error": error, "detail": detail}, status_code)


def _names(session: DeviceSession) -> list[str]:
    return [channel_label(session, "output", n) for n in range(session.profile.n_outputs)]


def _colors(session: DeviceSession) -> list[str]:
    return [channel_color(session, "output", n) for n in range(session.profile.n_outputs)]


def traces(result: SystemResult, names: list[str], colors: list[str]) -> list[dict[str, Any]]:
    """graph.js traces: each output (thin) and, with more than one, the electrical sum (bold)."""
    out = [
        {"id": f"o{n}", "label": names[n], "color": colors[n], "kind": "channel", "response": compact(r)}
        for n, r in result.channels.items()
    ]
    if result.total is not None and len(result.channels) > 1:
        out.append(
            {
                "id": "sum",
                "label": "Electrical sum",
                "color": SUM_COLOR,
                "kind": "sum",
                "bold": True,
                "response": compact(result.total),
            }
        )
    return out


# --- page -----------------------------------------------------------------------------------------------------------


@router.get("/system", response_class=HTMLResponse, name="system_page")
async def system_page(request: Request, serial: int | None = None, preset: int | None = None) -> Response:
    session = pick_session(request, serial)
    if serial is not None and session is None:
        return not_found_page(request, serial)
    ctx = edit_page_context(request, session, "System")
    if session is None:
        return render(request, "system.html", ctx)
    error = await load_state(session)
    if error:
        ctx["state_error"] = error
        return render(request, "system.html", ctx, status_code=503)
    p = resolve_preset(session, preset)
    if p is None:
        ctx["page_error"] = f"There is no preset {preset} on this device."
        return render(request, "system.html", ctx, status_code=404)
    doc = session.desired(p)
    names, colors = _names(session), _colors(session)
    page = page_ref(request)
    pair = default_pair(names)
    ctx.update(
        preset=p,
        tabs=preset_tabs(session, p),
        preset_link=lambda q: preset_link(request, page, q),
        system_outputs=[
            {"index": n, "name": names[n], "color": colors[n], "muted": ch.mute} for n, ch in enumerate(doc.outputs)
        ],
        input_names=[channel_label(session, "input", i) for i in range(session.profile.n_inputs)],
        system_cfg={
            "url": url(request, f"/ui/devices/{session.serial}/presets/{p}/system"),
            "preset": p,
            "outputs": list(range(len(doc.outputs))),
            "muted": [n for n, ch in enumerate(doc.outputs) if ch.mute],
            "pair": list(pair) if pair else None,
            "names": names,
            "threshold": CANCEL_THRESHOLD_DEG,
            "integrationRange": list(INTEGRATION_RANGE),
        },
        integration_range=INTEGRATION_RANGE,
        threshold=CANCEL_THRESHOLD_DEG,
    )
    return render(request, "system.html", ctx)


# --- data -----------------------------------------------------------------------------------------------------------


@router.get("/ui/devices/{serial}/presets/{preset}/system")
async def system_data(
    request: Request,
    serial: int,
    preset: int,
    outputs: Annotated[str | None, Query()] = None,
    span: Annotated[str | None, Query(alias="range")] = None,
    inputs: Annotated[str | None, Query()] = None,
    pair: Annotated[str | None, Query()] = None,
) -> Response:
    session = manager_of(request).get(serial)
    if session is None:
        return _error(404, "unknown_device", f"Unknown device {serial}")
    profile = session.profile
    if not 0 <= preset < profile.n_presets:
        return _error(404, "unknown_preset", f"There is no preset {preset + 1} on this device")
    problem = await load_state(session)
    if problem:
        return _error(503, "state_unavailable", problem)
    doc = session.desired(preset)
    names, colors = _names(session), _colors(session)
    try:
        lo, hi = parse_range(span, "sub")
        feeding = parse_indexes(inputs, profile.n_inputs, "inputs") or [0]
        the_pair = parse_pair(pair, profile.n_outputs, names)
        if outputs is not None and outputs.strip() == "none":
            selected: list[int] = []
        else:
            selected = parse_indexes(outputs, profile.n_outputs, "outputs") or []
            if outputs is None or not outputs.strip():
                selected = [n for n, ch in enumerate(doc.outputs) if not ch.mute]
    except ValueError as exc:
        return _error(422, "invalid", str(exc))

    taps = await _fir_taps(session, doc, sorted(set(selected) | set(the_pair or ())))
    freqs = resp.freq_grid(lo, hi, SYSTEM_POINTS)
    fs = profile.sample_rate

    def run() -> dict[str, Any]:
        result = system_view(doc, selected, freqs, fs, names=names, inputs=feeding, fir_taps=taps)
        warnings = list(result.warnings)
        data: dict[str, Any] = {
            "serial": serial,
            "preset": preset,
            "outputs": selected,
            "inputs": feeding,
            "range": [lo, hi],
            "muted": [n for n, ch in enumerate(doc.outputs) if ch.mute],
            "traces": traces(result, names, colors),
            "integration": None,
        }
        if the_pair is not None:
            a, b = the_pair
            analysis = integration(doc, a, b, fs, names=names, inputs=feeding, fir_taps=taps)
            data["integration"] = {
                **analysis.to_dict(digits=3),
                "label_a": names[a],
                "label_b": names[b],
                "color_a": colors[a],
                "color_b": colors[b],
            }
            warnings.extend(analysis.warnings)
        data["warnings"] = list(dict.fromkeys(warnings))
        return data

    return _json(await asyncio.to_thread(run))
