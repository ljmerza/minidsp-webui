"""Phase 2 parameter pages: routing matrix, input pages, output pages (header strip + Delay tab), plus the edit,
retry, push and activate actions they use.

Edits go through `DeviceSession.edit` (validation, gain ceilings, the big-step guard) with ``source="ui"``. The
edit route is JSON in / JSON out because params.js drives it optimistically: controls update at once, the sync
badges follow the writer's ``sync`` events, and a value the server rejects is put back to the stored one. A value
that fails to *sync* is kept (the badge shows it and the sync bar offers a retry).
"""

import math
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from pydantic import BaseModel

from app.api.deps import require_same_origin
from app.daemon.client import DaemonError
from app.model.config_doc import PathError, get_path
from app.state import RevisionConflict
from app.sync.session import (
    BULK_TIMEOUT_S,
    ConfirmationRequired,
    DesiredValidationError,
    DeviceSession,
    MasterValidationError,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    StateUnavailable,
    UnknownPreset,
)
from app.sync.status import badge_statuses, status_view
from app.web import chain, shell
from app.web.templating import TEMPLATE_DIR
from app.web.ui_common import (
    EDIT_TOPICS,
    channel_color,
    channel_label,
    channel_names,
    default_label,
    device_preset,
    edit_page_context,
    is_htmx,
    load_state,
    manager_of,
    not_found_page,
    pick_session,
    preset_tabs,
    render,
    resolve_preset,
    url,
)

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_same_origin)])

MAX_EDIT_VALUES = 64
SPEED_OF_SOUND_M_S = 343.0
DEFAULT_DISTANCE_M = 3.0

# Output page tabs, in the chain's order (phase 9 design §8: the tab bar is the chain, Level first). A tab is enabled
# once its partial exists in templates/output/<key>.html, so later phases light up a tab just by adding the template.
# ``fir`` is also hidden unless the profile supports FIR. The keys are the ``?tab=`` values and never change.
OUTPUT_TABS: tuple[tuple[str, str], ...] = (
    ("level", "Level"),
    ("crossover", "Band (crossover)"),
    ("peq", "EQ"),
    ("delay", "Delay"),
    ("polarity", "Polarity"),
    ("compressor", "Compressor (limiter)"),
    ("fir", "FIR"),
    ("response", "Response"),
)
# Input page tabs (design §9): Level · EQ. The bare URL opens EQ, as it always showed the EQ editor.
INPUT_TABS: tuple[str, ...] = ("level", "peq")
INPUT_DEFAULT_TAB = "peq"


def output_tabs(session: DeviceSession) -> list[dict[str, Any]]:
    tabs = []
    for key, label in OUTPUT_TABS:
        if key == "fir" and not session.profile.fir_supported:
            continue
        tabs.append({"key": key, "label": label, "enabled": (TEMPLATE_DIR / "output" / f"{key}.html").is_file()})
    return tabs


def channel_paths(kind: str, index: int) -> list[str]:
    side = f"{kind}s.{index}"
    paths = [f"{side}.gain_db", f"{side}.mute"]
    if kind == "output":
        paths += [f"{side}.invert", f"{side}.delay_ms"]
    return paths


def simple_values(session: DeviceSession, preset: int) -> dict[str, Any]:
    """Desired values of every Phase 2 parameter of the preset (gains, mutes, polarity, delay, routing)."""
    doc = session.desired(preset)
    values: dict[str, Any] = {}
    for n, ch in enumerate(doc.inputs):
        values[f"inputs.{n}.gain_db"] = ch.gain_db
        values[f"inputs.{n}.mute"] = ch.mute
        for o, cell in enumerate(ch.routing):
            values[f"inputs.{n}.routing.{o}.enabled"] = cell.enabled
            values[f"inputs.{n}.routing.{o}.gain_db"] = cell.gain_db
    for n, ch in enumerate(doc.outputs):
        values[f"outputs.{n}.gain_db"] = ch.gain_db
        values[f"outputs.{n}.mute"] = ch.mute
        values[f"outputs.{n}.invert"] = ch.invert
        values[f"outputs.{n}.delay_ms"] = ch.delay_ms
    return values


# Routing guard (UI only: the edit API and HA/MQTT clients are not affected). Routing enables can't be read back from
# the device, so a route switched off by accident silences an output with nothing to show for it. Before a route is
# turned off, the routing page asks if that leaves the output with no enabled route while it is un-muted or shows
# signal above this level (the no-signal floor of phase9 design §5.3; the real device's floor is not measured yet).
# Mirrored by app/static/js/routing.js (LIVE_DBFS, wouldSilence).
ROUTE_LIVE_DBFS = -90.0


def output_routed(values: Mapping[str, Any], n_inputs: int, output: int, *, without: int | None = None) -> bool:
    """Whether any input (other than ``without``) has an enabled route to ``output`` in ``values`` (see
    `simple_values`)."""
    return any(bool(values.get(f"inputs.{i}.routing.{output}.enabled")) for i in range(n_inputs) if i != without)


def would_silence_output(values: Mapping[str, Any], n_inputs: int, input_index: int, output: int) -> bool:
    """Whether turning off the route ``input_index`` -> ``output`` leaves ``output`` with no enabled route."""
    return not output_routed(values, n_inputs, output, without=input_index)


def output_title(session: DeviceSession, index: int) -> str:
    """ "Out 2 (Sub)" when the output has a name in Settings, else "Out 2"."""
    default = default_label("output", index)
    name = channel_label(session, "output", index)
    return default if name == default else f"{default} ({name})"


def page_ref(request: Request) -> str:
    """The current page as path + query without ``preset``, for links that switch the edited preset."""
    query = urlencode([(k, v) for k, v in request.query_params.multi_items() if k != "preset"])
    path = request.url.path.removeprefix(request.scope.get("root_path", "").rstrip("/")) or "/"
    return path + (f"?{query}" if query else "")


def preset_link(request: Request, page: str, preset: int) -> str:
    """``page`` (path + query, see `page_ref`) showing ``preset`` (0-based; the URL is 1-based)."""
    parts = urlsplit(page)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "preset"] + [("preset", str(preset + 1))]
    root = request.scope.get("root_path", "").rstrip("/")
    return f"{root}{parts.path}?{urlencode(query)}"


def _base_ctx(request: Request, session: DeviceSession, preset: int, title: str, page_path: str) -> dict[str, Any]:
    ctx = edit_page_context(request, session, title)
    serial = session.serial
    page = page_ref(request)
    statuses = badge_statuses(session, preset)  # the status model, so the badges agree with the header (design 11.1)
    values = simple_values(session, preset)
    summary = session.sync_summary(preset)
    ctx.update(
        preset=preset,
        active_preset=device_preset(session),
        tabs=preset_tabs(session, preset),
        page_path=page_path,
        preset_link=lambda p: preset_link(request, page, p),
        statuses=statuses,
        values=values,
        summary=summary,
        failed_reasons=summary.get("failed", {}),
        needs_adoption=session.needs_adoption,
        input_names=channel_names(session, "input"),
        output_names=channel_names(session, "output"),
        output_colors=[channel_color(session, "output", o) for o in range(session.profile.n_outputs)],
        input_colors=[channel_color(session, "input", i) for i in range(session.profile.n_inputs)],
        edit_url=url(request, f"/ui/devices/{serial}/presets/{preset}/edit"),
        syncbar_url=url(request, f"/ui/devices/{serial}/presets/{preset}/syncbar", page=page),
        confirm_db=request.app.state.settings.gain_step_confirm_db,
        bar=syncbar_context(request, session, preset, page),
    )
    return ctx


async def _page_setup(
    request: Request, serial: int | None, preset_1based: int | None, title: str
) -> tuple[DeviceSession | None, int | None, dict[str, Any] | Response]:
    """Common start of every editing page: pick the session, load its state, resolve the preset."""
    session = pick_session(request, serial)
    if serial is not None and session is None:
        return None, None, not_found_page(request, serial)
    ctx = edit_page_context(request, session, title)
    if session is None:
        return None, None, ctx
    error = await load_state(session)
    if error:
        ctx["state_error"] = error
        return session, None, ctx
    preset = resolve_preset(session, preset_1based)
    if preset is None:
        ctx["page_error"] = f"There is no preset {preset_1based} on this device."
        return session, None, ctx
    return session, preset, ctx


def _error_page(request: Request, template: str, ctx: dict[str, Any] | Response) -> Response:
    if isinstance(ctx, Response):
        return ctx
    status = 404 if ctx.get("page_error") else 503 if ctx.get("state_error") else 200
    return render(request, template, ctx, status_code=status)


# --- pages ------------------------------------------------------------------------------------------------------------


@router.get("/routing", response_class=HTMLResponse, name="routing_page")
async def routing_page(request: Request, serial: int | None = None, preset: int | None = None) -> Response:
    session, p, ctx = await _page_setup(request, serial, preset, "Routing")
    if session is None or p is None:
        return _error_page(request, "routing.html", ctx)
    ctx = _base_ctx(request, session, p, "Routing", "/routing")
    n_in, n_out = session.profile.n_inputs, session.profile.n_outputs
    ctx["ceilings"] = [session.channel_ceiling("output", o) for o in range(n_out)]
    ctx["routed"] = [output_routed(ctx["values"], n_in, o) for o in range(n_out)]
    ctx["output_titles"] = [output_title(session, o) for o in range(n_out)]
    # Output levels feed the routing guard ("Out 2 currently has signal"), so this page also listens to levels.
    ctx["sse_url"] = url(request, "/api/v1/events", topics=",".join((*EDIT_TOPICS, "levels")), serial=session.serial)
    status = session.status
    ctx["initial_output_levels"] = list(status.output_levels) if status else []
    return render(request, "routing.html", ctx)


# /inputs and /outputs are the overview pages (phase 9 design §4.5, app/web/home.py); they used to redirect to the
# first channel.


def _channel_ctx(request: Request, session: DeviceSession, kind: str, index: int) -> dict[str, Any]:
    p = session.profile
    meta = session.channel_meta.get((kind, index))
    return {
        "kind": kind,
        "index": index,
        "name": channel_label(session, kind, index),
        "custom_name": meta.name if meta is not None and meta.name else "",
        "color": channel_color(session, kind, index),
        "ceiling": session.channel_ceiling(kind, index),
        "gain_min": p.gain_min_db,
        "gain_max": p.gain_max_db,
        "paths": channel_paths(kind, index),
        "name_url": url(request, f"/ui/devices/{session.serial}/channels/{kind}/{index}/name"),
    }


def _chain_ctx(request: Request, session: DeviceSession, kind: str, index: int, current: str | None) -> dict[str, Any]:
    """What the header block and the chain tab bar share on the output and input pages (design §8, §9)."""
    status = session.status
    return {
        "chain_key": f"{kind[0]}{index}",
        "chain_names": {"inputs": channel_names(session, "input"), "outputs": channel_names(session, "output")},
        "chain_levels": {
            "i": list(status.input_levels) if status else [],
            "o": list(status.output_levels) if status else [],
        },
        "tab_name": chain.TAB_NAMES.get(current or "", ""),
    }


@router.get("/inputs/{number}", response_class=HTMLResponse, name="input_page")
async def input_page(
    request: Request, number: int, serial: int | None = None, preset: int | None = None, tab: str | None = None
) -> Response:
    session, p, ctx = await _page_setup(request, serial, preset, f"In {number}")
    if session is not None and p is not None and not 1 <= number <= session.profile.n_inputs:
        ctx = edit_page_context(request, session, "Input")
        ctx["page_error"] = f"This device has no input {number}."
        p = None
    if session is None or p is None:
        return _error_page(request, "input.html", ctx)
    index = number - 1
    title = channel_label(session, "input", index)
    ctx = _base_ctx(request, session, p, title, f"/inputs/{number}")
    current = tab if tab in INPUT_TABS else INPUT_DEFAULT_TAB

    def link(key: str) -> str:
        return url(request, f"/inputs/{number}", serial=request.query_params.get("serial"), preset=p + 1, tab=key)

    summary = chain.edited_summary(session, p)
    info = summary.inputs[index] if summary is not None and index < len(summary.inputs) else None
    ctx.update(
        channel=_channel_ctx(request, session, "input", index),
        tab=current,
        tab_template=f"input/{current}.html",
        tab_link=link,
        in_summary=info,
        chips=chain.input_chips(info, chain.input_eq_label(session, p, index), link) if info is not None else [],
        **_chain_ctx(request, session, "input", index, current),
    )
    return render(request, "input.html", ctx)


@router.get("/outputs/{number}", response_class=HTMLResponse, name="output_page")
async def output_page(
    request: Request, number: int, serial: int | None = None, preset: int | None = None, tab: str | None = None
) -> Response:
    session, p, ctx = await _page_setup(request, serial, preset, f"Out {number}")
    if session is not None and p is not None and not 1 <= number <= session.profile.n_outputs:
        ctx = edit_page_context(request, session, "Output")
        ctx["page_error"] = f"This device has no output {number}."
        p = None
    if session is None or p is None:
        return _error_page(request, "output.html", ctx)
    index = number - 1
    title = channel_label(session, "output", index)
    ctx = _base_ctx(request, session, p, title, f"/outputs/{number}")
    tabs = output_tabs(session)
    enabled = [t["key"] for t in tabs if t["enabled"]]
    current = tab if tab in enabled else (enabled[0] if enabled else None)
    prof = session.profile

    def link(key: str) -> str:
        return url(request, f"/outputs/{number}", serial=request.query_params.get("serial"), preset=p + 1, tab=key)

    summary = chain.edited_summary(session, p)
    out = summary.outputs[index] if summary is not None and index < len(summary.outputs) else None
    doc_out = session.desired(p).outputs[index]
    ctx.update(
        channel=_channel_ctx(request, session, "output", index),
        output_tabs=tabs,
        tab=current,
        tab_template=f"output/{current}.html" if current else None,
        tab_link=link,
        out_summary=out,
        chips=chain.output_chips(out, tabs, link) if out is not None else [],
        fed_by=chain.fed_by(out) if out is not None else "",
        chain_reason=chain.badge_reason(out) if out is not None else None,
        level_view=chain.level_view(out, prof.gain_min_db) if out is not None else None,
        bass=chain.is_bass(doc_out, out.tactile if out is not None else False),
        louder_href=_task_link(request, "louder", number),
        lower_href=_task_link(request, "louder", number, method="others"),
        **_chain_ctx(request, session, "output", index, current),
        sample_rate=prof.sample_rate,
        delay_max_ms=prof.delay_max_ms,
        delay_max_samples=prof.delay_max_samples,
        speed_of_sound=SPEED_OF_SOUND_M_S,
        default_distance_m=DEFAULT_DISTANCE_M,
    )
    return render(request, "output.html", ctx)


def _task_link(request: Request, task_id: str, output: int, **params: str) -> str | None:
    """A guided task's link for one output (the dialog opens it), None while the wizard isn't served."""
    href = shell.task_href(request, task_id)
    if not href or not href.startswith("/tasks/"):
        return None
    return url(request, href, output=output, **params)


# --- edits ------------------------------------------------------------------------------------------------------------


class EditRequest(BaseModel):
    values: dict[str, Any]
    confirm: bool = False
    commit: bool = False


def patch_from_values(values: dict[str, Any]) -> dict[str, Any]:
    """``{"outputs.0.gain_db": 3}`` -> the merge patch ``{"outputs": {"0": {"gain_db": 3}}}`` (paths merged)."""
    for path in values:
        if not path or any(not part for part in path.split(".")):
            raise DesiredValidationError("invalid edit", ["not a document path (empty path segment)"])
    ordered = sorted(values)
    for a, b in zip(ordered, ordered[1:], strict=False):
        if b.startswith(f"{a}."):
            raise DesiredValidationError("invalid edit", [f"{b}: overlaps {a} in the same edit"])
    patch: dict[str, Any] = {}
    for path, value in values.items():
        *parents, last = path.split(".")
        node = patch
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    return patch


def _current(session: DeviceSession, preset: int, paths: list[str]) -> dict[str, Any]:
    """Desired values of ``paths`` (JSON-ready); unknown paths are left out."""
    try:
        doc = session.desired(preset)
    except (StateUnavailable, UnknownPreset):
        return {}
    out: dict[str, Any] = {}
    for path in paths:
        try:
            value = get_path(doc, path)
        except PathError:
            continue
        out[path] = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return out


def _statuses(session: DeviceSession, preset: int, paths: list[str]) -> dict[str, str]:
    try:
        status = badge_statuses(session, preset)
    except (StateUnavailable, UnknownPreset):
        return {}
    return {p: status[p] for p in paths if p in status}


def _edit_error(
    session: DeviceSession, preset: int, paths: list[str], status: int, error: str, detail: str, **extra: Any
) -> JSONResponse:
    content = {
        "error": error,
        "detail": detail,
        "values": _current(session, preset, paths),
        "sync": _statuses(session, preset, paths),
        **extra,
    }
    return JSONResponse(status_code=status, content=content, headers={"Cache-Control": "no-store"})


def _session_or_404(request: Request, serial: int) -> DeviceSession | None:
    return manager_of(request).get(serial)


@router.post("/ui/devices/{serial}/presets/{preset}/edit")
async def edit_values(request: Request, serial: int, preset: int) -> Response:
    """Set desired values by path: ``{"values": {"outputs.0.gain_db": -3}, "commit": false, "confirm": false}``.

    ``commit`` marks a commit boundary (slider release, toggle, submit); drags send ``commit=false``.
    """
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = _session_or_404(request, serial)
    if session is None:
        return JSONResponse({"error": "unknown_device", "detail": f"Unknown device {serial}"}, status_code=404)
    try:
        body = EditRequest.model_validate(await request.json())
    except Exception:
        return JSONResponse({"error": "bad_request", "detail": "expected {values: {path: value}}"}, status_code=400)
    paths = list(body.values)
    if not paths or len(paths) > MAX_EDIT_VALUES:
        return JSONResponse({"error": "bad_request", "detail": f"send 1 to {MAX_EDIT_VALUES} values"}, status_code=400)
    for value in body.values.values():
        if isinstance(value, float) and not math.isfinite(value):
            return _edit_error(session, preset, paths, 422, "invalid", "values must be finite numbers")
    try:
        result = await session.edit(
            preset, patch_from_values(body.values), confirm=body.confirm, commit=body.commit, source="ui"
        )
    except ConfirmationRequired as exc:
        from app.web.sync_ui import confirmation_text  # sync_ui builds on this module

        return _edit_error(session, preset, paths, 409, "confirmation_required", confirmation_text(session, exc))
    except UnknownPreset as exc:
        return _edit_error(session, preset, paths, 404, "unknown_preset", str(exc))
    except DesiredValidationError as exc:
        return _edit_error(session, preset, paths, 422, "invalid", "; ".join(exc.problems), problems=exc.problems)
    except MasterValidationError as exc:
        return _edit_error(session, preset, paths, 422, "invalid", str(exc))
    except RevisionConflict as exc:
        return _edit_error(session, preset, paths, 412, "revision_conflict", str(exc))
    except StateUnavailable as exc:
        return _edit_error(session, preset, paths, 503, "state_unavailable", str(exc))
    watched = list(dict.fromkeys([*paths, *result.device_paths]))
    summary = session.sync_summary(preset)
    return JSONResponse(
        {
            "preset": preset,
            "revision": result.revision,
            "changed": result.paths,
            "values": _current(session, preset, paths),
            "sync": _statuses(session, preset, watched),
            "counts": summary["counts"],
        },
        headers={"Cache-Control": "no-store"},
    )


# --- sync bar (per-page preset status, retry, push, switch & apply) --------------------------------------------------

DEVICE_PAGE = "/device"  # its sync bar is the status overview (`syncbar_response`)


def syncbar_context(request: Request, session: DeviceSession, preset: int, page: str | None) -> dict[str, Any]:
    serial = session.serial
    summary = session.sync_summary(preset)
    active = device_preset(session)
    base = f"/ui/devices/{serial}/presets/{preset}"
    # The status model (app/sync/status.py): its counts and headline state, so the bar never says "all sent" while the
    # header says "changed on device".
    view = status_view(session, preset)
    return {
        "session": session,
        "serial": serial,
        "preset": preset,
        "active_preset": active,
        "is_active": active == preset,
        "summary": summary,
        "counts": summary["counts"],
        "view": view,
        "tabs": preset_tabs(session, preset),
        "page": page,
        "preset_link": (lambda p: preset_link(request, page, p)) if page else None,
        "retry_url": url(request, f"{base}/retry", page=page),
        "push_url": url(request, f"{base}/push", page=page),
        "activate_url": url(request, f"{base}/activate", page=page),
        "syncbar_url": url(request, f"{base}/syncbar", page=page),
        "mute_wrap": request.app.state.settings.mute_wrap_bulk_apply,
    }


def _safe_page(page: str | None) -> str | None:
    """Only same-site page paths are linked from the sync bar's preset tabs."""
    if page and page.startswith("/") and not page.startswith("//") and "\\" not in page and ":" not in page:
        return page
    return None


def syncbar_response(
    request: Request,
    session: DeviceSession,
    preset: int,
    page: str | None,
    *,
    message: str | None = None,
    status_code: int = 200,
    error: str | None = None,
) -> Response:
    ctx = {"bar": syncbar_context(request, session, preset, _safe_page(page)), "message": message}
    template = "partials/syncbar.html"
    if ctx["bar"]["page"] == DEVICE_PAGE:
        # The Device & sync page shows the status overview instead (phase 9 design §11.1, app/web/sync_ui.py).
        template = "partials/sync_overview.html"
        ctx["view"] = status_view(session, preset)
    return render(request, template, ctx, status_code=status_code, error=error)


async def _preset_session(request: Request, serial: int, preset: int) -> DeviceSession | Response:
    session = _session_or_404(request, serial)
    if session is None:
        return PlainTextResponse(f"Unknown device {serial}", status_code=404)
    if not 0 <= preset < session.profile.n_presets:
        return PlainTextResponse(f"Unknown preset {preset}", status_code=404)
    error = await load_state(session)
    if error:
        return PlainTextResponse(error, status_code=503, headers={"X-Mdsp-Error": "preset state unavailable"})
    return session


@router.get("/ui/devices/{serial}/presets/{preset}/syncbar", response_class=HTMLResponse)
async def syncbar(request: Request, serial: int, preset: int, page: str | None = None) -> Response:
    session = await _preset_session(request, serial, preset)
    if isinstance(session, Response):
        return session
    return syncbar_response(request, session, preset, page)


@router.post("/ui/devices/{serial}/presets/{preset}/retry", response_class=HTMLResponse)
async def retry(request: Request, serial: int, preset: int, page: str | None = None) -> Response:
    """Retry failed paths now and cut any backoff short."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = await _preset_session(request, serial, preset)
    if isinstance(session, Response):
        return session
    session.writer.kick(preset)
    return syncbar_response(request, session, preset, page, message="Retrying.")


def _flush_message(result: Any, writes: int) -> str:
    if result.ok:
        return f"All sent ({writes} write{'s' if writes != 1 else ''})."
    if result.staged:
        return "Waiting: everything is sent when this preset plays."
    return f"Not all sent: {len(result.pending)} sending, {len(result.failed)} failed. {result.error or ''}".strip()


@router.post("/ui/devices/{serial}/presets/{preset}/push", response_class=HTMLResponse)
async def push_all(request: Request, serial: int, preset: int, page: str | None = None) -> Response:
    """Push every setting of the preset (mute-wrapped per settings). The button asks for confirmation first."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = await _preset_session(request, serial, preset)
    if isinstance(session, Response):
        return session
    before = session.writer.writes_sent
    try:
        result = await session.push_all(preset, timeout=BULK_TIMEOUT_S)
    except DesiredValidationError as exc:
        message = str(exc)
        return syncbar_response(request, session, preset, page, message=message, error=message)
    message = _flush_message(result, session.writer.writes_sent - before)
    error = None if result.ok or result.staged else message
    return syncbar_response(request, session, preset, page, message=message, error=error)


@router.post("/ui/devices/{serial}/presets/{preset}/activate", response_class=HTMLResponse)
async def activate(request: Request, serial: int, preset: int, page: str | None = None) -> Response:
    """Switch & apply now: switch to the preset, then wait for its staged edits. The page reloads on success."""
    if not is_htmx(request):
        return PlainTextResponse("HTMX request expected", status_code=400)
    session = await _preset_session(request, serial, preset)
    if isinstance(session, Response):
        return session
    try:
        await session.activate_preset(preset, source="ui")
    except (PresetSwitchInProgress, PresetSwitchTimeout, DaemonError, MasterValidationError) as exc:
        message = str(exc) or type(exc).__name__
        session.log_event("warning", f"switch & apply preset {preset + 1} failed: {message}", kind="preset")
        status = 502
        if isinstance(exc, PresetSwitchTimeout):
            status = 504
        elif isinstance(exc, PresetSwitchInProgress | MasterValidationError):
            status = 409
        return syncbar_response(request, session, preset, page, status_code=status, error=message)
    response = syncbar_response(request, session, preset, page, message=f"Preset {preset + 1} is active.")
    response.headers["HX-Refresh"] = "true"  # the whole page (header, preset tabs) changes with the active preset
    return response


__all__ = ["INPUT_TABS", "OUTPUT_TABS", "output_tabs", "patch_from_values", "router", "simple_values"]
