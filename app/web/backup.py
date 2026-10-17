"""Settings → Backup (docs 05 §9): JSON export/import, per-preset CLI scripts, database download/restore.

The partial ``settings/_backup.html`` is rendered with ``backup`` = `backup_context`. Uploads go straight to the JSON
API (``/api/v1/backup/…``) from the page's script; this module only renders the section and cancels a staged restore.
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api.deps import require_same_origin
from app.sync import backup as svc
from app.sync.session import DeviceSession
from app.web.ui_common import pick_session, render, repo_of, url

router = APIRouter(include_in_schema=False, dependencies=[Depends(require_same_origin)])

MAX_LISTED = 20


def backup_context(request: Request, session: DeviceSession | None, **extra: Any) -> dict[str, Any]:
    db_path = request.app.state.settings.db_path
    api = url(request, "/api/v1/backup")
    files = svc.list_backups(svc.default_backup_dir(db_path))
    serial = session.serial if session is not None else None
    scripts = []
    if session is not None:
        base = url(request, f"/api/v1/devices/{session.serial}/presets")
        scripts = [
            {"label": f"Preset {p + 1}", "url": f"{base}/{p}/export/cli-script?download=true"}
            for p in range(session.profile.n_presets)
        ]
    return {
        "serial": serial,
        "db_available": repo_of(request, session) is not None,
        "partial_url": url(request, "/ui/settings/backup", serial=serial),
        "cancel_url": url(request, "/ui/settings/backup/restore/cancel", serial=serial),
        "export_url": f"{api}/export?download=true",
        "db_url": f"{api}/db",
        "scripts": scripts,
        "files": [{**f.as_json(), "url": f"{api}/db/files/{f.name}"} for f in files[:MAX_LISTED]],
        "more_files": max(0, len(files) - MAX_LISTED),
        "keep": svc.KEEP_BACKUPS,
        "pending_restore": svc.pending_restore(db_path),
        "message": None,
        "config": {
            "previewUrl": f"{api}/import/preview",
            "importUrl": f"{api}/import",
            "restoreUrl": f"{api}/db/restore",
            "partialUrl": url(request, "/ui/settings/backup", serial=serial),
            "serial": serial,
            "maxJson": svc.MAX_JSON_BYTES,
            "maxDb": svc.MAX_DB_BYTES,
        },
        **extra,
    }


def _render(request: Request, serial: int | None, **extra: Any) -> HTMLResponse:
    session = pick_session(request, serial)
    return render(request, "settings/_backup.html", {"backup": backup_context(request, session, **extra)})


@router.get("/ui/settings/backup", response_class=HTMLResponse)
async def backup_partial(request: Request, serial: int | None = None) -> HTMLResponse:
    return _render(request, serial)


@router.post("/ui/settings/backup/restore/cancel", response_class=HTMLResponse)
async def cancel_restore(request: Request, serial: int | None = None) -> HTMLResponse:
    cancelled = svc.cancel_restore(request.app.state.settings.db_path)
    return _render(request, serial, message="Restore cancelled." if cancelled else "No restore was staged.")
