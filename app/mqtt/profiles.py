"""Saved profiles as the bridge sees them: the Profile select's options, which profile the active preset matches,
and "load profile into active preset" (docs/10 Phase 5).

Profiles are read from the repo (a read, never a write) and cached for a short while, so the periodic discovery
refresh doesn't hit the database on every state publish. `ProfileCatalog.invalidate` drops the cache right away.
"""

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.model.config_doc import ConfigDoc, validate_doc
from app.model.device_profile import DeviceProfile
from app.model.payload import device_values, same_value
from app.sync import profiles as profiles_svc
from app.sync.session import ConfirmationRequired, DeviceSession, EditResult

log = logging.getLogger(__name__)

CACHE_TTL_S = 60.0


@dataclass(frozen=True)
class ProfileEntry:
    id: int
    name: str
    doc: ConfigDoc
    values: dict[str, Any]  # device values of ``doc``, for matching against the active preset


def entry_for(profile_id: int, name: str, config: Any, device_profile: DeviceProfile) -> ProfileEntry | None:
    """A loadable entry, or None when the stored config isn't a valid document for this device model."""
    try:
        doc = ConfigDoc.model_validate(config)
    except ValidationError:
        return None
    if validate_doc(doc, device_profile):
        return None
    return ProfileEntry(profile_id, name, doc, device_values(doc, device_profile))


def matches(entry: ProfileEntry, values: Mapping[str, Any], profile: DeviceProfile) -> bool:
    """Whether a preset whose device values are ``values`` holds exactly this profile (delays as whole samples)."""
    if entry.values.keys() != values.keys():
        return False
    return all(same_value(path, value, values[path], profile) for path, value in entry.values.items())


class ProfileCatalog:
    def __init__(self, *, ttl_s: float = CACHE_TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, list[ProfileEntry]]] = {}  # product name -> (loaded at, entries)
        self._match: dict[int, tuple[ConfigDoc, int, str | None]] = {}  # serial -> (desired, generation, name)
        self.generation = 0

    def invalidate(self) -> None:
        self._cache.clear()
        self._match.clear()
        self.generation += 1

    async def entries(self, session: DeviceSession, *, fresh: bool = False) -> list[ProfileEntry]:
        """Profiles usable on this device (no product, or the device's product), sorted by name."""
        repo = session.repo
        if repo is None:
            return []
        product = session.profile.product_name
        cached = self._cache.get(product)
        if not fresh and cached is not None and self._clock() - cached[0] < self.ttl_s:
            return cached[1]
        profiles = await repo.list_profiles()
        entries = []
        for profile in profiles:
            if profile.product_name not in (None, product):
                continue
            entry = entry_for(profile.id, profile.name, profile.config, session.profile)
            if entry is None:
                log.debug("mqtt: profile %r is not a valid %s document; not offered", profile.name, product)
                continue
            entries.append(entry)
        if cached is None or [(e.id, e.name, e.values) for e in cached[1]] != [
            (e.id, e.name, e.values) for e in entries
        ]:
            self._match.clear()
            self.generation += 1
        self._cache[product] = (self._clock(), entries)
        return entries

    def matching(self, session: DeviceSession, entries: list[ProfileEntry], preset: int | None) -> str | None:
        """Name of the profile the preset's desired state equals, if exactly one does (cached per desired doc)."""
        if preset is None or not entries or not session.loaded:
            return None
        try:
            state = session.writer.state(preset)
        except Exception:
            return None
        cached = self._match.get(session.serial)
        if cached is not None and cached[0] is state.desired and cached[1] == self.generation:
            return cached[2]
        values = state.values(session.profile)
        found = [e.name for e in entries if matches(e, values, session.profile)]
        name = found[0] if len(found) == 1 else None
        self._match[session.serial] = (state.desired, self.generation, name)
        return name


def _unconfirmed_unmutes(session: DeviceSession, preset: int, doc: ConfigDoc) -> list[str]:
    """Outputs the load would leave un-muted although their gain or mute was never confirmed on the device (the same
    rule as un-muting one output from Home Assistant)."""
    status = session.sync_status(preset)
    return [
        f"output {n + 1}"
        for n, out in enumerate(doc.outputs)
        if not out.mute
        and any(status.get(f"outputs.{n}.{field}", "unknown") == "unknown" for field in ("gain_db", "mute"))
    ]


async def load_into_active(session: DeviceSession, entry: ProfileEntry) -> EditResult:
    """ "Load profile into active preset" through the profiles service, like a load from the Profiles page: pending
    edits are committed first, the load is one revision (source "profile"), it is a bulk apply (mute-wrapped per
    settings) and the profile list records which preset it was loaded into.

    Home Assistant can't confirm, so guarded loads (a gain raise above the big-step threshold, an FIR load) raise
    `ConfirmationRequired` and are rejected, and so is a load that would un-mute an output whose gain or mute was
    never confirmed on the device. Value ranges and channel ceilings apply as well."""
    await session.ensure_loaded()
    preset = await session.active_preset()
    if unsynced := _unconfirmed_unmutes(session, preset, entry.doc):
        raise ConfirmationRequired(
            f"loading {entry.name!r} would un-mute {', '.join(unsynced)}, never synced to the device; "
            "load it from the web UI"
        )
    result = await profiles_svc.load_profile(session, entry.id, preset, confirm=False)
    flush = result.edit.flush
    if flush is not None and not flush.ok:
        session.log_event(
            "warning",
            f"profile {entry.name!r} loaded into preset {result.preset + 1} from Home Assistant, "
            "but not every setting is on the device yet",
            kind="profile",
            source="mqtt",
        )
    else:
        session.log_event(
            "info",
            f"profile {entry.name!r} loaded into preset {result.preset + 1} from Home Assistant",
            kind="profile",
            source="mqtt",
        )
    return result.edit
