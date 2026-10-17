"""Backups (docs 05 §9, docs 07 "Backups"): JSON export/import of the whole setup, and SQLite database backups.

Uploads (a JSON backup, a database file) are sent as the raw request body, not multipart: they are read into memory
with a size cap while they stream in, so nothing is spooled to a temporary file (docs 01, SD card rule 6).
Per-preset ``minidsp -f`` scripts are exported by ``GET /devices/{serial}/presets/{p}/export/cli-script``.
"""

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from app.api.deps import refresh_mqtt_after_write
from app.config import Settings
from app.state import Repo
from app.sync import backup as svc
from app.sync.session import SessionManager

router = APIRouter(prefix="/backup", tags=["backup"], dependencies=[Depends(refresh_mqtt_after_write)])

SQLITE_MEDIA_TYPE = "application/vnd.sqlite3"


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _repo(request: Request) -> Repo:
    repo = getattr(request.app.state, "repo", None)
    if repo is None:
        raise HTTPException(status_code=503, detail="the state database is not available")
    return repo


def _manager(request: Request) -> SessionManager | None:
    return getattr(request.app.state, "manager", None)


def _problem(exc: svc.BackupError, status_code: int = 422) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": str(exc), "problems": exc.problems})


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, refused with 413 as soon as it is known to exceed ``limit`` bytes."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(status_code=413, detail=f"request body too large (at most {limit} bytes)")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(status_code=413, detail=f"request body too large (at most {limit} bytes)")
        chunks.append(chunk)
    return b"".join(chunks)


async def save_body(request: Request, path: Path, limit: int) -> None:
    """Stream the request body into ``path``, refused with 413 as soon as it is known to exceed ``limit`` bytes."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise HTTPException(status_code=413, detail=f"request body too large (at most {limit} bytes)")
    size = 0
    with open(path, "wb") as fh:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(status_code=413, detail=f"request body too large (at most {limit} bytes)")
            await asyncio.to_thread(fh.write, chunk)
        fh.flush()
        await asyncio.to_thread(os.fsync, fh.fileno())


def _attachment(filename: str) -> dict[str, str]:
    return {"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"}


# --- JSON export / import ---------------------------------------------------------------------------------------------


@router.get("/export")
async def export_json(
    request: Request,
    download: Annotated[bool, Query(description="Send as a file attachment")] = False,
) -> JSONResponse:
    """Every preset (desired + applied shadow), channel names/colours/ceilings, profiles, FIR blobs and settings."""
    data = await svc.build_export(_repo(request), manager=_manager(request))
    headers = _attachment(svc.export_filename()) if download else {"Cache-Control": "no-store"}
    return JSONResponse(data, headers=headers)


ImportQuery = Annotated[
    str | None,
    Query(description=f"Comma-separated parts to import (default all): {', '.join(svc.PARTS)}"),
]


async def _plan(
    request: Request,
    include: str | None,
    target_serial: int | None,
    overwrite_profiles: bool,
    restore_shadow: bool,
) -> svc.ImportPlan:
    repo = _repo(request)
    options = svc.ImportOptions.build(
        include=include.split(",") if include is not None else None,
        target_serial=target_serial,
        overwrite_profiles=overwrite_profiles,
        restore_shadow=restore_shadow,
    )
    backup = await asyncio.to_thread(svc.parse_backup, await read_body(request, svc.MAX_JSON_BYTES))
    return await svc.plan_import(
        backup, repo=repo, manager=_manager(request), settings=_settings(request), options=options
    )


@router.post("/import/preview")
async def preview_import(
    request: Request,
    include: ImportQuery = None,
    target_serial: Annotated[int | None, Query(description="Import the backup's only device as this one")] = None,
    overwrite_profiles: bool = False,
    restore_shadow: bool = False,
) -> Any:
    """Validate a JSON backup (the request body) and report what importing it would change. Writes nothing."""
    try:
        plan = await _plan(request, include, target_serial, overwrite_profiles, restore_shadow)
    except svc.BackupError as exc:
        return _problem(exc)
    return plan.as_json()


@router.post("/import")
async def import_json(
    request: Request,
    include: ImportQuery = None,
    target_serial: Annotated[int | None, Query(description="Import the backup's only device as this one")] = None,
    overwrite_profiles: bool = False,
    restore_shadow: Annotated[
        bool,
        Query(description="For presets this app has no state for: trust the backup's record of the device"),
    ] = False,
    confirm: Annotated[bool, Query(description="Required: an import is a bulk apply to the device")] = False,
) -> Any:
    """Import a JSON backup (the request body). Live devices get each preset as a mute-wrapped bulk apply (the
    active preset is written, the others staged); offline devices get it stored for later."""
    try:
        plan = await _plan(request, include, target_serial, overwrite_profiles, restore_shadow)
    except svc.BackupError as exc:
        return _problem(exc)
    if not plan.ok:
        return JSONResponse(
            status_code=422,
            content={"detail": "the backup can't be imported", "problems": plan.problems, "plan": plan.as_json()},
        )
    if not confirm:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "importing a backup replaces preset settings on the device; repeat with confirm",
                "error": "confirmation_required",
                "plan": plan.as_json(),
            },
        )
    report = await svc.apply_import(plan, repo=_repo(request), manager=_manager(request), settings=_settings(request))
    return report.as_json()


# --- SQLite backups ---------------------------------------------------------------------------------------------------


@router.get("/db", response_class=Response)
async def download_db(request: Request) -> Response:
    """A consistent copy of the live database (SQLite backup API)."""
    _repo(request)
    try:
        data = await asyncio.to_thread(svc.snapshot, _settings(request).db_path)
    except svc.BackupError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    name = svc.backup_name(svc.BACKUP_PREFIX, datetime.now(UTC))
    return Response(data, media_type=SQLITE_MEDIA_TYPE, headers=_attachment(name))


@router.get("/db/files")
async def list_db_backups(request: Request) -> dict[str, Any]:
    """The nightly backups (and copies kept from before a restore), newest first, and any staged restore."""
    db_path = _settings(request).db_path
    files = await asyncio.to_thread(svc.list_backups, svc.default_backup_dir(db_path))
    return {
        "files": [f.as_json() for f in files],
        "keep": svc.KEEP_BACKUPS,
        "pending_restore": svc.pending_restore(db_path),
    }


@router.get("/db/files/{name}", response_class=FileResponse)
async def download_db_backup(request: Request, name: str) -> FileResponse:
    path = svc.backup_file(svc.default_backup_dir(_settings(request).db_path), name)
    if path is None:
        raise HTTPException(status_code=404, detail=f"no backup named {name!r}")
    return FileResponse(path, media_type=SQLITE_MEDIA_TYPE, filename=name, headers={"Cache-Control": "no-store"})


@router.post("/db/restore")
async def restore_db(
    request: Request,
    dry_run: Annotated[bool, Query(description="Only validate the file")] = False,
) -> Any:
    """Validate a database file (the raw request body) and stage it to replace the database at the next app start.
    The replaced database is kept in the backup directory. Works even when the current database can't be opened."""
    db_path = _settings(request).db_path
    upload = svc.upload_path(db_path)
    try:
        await save_body(request, upload, svc.MAX_DB_BYTES)  # streamed to disk: a database can be tens of MB
        info = await asyncio.to_thread(svc.stage_restore_file, db_path, upload, dry_run=dry_run)
    except svc.BackupError as exc:
        return _problem(exc)
    finally:
        upload.unlink(missing_ok=True)
    repo = getattr(request.app.state, "repo", None)
    if not dry_run and repo is not None:
        await repo.log_event(
            "backup", "database restore staged; it takes effect when the app restarts", level="warning"
        )
    return {**info.as_json(), "staged": not dry_run, "restart_required": not dry_run}


@router.delete("/db/restore")
async def cancel_db_restore(request: Request) -> dict[str, bool]:
    return {"cancelled": svc.cancel_restore(_settings(request).db_path)}
