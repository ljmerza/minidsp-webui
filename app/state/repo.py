"""CRUD over the SQLite schema (migrations/0001_initial.sql).

JSON columns go in and out as plain dicts; callers validate them (e.g. ``ConfigDoc.model_validate``). Pydantic models
are accepted wherever a JSON document is stored.

Write discipline (SD card): upserts only touch the row when something actually changed, revisions are written only
by ``commit_revision`` (a commit boundary), and the event log is trimmed by ``trim_events`` from a daily job, not per
insert.
"""

import hashlib
import json
import math
import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel

from app.state.db import Connection, Database, utcnow_iso

REVISION_RETENTION = 200
EVENT_RETENTION = 5000
N_PRESETS = 4

ChannelKind = Literal["input", "output"]
EventLevel = Literal["debug", "info", "warning", "error"]
EVENT_LEVELS: tuple[str, ...] = ("debug", "info", "warning", "error")
REVISION_SOURCES = frozenset({"ui", "api", "import", "profile", "adopt", "revert", "mqtt"})

_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
_FIR_DTYPE = np.dtype("<f8")


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Any = _Unset()


class RevisionConflict(Exception):
    """``expected_revision`` did not match the stored revision (optimistic concurrency, e.g. ``If-Match``)."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected revision {expected}, found {actual}")
        self.expected = expected
        self.actual = actual


class ProfileNameTaken(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"a profile named {name!r} already exists")
        self.name = name


# --- row types ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Device:
    serial: int
    product_name: str | None
    hw_id: int | None
    dsp_version: int | None
    fw: str | None
    daemon_url: str | None
    first_seen: str | None
    last_seen: str | None


@dataclass(frozen=True)
class PresetState:
    serial: int
    preset: int
    desired: dict[str, Any]
    applied: dict[str, Any] | None  # the shadow of what is on the device; None = nothing known
    unknown_paths: list[str]
    revision: int
    updated_at: str | None


@dataclass(frozen=True)
class RevisionInfo:
    id: int
    serial: int
    preset: int
    revision: int
    summary: str | None
    source: str | None
    created_at: str | None


@dataclass(frozen=True)
class Revision(RevisionInfo):
    desired: dict[str, Any]


@dataclass(frozen=True)
class Profile:
    id: int
    name: str
    description: str | None
    product_name: str | None
    config: dict[str, Any]
    created_at: str | None
    updated_at: str | None


@dataclass(frozen=True)
class FirBlob:
    id: str
    name: str | None
    n_taps: int
    sha256: str
    created_at: str | None
    taps: np.ndarray | None = None  # float64; None in listings


@dataclass(frozen=True)
class ChannelMeta:
    serial: int
    kind: ChannelKind
    idx: int
    name: str | None
    color: str | None
    max_gain_db: float | None


@dataclass(frozen=True)
class Event:
    id: int
    ts: str
    level: str
    kind: str
    message: str
    data: Any


# --- helpers --------------------------------------------------------------------------------------------------


def dumps(value: Any) -> str:
    """Canonical JSON (sorted keys, compact, no NaN) so equal documents serialize to equal strings."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _loads(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _check_preset(preset: int) -> None:
    if not 0 <= preset < N_PRESETS:
        raise ValueError(f"preset must be 0..{N_PRESETS - 1}, got {preset}")


def _check_kind(kind: str) -> None:
    if kind not in ("input", "output"):
        raise ValueError(f"channel kind must be 'input' or 'output', got {kind!r}")


def _unknown_json(paths: Iterable[str]) -> str:
    return json.dumps(sorted(set(paths)), separators=(",", ":"))


def _preset_state(row: sqlite3.Row) -> PresetState:
    return PresetState(
        serial=row["serial"],
        preset=row["preset"],
        desired=json.loads(row["desired_json"]),
        applied=_loads(row["applied_json"]),
        unknown_paths=json.loads(row["unknown_paths_json"]),
        revision=row["revision"],
        updated_at=row["updated_at"],
    )


def _revision_info(row: sqlite3.Row) -> RevisionInfo:
    return RevisionInfo(
        id=row["id"],
        serial=row["serial"],
        preset=row["preset"],
        revision=row["revision"],
        summary=row["summary"],
        source=row["source"],
        created_at=row["created_at"],
    )


def _profile(row: sqlite3.Row) -> Profile:
    return Profile(
        id=row["id"],
        name=row["name"],
        description=row["description"],
        product_name=row["product_name"],
        config=json.loads(row["config_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _fir_blob(row: sqlite3.Row, *, with_taps: bool) -> FirBlob:
    taps = np.frombuffer(row["taps"], dtype=_FIR_DTYPE).astype(np.float64) if with_taps else None
    return FirBlob(
        id=row["id"],
        name=row["name"],
        n_taps=row["n_taps"],
        sha256=row["sha256"],
        created_at=row["created_at"],
        taps=taps,
    )


def _event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        ts=row["ts"],
        level=row["level"],
        kind=row["kind"],
        message=row["message"],
        data=_loads(row["data_json"]),
    )


async def _write_committed_state(
    tx: Connection,
    serial: int,
    preset: int,
    desired_json: str,
    revision: int,
    now: str,
    applied: Any,
    unknown_paths: Any,
) -> None:
    sets = ["desired_json = excluded.desired_json", "revision = excluded.revision", "updated_at = excluded.updated_at"]
    applied_json = None
    unknown_json = "[]"
    if applied is not UNSET:
        applied_json = None if applied is None else dumps(applied)
        sets.append("applied_json = excluded.applied_json")
    if unknown_paths is not UNSET:
        unknown_json = _unknown_json(unknown_paths)
        sets.append("unknown_paths_json = excluded.unknown_paths_json")
    await tx.execute(
        "INSERT INTO preset_state"
        " (serial, preset, desired_json, applied_json, unknown_paths_json, revision, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        f" ON CONFLICT (serial, preset) DO UPDATE SET {', '.join(sets)}",
        (serial, preset, desired_json, applied_json, unknown_json, revision, now),
    )


_DEVICE_FIELDS = ("product_name", "hw_id", "dsp_version", "fw", "daemon_url")


class Repo:
    def __init__(self, db: Database, *, clock: Callable[[], str] = utcnow_iso) -> None:
        self.db = db
        self._now = clock

    # --- device -----------------------------------------------------------------------------------------------

    async def upsert_device(
        self,
        serial: int,
        *,
        product_name: str | None = None,
        hw_id: int | None = None,
        dsp_version: int | None = None,
        fw: str | None = None,
        daemon_url: str | None = None,
        touch: bool = False,
    ) -> bool:
        """Insert or update a device. Writes only when the device is new or its identity changed, or when
        ``touch`` asks for ``last_seen`` to be bumped. Returns whether a write happened."""
        values = {
            "product_name": product_name,
            "hw_id": hw_id,
            "dsp_version": dsp_version,
            "fw": fw,
            "daemon_url": daemon_url,
        }
        async with self.db.transaction() as tx:
            row = await tx.fetchone("SELECT * FROM device WHERE serial = ?", (serial,))
            now = self._now()
            if row is None:
                await tx.execute(
                    "INSERT INTO device"
                    " (serial, product_name, hw_id, dsp_version, fw, daemon_url, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (serial, *values.values(), now, now),
                )
                return True
            if not touch and all(row[f] == values[f] for f in _DEVICE_FIELDS):
                return False
            await tx.execute(
                "UPDATE device SET product_name = ?, hw_id = ?, dsp_version = ?, fw = ?, daemon_url = ?, last_seen = ?"
                " WHERE serial = ?",
                (*values.values(), now, serial),
            )
            return True

    async def get_device(self, serial: int) -> Device | None:
        row = await self.db.fetchone("SELECT * FROM device WHERE serial = ?", (serial,))
        return None if row is None else Device(**dict(row))

    async def list_devices(self) -> list[Device]:
        rows = await self.db.fetchall("SELECT * FROM device ORDER BY serial")
        return [Device(**dict(row)) for row in rows]

    async def delete_device(self, serial: int) -> bool:
        res = await self.db.execute("DELETE FROM device WHERE serial = ?", (serial,))
        return res.rowcount > 0

    # --- preset state -----------------------------------------------------------------------------------------

    async def get_preset_state(self, serial: int, preset: int) -> PresetState | None:
        _check_preset(preset)
        row = await self.db.fetchone("SELECT * FROM preset_state WHERE serial = ? AND preset = ?", (serial, preset))
        return None if row is None else _preset_state(row)

    async def list_preset_states(self, serial: int) -> list[PresetState]:
        rows = await self.db.fetchall("SELECT * FROM preset_state WHERE serial = ? ORDER BY preset", (serial,))
        return [_preset_state(row) for row in rows]

    async def save_preset_state(
        self,
        serial: int,
        preset: int,
        *,
        desired: Any,
        applied: Any = None,
        unknown_paths: Iterable[str] = (),
    ) -> bool:
        """Persist desired + shadow without touching the revision counter (that's ``commit_revision``).

        A no-op when the stored row is identical. Returns whether the row changed.
        """
        _check_preset(preset)
        params = (
            serial,
            preset,
            dumps(desired),
            None if applied is None else dumps(applied),
            _unknown_json(unknown_paths),
            self._now(),
        )
        res = await self.db.execute(
            "INSERT INTO preset_state (serial, preset, desired_json, applied_json, unknown_paths_json, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (serial, preset) DO UPDATE SET"
            "  desired_json = excluded.desired_json, applied_json = excluded.applied_json,"
            "  unknown_paths_json = excluded.unknown_paths_json, updated_at = excluded.updated_at"
            " WHERE desired_json IS NOT excluded.desired_json OR applied_json IS NOT excluded.applied_json"
            "  OR unknown_paths_json IS NOT excluded.unknown_paths_json",
            params,
        )
        return res.rowcount > 0

    async def delete_preset_state(self, serial: int, preset: int, *, with_history: bool = False) -> bool:
        _check_preset(preset)
        async with self.db.transaction() as tx:
            res = await tx.execute("DELETE FROM preset_state WHERE serial = ? AND preset = ?", (serial, preset))
            if with_history:
                await tx.execute("DELETE FROM revision WHERE serial = ? AND preset = ?", (serial, preset))
            return res.rowcount > 0

    # --- revisions --------------------------------------------------------------------------------------------

    async def commit_revision(
        self,
        serial: int,
        preset: int,
        desired: Any,
        *,
        summary: str = "",
        source: str = "ui",
        expected_revision: int | None = None,
        applied: Any = UNSET,
        unknown_paths: Iterable[str] | Any = UNSET,
        skip_if_unchanged: bool = True,
    ) -> int:
        """Record a commit boundary: bump ``preset_state.revision``, store ``desired`` there and as a history row,
        and trim history to the newest ``REVISION_RETENTION`` rows for this preset. One transaction.

        ``applied``/``unknown_paths`` default to keeping what is stored. With ``skip_if_unchanged`` a desired doc equal
        to the latest history row writes nothing and returns the current revision. Returns the (new) revision.
        """
        _check_preset(preset)
        if source not in REVISION_SOURCES:
            raise ValueError(f"unknown revision source {source!r}")
        desired_json = dumps(desired)
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT revision FROM preset_state WHERE serial = ? AND preset = ?",
                (serial, preset),
            )
            if row is not None:
                current = row["revision"]
            else:  # state row deleted but history kept: continue the numbering
                current = await tx.fetchval(
                    "SELECT COALESCE(MAX(revision), 0) FROM revision WHERE serial = ? AND preset = ?",
                    (serial, preset),
                )
            if expected_revision is not None and expected_revision != current:
                raise RevisionConflict(expected_revision, current)
            if skip_if_unchanged and row is not None:
                latest = await tx.fetchval(
                    "SELECT desired_json FROM revision WHERE serial = ? AND preset = ? AND revision = ?",
                    (serial, preset, current),
                )
                if latest == desired_json:
                    return current
            new = current + 1
            now = self._now()
            await _write_committed_state(tx, serial, preset, desired_json, new, now, applied, unknown_paths)
            await tx.execute(
                "INSERT INTO revision (serial, preset, revision, desired_json, summary, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (serial, preset, new, desired_json, summary, source, now),
            )
            await tx.execute(
                "DELETE FROM revision WHERE serial = ? AND preset = ? AND revision <= ?",
                (serial, preset, new - REVISION_RETENTION),
            )
            return new

    async def list_revisions(
        self, serial: int, preset: int, *, limit: int = 50, before: int | None = None
    ) -> list[RevisionInfo]:
        """Newest first. ``before`` pages back from a revision number (exclusive)."""
        _check_preset(preset)
        rows = await self.db.fetchall(
            "SELECT id, serial, preset, revision, summary, source, created_at FROM revision"
            " WHERE serial = ? AND preset = ? AND revision < ? ORDER BY revision DESC LIMIT ?",
            (serial, preset, before if before is not None else 2**62, limit),
        )
        return [_revision_info(row) for row in rows]

    async def get_revision(self, serial: int, preset: int, revision: int) -> Revision | None:
        _check_preset(preset)
        row = await self.db.fetchone(
            "SELECT * FROM revision WHERE serial = ? AND preset = ? AND revision = ?",
            (serial, preset, revision),
        )
        if row is None:
            return None
        info = _revision_info(row)
        return Revision(**info.__dict__, desired=json.loads(row["desired_json"]))

    async def count_revisions(self, serial: int, preset: int) -> int:
        return await self.db.fetchval(
            "SELECT COUNT(*) FROM revision WHERE serial = ? AND preset = ?",
            (serial, preset),
        )

    # --- profiles ---------------------------------------------------------------------------------------------

    async def create_profile(
        self,
        name: str,
        config: Any,
        *,
        description: str | None = None,
        product_name: str | None = None,
    ) -> Profile:
        name = name.strip()
        if not name:
            raise ValueError("profile name must not be empty")
        now = self._now()
        try:
            res = await self.db.execute(
                "INSERT INTO profile (name, description, product_name, config_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, description, product_name, dumps(config), now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ProfileNameTaken(name) from exc
        profile = await self.get_profile(res.lastrowid)
        assert profile is not None
        return profile

    async def get_profile(self, profile_id: int) -> Profile | None:
        row = await self.db.fetchone("SELECT * FROM profile WHERE id = ?", (profile_id,))
        return None if row is None else _profile(row)

    async def get_profile_by_name(self, name: str) -> Profile | None:
        row = await self.db.fetchone("SELECT * FROM profile WHERE name = ?", (name,))
        return None if row is None else _profile(row)

    async def list_profiles(self, *, product_name: str | None = None) -> list[Profile]:
        if product_name is None:
            rows = await self.db.fetchall("SELECT * FROM profile ORDER BY name COLLATE NOCASE")
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM profile WHERE product_name = ? ORDER BY name COLLATE NOCASE",
                (product_name,),
            )
        return [_profile(row) for row in rows]

    async def update_profile(
        self,
        profile_id: int,
        *,
        name: str | Any = UNSET,
        description: str | None | Any = UNSET,
        product_name: str | None | Any = UNSET,
        config: Any = UNSET,
    ) -> Profile | None:
        """Update the given fields; returns the updated profile, or None if it doesn't exist."""
        sets: list[str] = []
        params: list[Any] = []
        if name is not UNSET:
            name = name.strip()
            if not name:
                raise ValueError("profile name must not be empty")
            sets.append("name = ?")
            params.append(name)
        if description is not UNSET:
            sets.append("description = ?")
            params.append(description)
        if product_name is not UNSET:
            sets.append("product_name = ?")
            params.append(product_name)
        if config is not UNSET:
            sets.append("config_json = ?")
            params.append(dumps(config))
        if sets:
            sets.append("updated_at = ?")
            params.append(self._now())
            try:
                await self.db.execute(f"UPDATE profile SET {', '.join(sets)} WHERE id = ?", (*params, profile_id))
            except sqlite3.IntegrityError as exc:
                raise ProfileNameTaken(name) from exc
        return await self.get_profile(profile_id)

    async def delete_profile(self, profile_id: int) -> bool:
        res = await self.db.execute("DELETE FROM profile WHERE id = ?", (profile_id,))
        return res.rowcount > 0

    # --- FIR blobs --------------------------------------------------------------------------------------------

    async def put_fir_blob(self, taps: Sequence[float] | np.ndarray, *, name: str | None = None) -> FirBlob:
        """Store FIR taps (as float64). Content-addressed: identical taps return the existing blob unchanged."""
        arr = np.asarray(taps, dtype=np.float64)
        if arr.ndim != 1 or arr.size == 0:
            raise ValueError("FIR taps must be a non-empty 1-D sequence")
        if not np.all(np.isfinite(arr)):
            raise ValueError("FIR taps must be finite")
        data = arr.astype(_FIR_DTYPE).tobytes()
        sha = hashlib.sha256(data).hexdigest()
        blob_id = sha[:16]
        async with self.db.transaction() as tx:
            row = await tx.fetchone(
                "SELECT id, name, n_taps, sha256, created_at FROM fir_blob WHERE sha256 = ?",
                (sha,),
            )
            if row is not None:
                return _fir_blob(row, with_taps=False)
            now = self._now()
            await tx.execute(
                "INSERT INTO fir_blob (id, name, taps, n_taps, sha256, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (blob_id, name, data, int(arr.size), sha, now),
            )
        return FirBlob(id=blob_id, name=name, n_taps=int(arr.size), sha256=sha, created_at=now)

    async def get_fir_blob(self, blob_id: str) -> FirBlob | None:
        row = await self.db.fetchone("SELECT * FROM fir_blob WHERE id = ?", (blob_id,))
        return None if row is None else _fir_blob(row, with_taps=True)

    async def list_fir_blobs(self) -> list[FirBlob]:
        rows = await self.db.fetchall(
            "SELECT id, name, n_taps, sha256, created_at FROM fir_blob ORDER BY created_at, id"
        )
        return [_fir_blob(row, with_taps=False) for row in rows]

    async def delete_fir_blob(self, blob_id: str) -> bool:
        res = await self.db.execute("DELETE FROM fir_blob WHERE id = ?", (blob_id,))
        return res.rowcount > 0

    # --- channel meta -----------------------------------------------------------------------------------------

    async def set_channel_meta(
        self,
        serial: int,
        kind: ChannelKind,
        idx: int,
        *,
        name: str | None = None,
        color: str | None = None,
        max_gain_db: float | None = None,
    ) -> bool:
        """Replace one channel's metadata. Returns whether the row changed."""
        _check_kind(kind)
        if idx < 0:
            raise ValueError("channel index must be >= 0")
        if name is not None:
            name = name.strip() or None
        if color is not None and not _COLOR.match(color):
            raise ValueError(f"color must look like #rrggbb, got {color!r}")
        if max_gain_db is not None and not math.isfinite(max_gain_db):
            raise ValueError("max_gain_db must be finite")
        res = await self.db.execute(
            "INSERT INTO channel_meta (serial, kind, idx, name, color, max_gain_db) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (serial, kind, idx) DO UPDATE SET"
            "  name = excluded.name, color = excluded.color, max_gain_db = excluded.max_gain_db"
            " WHERE name IS NOT excluded.name OR color IS NOT excluded.color"
            "  OR max_gain_db IS NOT excluded.max_gain_db",
            (serial, kind, idx, name, color, max_gain_db),
        )
        return res.rowcount > 0

    async def get_channel_meta(self, serial: int, kind: ChannelKind, idx: int) -> ChannelMeta | None:
        _check_kind(kind)
        row = await self.db.fetchone(
            "SELECT * FROM channel_meta WHERE serial = ? AND kind = ? AND idx = ?",
            (serial, kind, idx),
        )
        return None if row is None else ChannelMeta(**dict(row))

    async def list_channel_meta(self, serial: int, kind: ChannelKind | None = None) -> list[ChannelMeta]:
        if kind is None:
            rows = await self.db.fetchall(
                "SELECT * FROM channel_meta WHERE serial = ? ORDER BY kind, idx",
                (serial,),
            )
        else:
            _check_kind(kind)
            rows = await self.db.fetchall(
                "SELECT * FROM channel_meta WHERE serial = ? AND kind = ? ORDER BY idx",
                (serial, kind),
            )
        return [ChannelMeta(**dict(row)) for row in rows]

    async def delete_channel_meta(self, serial: int, kind: ChannelKind, idx: int) -> bool:
        _check_kind(kind)
        res = await self.db.execute(
            "DELETE FROM channel_meta WHERE serial = ? AND kind = ? AND idx = ?",
            (serial, kind, idx),
        )
        return res.rowcount > 0

    # --- settings ---------------------------------------------------------------------------------------------

    async def get_setting(self, key: str, default: Any = None) -> Any:
        text = await self.db.fetchval("SELECT value_json FROM setting WHERE key = ?", (key,))
        return default if text is None else json.loads(text)

    async def set_setting(self, key: str, value: Any) -> bool:
        """Store a JSON value. Returns whether the stored value changed."""
        res = await self.db.execute(
            "INSERT INTO setting (key, value_json) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value_json = excluded.value_json"
            " WHERE value_json IS NOT excluded.value_json",
            (key, dumps(value)),
        )
        return res.rowcount > 0

    async def delete_setting(self, key: str) -> bool:
        res = await self.db.execute("DELETE FROM setting WHERE key = ?", (key,))
        return res.rowcount > 0

    async def all_settings(self) -> dict[str, Any]:
        rows = await self.db.fetchall("SELECT key, value_json FROM setting ORDER BY key")
        return {row["key"]: json.loads(row["value_json"]) for row in rows}

    # --- event log --------------------------------------------------------------------------------------------

    async def log_event(
        self,
        kind: str,
        message: str,
        *,
        level: EventLevel = "info",
        data: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> int:
        """Append a meaningful event (user action, apply result, error, reconnect). Never per tick or per call."""
        if level not in EVENT_LEVELS:
            raise ValueError(f"unknown event level {level!r}")
        data_json = None if data is None else json.dumps(data, separators=(",", ":"), default=str)
        res = await self.db.execute(
            "INSERT INTO event_log (ts, level, kind, message, data_json) VALUES (?, ?, ?, ?, ?)",
            (self._now(), level, kind, message, data_json),
        )
        assert res.lastrowid is not None
        return res.lastrowid

    async def list_events(
        self,
        *,
        limit: int = 100,
        before_id: int | None = None,
        kind: str | None = None,
        min_level: EventLevel | None = None,
    ) -> list[Event]:
        """Newest first. ``before_id`` pages back (exclusive)."""
        where = ["1 = 1"]
        params: list[Any] = []
        if before_id is not None:
            where.append("id < ?")
            params.append(before_id)
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if min_level is not None:
            if min_level not in EVENT_LEVELS:
                raise ValueError(f"unknown event level {min_level!r}")
            levels = EVENT_LEVELS[EVENT_LEVELS.index(min_level) :]
            where.append(f"level IN ({', '.join('?' * len(levels))})")
            params.extend(levels)
        rows = await self.db.fetchall(
            f"SELECT * FROM event_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        )
        return [_event(row) for row in rows]

    async def count_events(self) -> int:
        return await self.db.fetchval("SELECT COUNT(*) FROM event_log")

    async def trim_events(self, keep: int = EVENT_RETENTION) -> int:
        """Delete all but the newest ``keep`` events (the daily job). Returns the number deleted."""
        if keep < 0:
            raise ValueError("keep must be >= 0")
        res = await self.db.execute(
            "DELETE FROM event_log WHERE id <= (SELECT id FROM event_log ORDER BY id DESC LIMIT 1 OFFSET ?)",
            (keep,),
        )
        return res.rowcount
