"""Backups (docs 05 §7/§9, docs 07 "Backups"): the full-fidelity JSON export/import and SQLite file backups.

JSON export
    Every device's presets (desired document, applied shadow, unknown paths, revision), channel names/colours/ceilings,
    profiles, FIR blobs and the stored settings, as one document (`FORMAT`, `EXPORT_VERSION`). Revision history and
    the event log are not part of it; the SQLite backup has those.

JSON import
    `parse_backup` validates the file's shape, `plan_import` checks it against this app (device models, ranges, gain
    ceilings, FIR references, profile name clashes) without writing anything, and `apply_import` applies a clean plan:

    - A device with a live session gets each changed preset through ``DeviceSession.replace_desired``: the same
      validation and mute-wrapped bulk apply as a profile load. The active preset is written, the others are staged.
      The session's own shadow decides what is written, so nothing is assumed to be on the device, and settings it
      has no confirmed value for are written as well, so the preset ends up holding exactly the backup.
    - The backup's shadow is used only with ``restore_shadow``, only for presets this app has no state for (a
      reinstall), only when the backup shows the preset fully synced, and never when the import is re-targeted to
      another device (its shadow describes different hardware). A live session then adopts the preset ("trust"),
      an offline device gets the shadow stored; otherwise only the desired state is used (pushed / nothing known).
      Trusting a stale or foreign shadow would let the writer un-bypass coefficients the device never received.
    - An offline device (no session) gets its presets committed to the database; its session loads them later.

SQLite backups
    `snapshot` copies the live database with the SQLite backup API through a second connection (WAL readers don't
    block the app), entirely in memory. The nightly job (`nightly_backup_loop`) writes one only when the content
    changed since the newest backup (the snapshot of an unchanged database is byte-identical) and keeps
    `KEEP_BACKUPS`. A restore is validated and staged next to the database; `apply_staged_restore` swaps it in at the
    next start, before the database is opened, keeping a copy of the replaced database.
"""

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app import __version__
from app.config import Settings
from app.model.config_doc import ConfigDoc, default_config_doc, diff_paths, validate_doc
from app.model.device_profile import PROFILES, DeviceProfile, profile_for
from app.model.payload import device_values, same_value
from app.state import Repo
from app.state.db import MIGRATIONS_DIR, discover_migrations, utcnow_iso
from app.state.repo import UNSET
from app.sync import fir as fir_svc
from app.sync import profiles as profiles_svc
from app.sync.session import (
    BULK_TIMEOUT_S,
    PENDING_KEY,
    WRAP_KEY,
    DeviceSession,
    MasterValidationError,
    PresetSwitchInProgress,
    RevisionConflict,
    SessionManager,
    StateUnavailable,
    flush_json,
)

log = logging.getLogger(__name__)

FORMAT = "minidsp-webui-backup"
EXPORT_VERSION = 1

PARTS = ("presets", "channels", "profiles", "settings")
IMPORT_SUMMARY = "restore from a JSON backup"

MAX_JSON_BYTES = 16 << 20
MAX_DB_BYTES = 64 << 20

KEEP_BACKUPS = 14
KEEP_PRE_RESTORE = 3
NIGHTLY_AT = time(3, 30)  # local time
BACKUP_PREFIX = "minidsp-webui-"
PRE_RESTORE_PREFIX = "pre-restore-"
BACKUP_SUFFIX = ".sqlite3"
BACKUP_NAME = re.compile(r"^(minidsp-webui|pre-restore)-\d{8}T\d{6}Z\.sqlite3$")

SQLITE_MAGIC = b"SQLite format 3\x00"
REQUIRED_TABLES = frozenset(
    {"schema_migrations", "device", "preset_state", "revision", "profile", "fir_blob", "channel_meta", "setting"}
)

# Settings with live effects; everything else is stored verbatim.
RUNTIME_KEY = "ui.runtime_settings"
ADOPTION_PREFIX = "adopted."


class BackupError(ValueError):
    """A backup file (JSON or SQLite) is unusable, or an import can't proceed. ``problems`` lists every reason."""

    def __init__(self, message: str, problems: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems)


# --- JSON schema ----------------------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PresetEntry(_Strict):
    preset: int = Field(ge=0, le=15)
    revision: int = Field(default=0, ge=0)
    desired: dict[str, Any]
    applied: dict[str, Any] | None = None  # the shadow as stored; None = nothing known
    unknown_paths: list[str] = Field(default_factory=list)


class ChannelEntry(_Strict):
    kind: Literal["input", "output"]
    idx: int = Field(ge=0, le=63)
    name: str | None = Field(default=None, max_length=64)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    max_gain_db: float | None = None


class DeviceEntry(_Strict):
    serial: int = Field(ge=0)
    product_name: str | None = None
    hw_id: int | None = None
    dsp_version: int | None = None
    fw: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    presets: list[PresetEntry] = Field(default_factory=list, max_length=16)
    channels: list[ChannelEntry] = Field(default_factory=list, max_length=64)


class ProfileEntry(_Strict):
    id: int | None = None  # the id in the exporting database, to translate the "loaded into" record
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    product_name: str | None = None
    config: dict[str, Any]
    created_at: str | None = None
    updated_at: str | None = None


class FirEntry(_Strict):
    id: str = Field(pattern=r"^[0-9a-f]{16}$")
    name: str | None = None
    n_taps: int = Field(ge=1, le=1 << 16)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    taps: str = Field(description="base64 of little-endian float64 taps")
    created_at: str | None = None

    def decode(self) -> np.ndarray:
        """The taps, after checking length, hash, id and finiteness against the entry. Raises ValueError."""
        try:
            data = base64.b64decode(self.taps, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("taps are not valid base64") from None
        if len(data) != self.n_taps * 8:
            raise ValueError(f"taps hold {len(data) // 8} values, n_taps says {self.n_taps}")
        sha = hashlib.sha256(data).hexdigest()
        if sha != self.sha256 or sha[:16] != self.id:
            raise ValueError("taps don't match the sha256/id")
        taps = np.frombuffer(data, dtype="<f8").astype(np.float64)
        if not np.all(np.isfinite(taps)):
            raise ValueError("taps must be finite")
        return taps


class BackupDoc(_Strict):
    format: Literal["minidsp-webui-backup"]
    version: int = Field(ge=1)
    app_version: str | None = None
    exported_at: str | None = None
    db_schema_version: int | None = None
    devices: list[DeviceEntry] = Field(default_factory=list, max_length=16)
    profiles: list[ProfileEntry] = Field(default_factory=list, max_length=1000)
    fir_blobs: list[FirEntry] = Field(default_factory=list, max_length=256)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("version")
    @classmethod
    def _known_version(cls, version: int) -> int:
        if version > EXPORT_VERSION:
            raise ValueError(f"backup format version {version} is newer than this app understands ({EXPORT_VERSION})")
        return version


def _loc(loc: Iterable[Any]) -> str:
    return ".".join(str(part) for part in loc)


def validation_problems(exc: ValidationError, prefix: str = "") -> list[str]:
    problems = []
    for err in exc.errors():
        where = _loc(err["loc"])
        where = f"{prefix}.{where}" if prefix and where else prefix or where
        problems.append(f"{where}: {err['msg']}" if where else err["msg"])
    return problems


def parse_doc_entry(data: Mapping[str, Any], where: str) -> tuple[ConfigDoc | None, list[str]]:
    try:
        return ConfigDoc.model_validate(data), []
    except ValidationError as exc:
        return None, validation_problems(exc, where)


def parse_backup(raw: bytes | str | Mapping[str, Any]) -> BackupDoc:
    """Parse and check a JSON backup. Raises `BackupError` listing every problem found."""
    if isinstance(raw, bytes | str):
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise BackupError(f"not a JSON file: {exc}") from None
    else:
        data = raw
    if not isinstance(data, dict):
        raise BackupError("not a minidsp-webui backup: expected a JSON object")
    if data.get("format") != FORMAT:
        raise BackupError(f"not a minidsp-webui backup (format is {data.get('format')!r}, expected {FORMAT!r})")
    try:
        backup = BackupDoc.model_validate(data)
    except ValidationError as exc:
        raise BackupError("the backup file is malformed", validation_problems(exc)) from None
    problems = check_backup(backup)
    if problems:
        raise BackupError("the backup file is inconsistent", problems)
    return backup


def check_backup(backup: BackupDoc) -> list[str]:
    """Consistency checks that don't depend on this app's state: duplicates, FIR blobs, document shapes."""
    problems: list[str] = []
    serials: set[int] = set()
    for d, device in enumerate(backup.devices):
        where = f"devices.{d}"
        if device.serial in serials:
            problems.append(f"{where}: device {device.serial} appears twice")
        serials.add(device.serial)
        presets: set[int] = set()
        for k, entry in enumerate(device.presets):
            if entry.preset in presets:
                problems.append(f"{where}.presets.{k}: preset {entry.preset + 1} appears twice")
            presets.add(entry.preset)
            problems += parse_doc_entry(entry.desired, f"{where}.presets.{k}.desired")[1]
        channels: set[tuple[str, int]] = set()
        for k, channel in enumerate(device.channels):
            if (channel.kind, channel.idx) in channels:
                problems.append(f"{where}.channels.{k}: {channel.kind} {channel.idx + 1} appears twice")
            channels.add((channel.kind, channel.idx))
    names: set[str] = set()
    for k, profile in enumerate(backup.profiles):
        name = profile.name.strip()
        if not name:
            problems.append(f"profiles.{k}.name: must not be empty")
        elif name in names:
            problems.append(f"profiles.{k}: profile {name!r} appears twice")
        names.add(name)
        problems += parse_doc_entry(profile.config, f"profiles.{k}.config")[1]
    blob_ids: set[str] = set()
    for k, blob in enumerate(backup.fir_blobs):
        if blob.id in blob_ids:
            problems.append(f"fir_blobs.{k}: blob {blob.id} appears twice")
        blob_ids.add(blob.id)
        try:
            blob.decode()
        except ValueError as exc:
            problems.append(f"fir_blobs.{k}: {exc}")
    for key in backup.settings:
        if not key or len(key) > 200:
            problems.append(f"settings: bad key {key[:40]!r}")
    return problems


# --- JSON export ----------------------------------------------------------------------------------------------------


async def build_export(repo: Repo, *, manager: SessionManager | None = None) -> dict[str, Any]:
    """Everything in the database that describes the setup, as a `BackupDoc`-shaped dict (stored JSON verbatim).

    Preset state comes from the database, which trails the live sessions by at most the persist debounce (~1 s).
    """
    db = repo.db
    devices = {d.serial: d for d in await repo.list_devices()}
    extra = await db.fetchall("SELECT serial FROM preset_state UNION SELECT serial FROM channel_meta")
    serials = sorted(set(devices) | {row["serial"] for row in extra})
    migrations = await db.applied_migrations()
    out_devices = []
    for serial in serials:
        device = devices.get(serial)
        session = manager.get(serial) if manager is not None else None
        product = device.product_name if device else session.profile.product_name if session else None
        entry: dict[str, Any] = {
            "serial": serial,
            "product_name": product,
            "hw_id": device.hw_id if device else None,
            "dsp_version": device.dsp_version if device else None,
            "fw": device.fw if device else None,
            "first_seen": device.first_seen if device else None,
            "last_seen": device.last_seen if device else None,
            "presets": [
                {
                    "preset": state.preset,
                    "revision": state.revision,
                    "desired": state.desired,
                    "applied": state.applied,
                    "unknown_paths": state.unknown_paths,
                }
                for state in await repo.list_preset_states(serial)
            ],
            "channels": [
                {"kind": m.kind, "idx": m.idx, "name": m.name, "color": m.color, "max_gain_db": m.max_gain_db}
                for m in await repo.list_channel_meta(serial)
            ],
        }
        out_devices.append(entry)
    blobs = []
    for info in await repo.list_fir_blobs():
        blob = await repo.get_fir_blob(info.id)
        if blob is None or blob.taps is None:
            continue
        blobs.append(
            {
                "id": blob.id,
                "name": blob.name,
                "n_taps": blob.n_taps,
                "sha256": blob.sha256,
                "taps": base64.b64encode(blob.taps.astype("<f8").tobytes()).decode("ascii"),
                "created_at": blob.created_at,
            }
        )
    return {
        "format": FORMAT,
        "version": EXPORT_VERSION,
        "app_version": __version__,
        "exported_at": utcnow_iso(),
        "db_schema_version": max(migrations, default=None),
        "devices": out_devices,
        "profiles": [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "product_name": p.product_name,
                "config": p.config,
                "created_at": p.created_at,
                "updated_at": p.updated_at,
            }
            for p in await repo.list_profiles()
        ],
        "fir_blobs": blobs,
        "settings": await repo.all_settings(),
    }


def export_filename(now: datetime | None = None, suffix: str = ".json") -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{BACKUP_PREFIX}backup-{stamp}{suffix}"


# --- JSON import: plan ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportOptions:
    include: frozenset[str] = frozenset(PARTS)
    target_serial: int | None = None  # import the backup's only device as this one (e.g. replaced hardware)
    overwrite_profiles: bool = False
    restore_shadow: bool = False

    @classmethod
    def build(
        cls,
        *,
        include: Iterable[str] | None = None,
        target_serial: int | None = None,
        overwrite_profiles: bool = False,
        restore_shadow: bool = False,
    ) -> "ImportOptions":
        parts = frozenset(p.strip() for p in include if p.strip()) if include is not None else frozenset(PARTS)
        unknown = sorted(parts - set(PARTS))
        if unknown:
            raise BackupError(f"unknown part(s) {', '.join(unknown)}; expected some of {', '.join(PARTS)}")
        return cls(parts, target_serial, overwrite_profiles, restore_shadow)


@dataclass
class PresetPlan:
    preset: int
    doc: ConfigDoc
    entry: PresetEntry
    changes: int
    active: bool
    stored: bool  # this app already has state for the preset
    unknown: int = 0  # live session: settings with no confirmed device value (written by the import as well)


@dataclass
class DevicePlan:
    source_serial: int
    serial: int
    entry: DeviceEntry
    profile: DeviceProfile
    session: DeviceSession | None
    known: bool  # the device table has it
    restore_shadow: bool = False  # the backup's shadow may be used (``restore_shadow`` and not re-targeted)
    presets: list[PresetPlan] = field(default_factory=list)
    channels: list[ChannelEntry] = field(default_factory=list)  # entries that differ from what is stored

    def as_json(self) -> dict[str, Any]:
        return {
            "serial": self.serial,
            "source_serial": self.source_serial,
            "product_name": self.profile.product_name,
            "live": self.session is not None,
            "presets": [
                {"preset": p.preset, "changes": p.changes, "active": p.active, "stored": p.stored, "unknown": p.unknown}
                for p in self.presets
            ],
            "channel_changes": len(self.channels),
        }


@dataclass
class ImportPlan:
    backup: BackupDoc
    options: ImportOptions
    devices: list[DevicePlan] = field(default_factory=list)
    profiles_new: list[ProfileEntry] = field(default_factory=list)
    profiles_overwrite: list[ProfileEntry] = field(default_factory=list)
    profiles_skip: list[ProfileEntry] = field(default_factory=list)
    fir_new: list[FirEntry] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)  # (re-keyed) settings that differ from what is stored
    loaded: dict[str, Any] | None = None  # the backup's "profiles.loaded" record, translated after the profiles
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exported_at": self.backup.exported_at,
            "app_version": self.backup.app_version,
            "include": sorted(self.options.include),
            "devices": [d.as_json() for d in self.devices],
            "profiles": {
                "new": [p.name for p in self.profiles_new],
                "overwrite": [p.name for p in self.profiles_overwrite],
                "skip": [p.name for p in self.profiles_skip],
            },
            "fir_blobs": {"new": len(self.fir_new), "total": len(self.backup.fir_blobs)},
            "settings": sorted([*self.settings, *([profiles_svc.LOADED_KEY] if self.loaded else [])]),
            "problems": self.problems,
            "warnings": self.warnings,
        }


def _target_profile(entry: DeviceEntry, session: DeviceSession | None, settings: Settings) -> DeviceProfile | None:
    if session is not None:
        return session.profile
    return profile_for(
        entry.product_name,
        gain_min_db=settings.gain_min_db,
        gain_max_db=settings.gain_max_db,
        fir_supported=True if fir_svc.fir_experimental(settings) else None,
    )


def _fir_refs(doc: ConfigDoc) -> set[str]:
    return {ch.fir.blob_id for ch in doc.outputs if ch.fir.blob_id}


def fully_synced(entry: PresetEntry, doc: ConfigDoc, profile: DeviceProfile) -> bool:
    """Whether the backup's shadow says the device held exactly ``doc`` (nothing pending, wrapped or unknown)."""
    applied = entry.applied
    if not applied or entry.unknown_paths or applied.get(PENDING_KEY) or applied.get(WRAP_KEY):
        return False
    values = device_values(doc, profile)
    return all(path in applied and same_value(path, applied[path], value, profile) for path, value in values.items())


async def _current_doc(
    repo: Repo, serial: int, preset: int, session: DeviceSession | None, profile: DeviceProfile
) -> tuple[ConfigDoc, bool]:
    if session is not None:
        doc = await session.get_desired(preset)
        return doc, session.revisions.get(preset, 0) > 0
    state = await repo.get_preset_state(serial, preset)
    if state is None:
        return default_config_doc(profile), False
    doc, _ = parse_doc_entry(state.desired, "")
    return doc or default_config_doc(profile), True


async def _plan_device(
    plan: ImportPlan,
    entry: DeviceEntry,
    serial: int,
    *,
    repo: Repo,
    manager: SessionManager | None,
    settings: Settings,
    blob_ids: set[str],
) -> None:
    where = f"device {serial}"
    session = manager.get(serial) if manager is not None else None
    profile = _target_profile(entry, session, settings)
    if profile is None:
        plan.problems.append(f"{where}: unsupported model {entry.product_name!r}")
        return
    if entry.product_name and entry.product_name != profile.product_name:
        plan.problems.append(
            f"{where}: the backup is for a {entry.product_name}, the device is a {profile.product_name}"
        )
        return
    if session is not None:
        try:
            await session.ensure_loaded()
        except StateUnavailable as exc:
            plan.problems.append(f"{where}: {exc}")
            return
    device = DevicePlan(
        entry.serial,
        serial,
        entry,
        profile,
        session,
        await repo.get_device(serial) is not None,
        restore_shadow=plan.options.restore_shadow and entry.serial == serial,
    )
    include = plan.options.include
    if "presets" in include:
        master = session.master if session is not None else None
        for preset_entry in entry.presets:
            label = f"{where} preset {preset_entry.preset + 1}"
            if preset_entry.preset >= profile.n_presets:
                plan.problems.append(f"{label}: the {profile.product_name} has {profile.n_presets} presets")
                continue
            doc, problems = parse_doc_entry(preset_entry.desired, label)
            if doc is None:
                plan.problems += problems
                continue
            plan.problems += [f"{label}: {p}" for p in validate_doc(doc, profile)]
            plan.problems += [
                f"{label}: FIR blob {b} is neither in the backup nor stored" for b in _fir_refs(doc) - blob_ids
            ]
            current, stored = await _current_doc(repo, serial, preset_entry.preset, session, profile)
            unknown = session.sync_summary(preset_entry.preset)["counts"].get("unknown", 0) if session else 0
            device.presets.append(
                PresetPlan(
                    preset=preset_entry.preset,
                    doc=doc,
                    entry=preset_entry,
                    changes=len(diff_paths(current, doc)),
                    active=master is not None and master.preset == preset_entry.preset,
                    stored=stored,
                    unknown=unknown,
                )
            )
    if "channels" in include:
        counts = {"input": profile.n_inputs, "output": profile.n_outputs}
        if session is not None:
            current_meta = dict(session.channel_meta)
        else:
            current_meta = {(m.kind, m.idx): m for m in await repo.list_channel_meta(serial)}
        for channel in entry.channels:
            label = f"{where} {channel.kind} {channel.idx + 1}"
            if channel.idx >= counts[channel.kind]:
                plan.problems.append(f"{label}: the {profile.product_name} has {counts[channel.kind]} {channel.kind}s")
                continue
            ceiling = channel.max_gain_db
            if ceiling is not None and not profile.gain_min_db <= ceiling <= profile.gain_max_db:
                plan.problems.append(
                    f"{label}: gain ceiling {ceiling:g} dB outside {profile.gain_min_db:g}..{profile.gain_max_db:g}"
                )
                continue
            old = current_meta.get((channel.kind, channel.idx))
            name = (channel.name or "").strip() or None
            if old is None or (old.name, old.color, old.max_gain_db) != (name, channel.color, ceiling):
                device.channels.append(channel)
    plan.devices.append(device)


async def _plan_settings(plan: ImportPlan, repo: Repo, settings: Settings) -> None:
    from app.web.settings_ui import RuntimeSettings  # the Settings page owns this schema; imported late (layering)

    stored = await repo.all_settings()
    remap = plan.options.target_serial is not None and len(plan.backup.devices) == 1
    source = plan.backup.devices[0].serial if remap else None
    for key, value in plan.backup.settings.items():
        if key == profiles_svc.LOADED_KEY:
            plan.loaded = value if isinstance(value, dict) and value else None  # translated by _apply_loaded
            continue
        if remap and key == f"{ADOPTION_PREFIX}{source}":
            key = f"{ADOPTION_PREFIX}{plan.options.target_serial}"
        elif key.startswith(ADOPTION_PREFIX) and remap:
            continue  # another device's adoption record; the backup is being re-targeted
        if key == RUNTIME_KEY:
            try:
                RuntimeSettings.model_validate({**RuntimeSettings.from_settings(settings).model_dump(), **value})
            except (ValidationError, TypeError) as exc:
                plan.problems.append(f"settings.{key}: {exc}")
                continue
        elif key == fir_svc.FIR_SETTING_KEY and not isinstance(value, bool):
            plan.problems.append(f"settings.{key}: expected true or false")
            continue
        if stored.get(key, UNSET) != value:
            plan.settings[key] = value


async def plan_import(
    backup: BackupDoc,
    *,
    repo: Repo,
    manager: SessionManager | None,
    settings: Settings,
    options: ImportOptions | None = None,
) -> ImportPlan:
    """Check a parsed backup against this app and work out what an import would change. Writes nothing."""
    options = options or ImportOptions()
    plan = ImportPlan(backup, options)
    include = options.include
    stored_blobs = {b.id for b in await repo.list_fir_blobs()}
    if include & {"presets", "profiles"}:  # FIR blobs come with whatever references them
        plan.fir_new = [b for b in backup.fir_blobs if b.id not in stored_blobs]
    blob_ids = stored_blobs | {b.id for b in backup.fir_blobs}

    if include & {"presets", "channels"} and backup.devices:
        if options.target_serial is not None and len(backup.devices) != 1:
            plan.problems.append(
                f"the backup holds {len(backup.devices)} devices; a target device needs a backup of exactly one"
            )
        else:
            for entry in backup.devices:
                serial = options.target_serial if options.target_serial is not None else entry.serial
                await _plan_device(
                    plan, entry, serial, repo=repo, manager=manager, settings=settings, blob_ids=blob_ids
                )
    if "profiles" in include:
        for entry in backup.profiles:
            name = entry.name.strip()
            doc, problems = parse_doc_entry(entry.config, f"profile {name!r}")
            plan.problems += problems
            model = PROFILES.get(entry.product_name or "")
            if doc is not None and model is not None:
                limits = profile_for(
                    model.product_name, gain_min_db=settings.gain_min_db, gain_max_db=settings.gain_max_db
                )
                plan.problems += [f"profile {name!r}: {p}" for p in validate_doc(doc, limits or model)]
            if doc is not None:
                plan.problems += [
                    f"profile {name!r}: FIR blob {b} is neither in the backup nor stored"
                    for b in _fir_refs(doc) - blob_ids
                ]
            existing = await repo.get_profile_by_name(name)
            if existing is None:
                plan.profiles_new.append(entry)
            elif existing.config == entry.config and (existing.description, existing.product_name) == (
                entry.description,
                entry.product_name,
            ):
                continue  # identical: nothing to do
            elif options.overwrite_profiles:
                plan.profiles_overwrite.append(entry)
            else:
                plan.profiles_skip.append(entry)
    if "settings" in include:
        await _plan_settings(plan, repo, settings)
    if options.restore_shadow and "presets" in include:
        for device in plan.devices:
            if not device.restore_shadow:
                plan.warnings.append(
                    f"device {device.serial}: the backup's record of what is on the device belongs to device "
                    f"{device.source_serial}, so it is not used"
                )
                continue
            for p in device.presets:
                if not p.stored and not fully_synced(p.entry, p.doc, device.profile):
                    what = "pushed" if device.session is not None else "stored with nothing assumed on the device"
                    plan.warnings.append(
                        f"device {device.serial} preset {p.preset + 1}: the backup doesn't show it fully synced, "
                        f"so it is {what} instead of trusted"
                    )
    return plan


# --- JSON import: apply -----------------------------------------------------------------------------------------------


@dataclass
class ImportReport:
    presets: list[dict[str, Any]] = field(default_factory=list)
    channels: int = 0
    profiles_created: list[str] = field(default_factory=list)
    profiles_updated: list[str] = field(default_factory=list)
    profiles_skipped: list[str] = field(default_factory=list)
    fir_blobs: int = 0
    settings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "presets": self.presets,
            "channels": self.channels,
            "profiles": {
                "created": self.profiles_created,
                "updated": self.profiles_updated,
                "skipped": self.profiles_skipped,
            },
            "fir_blobs": self.fir_blobs,
            "settings": self.settings,
            "errors": self.errors,
            "warnings": self.warnings,
        }


_PRESET_ERRORS = (MasterValidationError, StateUnavailable, PresetSwitchInProgress, RevisionConflict, ValueError)


async def _apply_settings(
    plan: ImportPlan, report: ImportReport, *, repo: Repo, manager: SessionManager | None, settings: Settings
) -> None:
    from app.web.settings_ui import RuntimeSettings, apply_runtime  # see _plan_settings

    sessions = list(manager) if manager is not None else []
    for key, value in plan.settings.items():
        if key == RUNTIME_KEY:
            values = RuntimeSettings.model_validate({**RuntimeSettings.from_settings(settings).model_dump(), **value})
            await repo.set_setting(key, value)
            apply_runtime(settings, values, manager)
        elif key == fir_svc.FIR_SETTING_KEY:
            await fir_svc.set_fir_capability(value, settings=settings, sessions=sessions, repo=repo)
        else:
            await repo.set_setting(key, value)
            if key.startswith(ADOPTION_PREFIX) and manager is not None and isinstance(value, dict):
                with contextlib.suppress(ValueError):
                    session = manager.get(int(key.removeprefix(ADOPTION_PREFIX)))
                    if session is not None and session.loaded and session.adoption is None:
                        session.adoption = value
        report.settings.append(key)


async def _apply_loaded(plan: ImportPlan, repo: Repo) -> bool:
    """Merge the backup's "which profile was loaded into which preset" record into this database's. Profile ids are
    the exporting database's: each is mapped by name to a profile here that holds the same document, and the serial
    is re-keyed for a re-targeted import; entries that don't resolve are dropped. Returns whether anything changed."""
    assert plan.loaded is not None
    names = {p.id: p.name.strip() for p in plan.backup.profiles if p.id is not None}
    configs = {p.name.strip(): p.config for p in plan.backup.profiles}
    remap = plan.options.target_serial is not None and len(plan.backup.devices) == 1
    source = plan.backup.devices[0].serial if remap else None
    slots = await profiles_svc.loaded_slots(repo)
    before = dict(slots)
    resolved: dict[int, int | None] = {}
    for key, entry in plan.loaded.items():
        serial, _, preset = str(key).partition(":")
        if not (serial.isdigit() and preset.isdigit() and isinstance(entry, dict)):
            continue
        old_id = entry.get("profile")
        if not isinstance(old_id, int) or old_id not in names:
            continue
        if old_id not in resolved:
            target = await repo.get_profile_by_name(names[old_id])
            same = target is not None and target.config == configs.get(names[old_id])
            resolved[old_id] = target.id if target is not None and same else None
        new_id = resolved[old_id]
        if new_id is None:
            continue
        target_serial = int(serial)
        if remap:
            if target_serial != source:
                continue  # another device's record; the backup is being re-targeted
            target_serial = plan.options.target_serial
        at = entry.get("at")
        slots[(target_serial, int(preset))] = (new_id, at if isinstance(at, str) else None)
    if slots == before:
        return False
    await profiles_svc.store_slots(repo, slots)
    return True


async def _apply_channels(
    device: DevicePlan, channels: list[ChannelEntry], repo: Repo
) -> tuple[int, list[tuple[ChannelEntry, str]]]:
    done, failed = 0, []
    for c in channels:
        try:
            if device.session is not None:
                await device.session.set_channel_meta(
                    c.kind, c.idx, name=c.name, color=c.color, max_gain_db=c.max_gain_db
                )
            else:
                await repo.set_channel_meta(
                    device.serial, c.kind, c.idx, name=c.name, color=c.color, max_gain_db=c.max_gain_db
                )
            done += 1
        except ValueError as exc:  # DesiredValidationError is a ValueError
            failed.append((c, str(exc)))
    return done, failed


async def _apply_preset(device: DevicePlan, p: PresetPlan, *, repo: Repo, timeout: float | None) -> dict[str, Any]:
    result: dict[str, Any] = {"serial": device.serial, "preset": p.preset}
    session = device.session
    trust = device.restore_shadow and not p.stored and fully_synced(p.entry, p.doc, device.profile)
    if session is None:
        keep = not trust
        revision = await repo.commit_revision(
            device.serial,
            p.preset,
            p.doc,
            summary=IMPORT_SUMMARY,
            source="import",
            applied=UNSET if keep else p.entry.applied,
            unknown_paths=UNSET if keep else p.entry.unknown_paths,
        )
        return {**result, "mode": "stored", "revision": revision}
    if trust:
        adopted = await session.adopt("trust", preset=p.preset, doc=p.doc, trust_all=True)
        return {**result, "mode": "trusted", "revision": adopted.revision, "writes": adopted.writes}
    if p.changes == 0 and p.stored and not session.writer.state(p.preset).unknown_paths(device.profile):
        return {**result, "mode": "unchanged", "revision": session.revisions.get(p.preset, 0)}
    edit = await session.replace_desired(
        p.preset,
        p.doc,
        confirm=True,
        source="import",
        summary=IMPORT_SUMMARY,
        commit=True,
        timeout=timeout,
        push_unknown=True,
    )
    return {
        **result,
        "mode": "applied" if p.active else "staged",
        "revision": edit.revision,
        "changed": len(edit.device_paths),
        "flush": flush_json(edit.flush),
    }


async def apply_import(
    plan: ImportPlan,
    *,
    repo: Repo,
    manager: SessionManager | None,
    settings: Settings,
    timeout: float | None = BULK_TIMEOUT_S,
) -> ImportReport:
    """Apply a clean plan (see the module docstring for what happens to live and offline devices).

    Order: FIR blobs (presets reference them), settings (the FIR toggle and runtime limits), channel metadata, presets,
    channel metadata that failed the first time (a lower ceiling needs the lower gains first), profiles. A failing
    item is reported and the rest continue.
    """
    if not plan.ok:
        raise BackupError("the backup can't be imported", plan.problems)
    report = ImportReport(warnings=list(plan.warnings))
    include = plan.options.include
    for blob in plan.fir_new:
        await repo.put_fir_blob(blob.decode(), name=blob.name)
        report.fir_blobs += 1
    if "settings" in include:
        await _apply_settings(plan, report, repo=repo, manager=manager, settings=settings)
    retry: list[tuple[DevicePlan, list[ChannelEntry]]] = []
    for device in plan.devices:
        if not device.known and device.session is None:
            e = device.entry
            await repo.upsert_device(
                device.serial,
                product_name=device.profile.product_name,
                hw_id=e.hw_id,
                dsp_version=e.dsp_version,
                fw=e.fw,
            )
        done, failed = await _apply_channels(device, device.channels, repo)
        report.channels += done
        if failed:
            retry.append((device, [c for c, _ in failed]))
        for p in device.presets:
            try:
                report.presets.append(await _apply_preset(device, p, repo=repo, timeout=timeout))
            except _PRESET_ERRORS as exc:
                problems = getattr(exc, "problems", None) or []
                detail = f"{exc}: {'; '.join(problems[:5])}" if problems else str(exc)
                report.errors.append(f"device {device.serial} preset {p.preset + 1}: {detail}")
    for device, channels in retry:
        done, failed = await _apply_channels(device, channels, repo)
        report.channels += done
        report.errors += [f"device {device.serial} {c.kind} {c.idx + 1}: {msg}" for c, msg in failed]
    for entry in plan.profiles_new:
        await repo.create_profile(
            entry.name, entry.config, description=entry.description, product_name=entry.product_name
        )
        report.profiles_created.append(entry.name.strip())
    for entry in plan.profiles_overwrite:
        existing = await repo.get_profile_by_name(entry.name.strip())
        if existing is not None:
            await repo.update_profile(
                existing.id, config=entry.config, description=entry.description, product_name=entry.product_name
            )
            report.profiles_updated.append(existing.name)
    report.profiles_skipped = [p.name.strip() for p in plan.profiles_skip]
    if plan.loaded is not None and await _apply_loaded(plan, repo):
        report.settings.append(profiles_svc.LOADED_KEY)
    await repo.log_event(
        "backup",
        f"imported a JSON backup: {len(report.presets)} preset(s), {len(report.profiles_created)} new profile(s)"
        + (f", {len(report.errors)} error(s)" if report.errors else ""),
        level="warning" if report.errors else "info",
        data={"errors": report.errors[:10]} if report.errors else None,
    )
    return report


# --- SQLite snapshot / nightly backups --------------------------------------------------------------------------------


def _check_file_db(db_path: str | Path) -> Path:
    text = str(db_path)
    if text == ":memory:" or text.startswith("file:"):
        raise BackupError("the database is not a plain file; it can't be backed up")
    return Path(text)


def snapshot(db_path: str | Path) -> bytes:
    """A consistent copy of the database (SQLite backup API through a second connection), as bytes. Blocking."""
    path = _check_file_db(db_path)
    if not path.is_file():
        raise BackupError(f"no database at {path}")
    src = sqlite3.connect(path, timeout=10)
    dst = sqlite3.connect(":memory:")
    try:
        src.backup(dst)
        return dst.serialize()
    finally:
        dst.close()
        src.close()


def backup_name(prefix: str, now: datetime | None = None) -> str:
    return f"{prefix}{(now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')}{BACKUP_SUFFIX}"


def default_backup_dir(db_path: str | Path) -> Path:
    return Path(db_path).parent / "backups"


@dataclass(frozen=True)
class BackupFile:
    name: str
    path: Path
    size: int
    kind: Literal["nightly", "pre-restore"]

    def as_json(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "kind": self.kind, "created": _stamp(self.name)}


def _stamp(name: str) -> str | None:
    match = re.search(r"(\d{8}T\d{6})Z", name)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=UTC).isoformat()


def list_backups(backup_dir: Path) -> list[BackupFile]:
    """Backup files in ``backup_dir``, newest first."""
    if not backup_dir.is_dir():
        return []
    files = []
    for path in backup_dir.iterdir():
        if BACKUP_NAME.match(path.name) and path.is_file():
            kind: Literal["nightly", "pre-restore"] = (
                "pre-restore" if path.name.startswith(PRE_RESTORE_PREFIX) else "nightly"
            )
            files.append(BackupFile(path.name, path, path.stat().st_size, kind))
    return sorted(files, key=lambda f: (_stamp(f.name) or "", f.name), reverse=True)


def backup_file(backup_dir: Path, name: str) -> Path | None:
    """The path of a listed backup, or None (also for anything that isn't a plain backup name)."""
    if not BACKUP_NAME.match(name):
        return None
    path = backup_dir / name
    return path if path.is_file() else None


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    with open(part, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(part, path)


def prune(backup_dir: Path, *, keep: int, kind: Literal["nightly", "pre-restore"] = "nightly") -> list[str]:
    """Delete all but the newest ``keep`` backups of one kind. Returns the deleted names."""
    old = [f for f in list_backups(backup_dir) if f.kind == kind][keep:]
    for f in old:
        with contextlib.suppress(FileNotFoundError):
            f.path.unlink()
    return [f.name for f in old]


@dataclass(frozen=True)
class BackupResult:
    path: Path | None  # None when skipped
    sha256: str
    pruned: list[str]


def run_backup(
    db_path: str | Path,
    backup_dir: Path,
    *,
    keep: int = KEEP_BACKUPS,
    last_sha256: str | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Write a backup unless the database is unchanged since the newest one. Blocking (use a thread)."""
    data = snapshot(db_path)
    sha = hashlib.sha256(data).hexdigest()
    if last_sha256 is None:
        newest = next((f for f in list_backups(backup_dir) if f.kind == "nightly"), None)
        if newest is not None:
            last_sha256 = hashlib.sha256(newest.path.read_bytes()).hexdigest()
    if sha == last_sha256:
        return BackupResult(None, sha, [])
    path = backup_dir / backup_name(BACKUP_PREFIX, now)
    _write_atomically(path, data)
    return BackupResult(path, sha, prune(backup_dir, keep=keep))


def seconds_until(at: time, now: datetime) -> float:
    """Seconds from ``now`` (naive local or aware) to the next ``at`` on the same clock."""
    target = now.replace(hour=at.hour, minute=at.minute, second=at.second, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def nightly_backup_loop(
    db_path: str | Path,
    backup_dir: Path | None = None,
    *,
    at: time = NIGHTLY_AT,
    keep: int = KEEP_BACKUPS,
    clock: Callable[[], datetime] = datetime.now,
) -> None:
    """Back up once a night at ``at`` (local time), skipping unchanged databases. Never raises (except cancel)."""
    backup_dir = backup_dir or default_backup_dir(db_path)
    last: str | None = None
    while True:
        await asyncio.sleep(seconds_until(at, clock()))
        try:
            result = await asyncio.to_thread(run_backup, db_path, backup_dir, keep=keep, last_sha256=last)
        except Exception:
            log.exception("nightly database backup failed")
            continue
        last = result.sha256
        if result.path is not None:
            log.info("database backed up to %s", result.path)
        await asyncio.sleep(1)  # the clock may still read `at` right after a fast backup


def start_nightly_backups(db_path: str | Path, backup_dir: Path | None = None) -> asyncio.Task:
    """Lifespan hook: start the nightly backup task (cancel it on shutdown)."""
    return asyncio.create_task(nightly_backup_loop(db_path, backup_dir), name="nightly-backup")


# --- restore ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DbInfo:
    schema_version: int
    size: int
    counts: dict[str, int]
    serials: list[int]
    warnings: list[str]

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "size": self.size,
            "counts": self.counts,
            "serials": self.serials,
            "warnings": self.warnings,
        }


def _open_bytes(data: bytes) -> sqlite3.Connection:
    """An in-memory connection over ``data``. WAL-mode files can't be deserialized as they are, so those are copied
    with the header saying rollback-journal (bytes 18/19)."""
    if len(data) >= 20 and data[18] == 2 and data[19] == 2:
        buf = bytearray(data)
        buf[18] = buf[19] = 1
        data = buf
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(data)
    except sqlite3.Error:
        conn.close()
        raise
    return conn


def _open_file(path: Path) -> sqlite3.Connection:
    """A read-only connection to a database file that SQLite treats as immutable: nothing is written next to it (no
    journal, WAL, shared-memory or lock files), and a WAL-mode header is fine."""
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)


def validate_db_bytes(data: bytes, *, migrations_dir: Path = MIGRATIONS_DIR) -> DbInfo:
    """Check that ``data`` is a minidsp-webui database this code can open. Raises `BackupError`.
    Holds the database in memory; large files go through `validate_db_file`."""
    if len(data) > MAX_DB_BYTES:
        raise BackupError(f"the file is larger than {MAX_DB_BYTES >> 20} MB")
    if not data.startswith(SQLITE_MAGIC):
        raise BackupError("not an SQLite database")
    try:
        conn = _open_bytes(data)
    except sqlite3.Error as exc:
        raise BackupError(f"the SQLite file can't be opened: {exc}") from None
    return _db_info(conn, len(data), migrations_dir)


def validate_db_file(path: Path, *, migrations_dir: Path = MIGRATIONS_DIR) -> DbInfo:
    """`validate_db_bytes` for a file, read through SQLite page by page (memory use doesn't grow with its size)."""
    size = path.stat().st_size
    if size > MAX_DB_BYTES:
        raise BackupError(f"the file is larger than {MAX_DB_BYTES >> 20} MB")
    with open(path, "rb") as fh:
        if fh.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
            raise BackupError("not an SQLite database")
    try:
        conn = _open_file(path)
    except sqlite3.Error as exc:
        raise BackupError(f"the SQLite file can't be opened: {exc}") from None
    return _db_info(conn, size, migrations_dir)


def _db_info(conn: sqlite3.Connection, size: int, migrations_dir: Path) -> DbInfo:
    """The checks behind `validate_db_bytes` / `validate_db_file`; closes ``conn``."""
    try:
        try:
            integrity = [row[0] for row in conn.execute("PRAGMA integrity_check(20)")]
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.Error as exc:
            raise BackupError(f"the SQLite file is unreadable: {exc}") from None
        if integrity != ["ok"]:
            raise BackupError("the database is corrupt", integrity)
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise BackupError("not a minidsp-webui database", [f"missing table {t}" for t in missing])
        known = {m.version: m.name for m in discover_migrations(migrations_dir)}
        applied = dict(conn.execute("SELECT version, name FROM schema_migrations").fetchall())
        if not applied:
            raise BackupError("the database has no migration history")
        problems = [
            f"migration {v} ({name}) is unknown to this version of the app (made by a newer version?)"
            for v, name in sorted(applied.items())
            if known.get(v) != name
        ]
        if problems:
            raise BackupError("the database doesn't match this app's schema", problems)
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("device", "preset_state", "revision", "profile", "fir_blob", "channel_meta")
        }
        serials = sorted(
            row[0] for row in conn.execute("SELECT serial FROM device UNION SELECT serial FROM preset_state")
        )
        warnings = []
        for serial, preset, desired in conn.execute("SELECT serial, preset, desired_json FROM preset_state"):
            try:
                ConfigDoc.model_validate_json(desired)
            except ValidationError:
                warnings.append(f"device {serial} preset {preset + 1}: unusable stored state (it would start empty)")
        return DbInfo(max(applied), size, counts, serials, warnings)
    finally:
        conn.close()


def restore_path(db_path: str | Path) -> Path:
    path = _check_file_db(db_path)
    return path.with_name(path.name + ".restore")


def stage_restore(db_path: str | Path, data: bytes) -> DbInfo:
    """Validate ``data`` and stage it to replace the database at the next start. Blocking."""
    info = validate_db_bytes(data)
    _write_atomically(restore_path(db_path), data)
    return info


def upload_path(db_path: str | Path) -> Path:
    """A fresh file for an uploaded database: next to the staged restore, or in the temp directory when the database
    is not a plain file (only a dry run can succeed then)."""
    token = secrets.token_hex(6)
    try:
        staged = restore_path(db_path)
    except BackupError:
        return Path(tempfile.gettempdir()) / f"minidsp-webui-restore-{token}.upload"
    staged.parent.mkdir(parents=True, exist_ok=True)
    return staged.with_name(f"{staged.name}.{token}.upload")


def stage_restore_file(db_path: str | Path, upload: Path, *, dry_run: bool = False) -> DbInfo:
    """Validate an uploaded (fsynced) database file and, unless ``dry_run``, move it into place as the staged
    restore. The upload is consumed either way. Blocking."""
    try:
        info = validate_db_file(upload)
        if not dry_run:
            os.replace(upload, restore_path(db_path))
        return info
    finally:
        with contextlib.suppress(FileNotFoundError):
            upload.unlink()


def pending_restore(db_path: str | Path) -> dict[str, Any] | None:
    try:
        path = restore_path(db_path)
    except BackupError:
        return None
    if not path.is_file():
        return None
    stat = path.stat()
    return {"size": stat.st_size, "staged_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat()}


def cancel_restore(db_path: str | Path) -> bool:
    try:
        restore_path(db_path).unlink()
    except (FileNotFoundError, BackupError):
        return False
    return True


def apply_staged_restore(db_path: str | Path, backup_dir: Path | None = None) -> Path | None:
    """Startup hook, before the database is opened: swap in a staged restore.

    The replaced database is kept as ``backups/pre-restore-*.sqlite3`` (the newest `KEEP_PRE_RESTORE`). A staged file
    that fails validation is renamed to ``*.restore.rejected`` and the database is left alone. Returns the path of
    the database that was restored, or None.
    """
    try:
        staged = restore_path(db_path)
    except BackupError:
        return None
    if not staged.is_file():
        return None
    path = Path(db_path)
    backup_dir = backup_dir or default_backup_dir(path)
    try:
        validate_db_file(staged)
    except BackupError as exc:
        log.error("staged database restore rejected: %s %s", exc, exc.problems)
        os.replace(staged, staged.with_name(staged.name + ".rejected"))
        return None
    if path.exists():
        keep_copy = backup_dir / backup_name(PRE_RESTORE_PREFIX)
        try:
            _write_atomically(keep_copy, snapshot(path))
        except (sqlite3.Error, BackupError, OSError):
            log.exception("could not snapshot the database before the restore; keeping a raw copy instead")
            _write_atomically(keep_copy, path.read_bytes())
        prune(backup_dir, keep=KEEP_PRE_RESTORE, kind="pre-restore")
    for suffix in ("-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            path.with_name(path.name + suffix).unlink()
    os.replace(staged, path)
    log.warning("restored the database from a staged backup")
    return path
