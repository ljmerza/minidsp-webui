"""Persistent state: SQLite (WAL) with plain-SQL migrations, a repository, and debounced persistence."""

from app.state.db import Database, MigrationError, utcnow_iso
from app.state.persist import DebouncedPersister, PresetSnapshot, preset_state_persister
from app.state.repo import (
    EVENT_RETENTION,
    REVISION_RETENTION,
    ChannelMeta,
    Device,
    Event,
    FirBlob,
    PresetState,
    Profile,
    ProfileNameTaken,
    Repo,
    Revision,
    RevisionConflict,
    RevisionInfo,
)

__all__ = [
    "EVENT_RETENTION",
    "REVISION_RETENTION",
    "ChannelMeta",
    "Database",
    "DebouncedPersister",
    "Device",
    "Event",
    "FirBlob",
    "MigrationError",
    "PresetSnapshot",
    "PresetState",
    "Profile",
    "ProfileNameTaken",
    "Repo",
    "Revision",
    "RevisionConflict",
    "RevisionInfo",
    "preset_state_persister",
    "utcnow_iso",
]
