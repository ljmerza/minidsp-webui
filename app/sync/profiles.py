"""Profiles and preset history (docs 05 §7, docs 06): the service layer behind the profiles API, page and MQTT.

A profile is a named, full ConfigDoc ("Movies", "Games") stored in the ``profile`` table. It can be saved from a
preset's desired state, loaded into any preset slot (staged when the slot isn't active, or switched to and applied),
duplicated, renamed, deleted, exported and imported as JSON, and diffed against another profile, a preset's desired
state or one of a preset's history revisions. Nothing is seeded automatically.

Loading a profile and reverting to a revision both go through `DeviceSession.replace_desired`: the whole document is
validated (ranges, channel ceilings, filter boosts, the big-step guard), written as a mute-wrapped bulk apply and
committed as one revision, so the session's write-ordering and un-bypass rules apply unchanged.

Which profile was last loaded into each preset slot is kept in one ``setting`` row (written only by a load), so the
profiles list can show "Preset 2: loaded 20:01, edited since".
"""

import asyncio
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.dsp.recognize import annotate_designs
from app.formats.cli_script import export_script, parse_script
from app.model.config_doc import (
    ConfigDoc,
    PathError,
    default_config_doc,
    diff_paths,
    get_path,
    is_device_path,
    set_path,
    validate_doc,
)
from app.model.device_profile import M2X4HD, DeviceProfile, profile_for
from app.model.master import MasterStatus
from app.state import Profile, ProfileNameTaken, Repo, Revision
from app.state.db import utcnow_iso
from app.state.repo import UNSET
from app.sync.session import (
    BULK_TIMEOUT_S,
    DesiredValidationError,
    DeviceSession,
    EditResult,
    describe_apply_error,
    parse_doc,
    script_error_problems,
)

__all__ = [
    "EXPORT_FORMAT",
    "LOADED_KEY",
    "DiffRow",
    "DiffSide",
    "LoadResult",
    "LoadedSlot",
    "ProfileError",
    "ProfileNameTaken",
    "ProfileNotFound",
    "ProfileView",
    "ProfilesUnavailable",
    "RevisionNotFound",
    "SourceError",
    "create_profile",
    "delete_profile",
    "diff_docs",
    "duplicate_profile",
    "export_profile",
    "export_profile_script",
    "get_profile",
    "import_profile",
    "import_profile_script",
    "list_profiles",
    "load_preview",
    "load_profile",
    "loaded_slots",
    "matching_presets",
    "normalize_delays",
    "parse_source",
    "profile_doc",
    "resolve_source",
    "revert_preview",
    "revert_to",
    "revision",
    "save_preset",
    "store_slots",
    "unique_name",
    "update_profile",
]

LOADED_KEY = "profiles.loaded"  # setting row: {"<serial>:<preset>": {"profile": id, "at": iso}}
EXPORT_FORMAT = "minidsp-webui/profile"
EXPORT_VERSION = 1
MAX_NAME = 80
MAX_DESCRIPTION = 500

_UNIT = re.compile(r"^((?:inputs|outputs)\.\d+\.(?:peq|crossover)\.\d+)\.")
_COPY_SUFFIX = re.compile(r"^(.*?) \(copy(?: (\d+))?\)$")


class ProfileError(ValueError):
    """A profile request that can't be done (wrong device model, bad import file, bad name)."""


class ProfileNotFound(ProfileError):
    def __init__(self, profile_id: int) -> None:
        super().__init__(f"there is no profile {profile_id}")
        self.profile_id = profile_id


class RevisionNotFound(ProfileError):
    def __init__(self, preset: int, revision: int) -> None:
        super().__init__(f"preset {preset + 1} has no revision {revision} (history keeps the newest 200)")
        self.preset = preset
        self.revision = revision


class SourceError(ProfileError):
    """A diff source that can't be parsed or resolved."""


class ProfilesUnavailable(RuntimeError):
    """The state database isn't open, so there are no profiles."""


# --- profiles -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadedSlot:
    """A preset slot whose last profile load was this profile."""

    serial: int
    preset: int
    at: str | None


@dataclass(frozen=True)
class ProfileView:
    profile: Profile
    loaded: list[LoadedSlot] = field(default_factory=list)

    def to_json(self, *, include_config: bool = False) -> dict[str, Any]:
        p = self.profile
        data: dict[str, Any] = {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "product_name": p.product_name,
            "created_at": p.created_at,
            "updated_at": p.updated_at,
            "loaded_into": [{"serial": s.serial, "preset": s.preset, "at": s.at} for s in self.loaded],
        }
        if include_config:
            data["config"] = p.config
        return data


def require_repo(repo: Repo | None) -> Repo:
    if repo is None:
        raise ProfilesUnavailable("the state database is not open, so profiles are unavailable")
    return repo


def _clean_name(name: Any) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ProfileError("a profile needs a name")
    name = " ".join(name.split())
    if len(name) > MAX_NAME:
        raise ProfileError(f"profile names are at most {MAX_NAME} characters")
    return name


def _clean_description(description: Any) -> str | None:
    if description is None:
        return None
    if not isinstance(description, str):
        raise ProfileError("the description must be text")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION:
        raise ProfileError(f"descriptions are at most {MAX_DESCRIPTION} characters")
    return description or None


def unique_name(base: str, taken: Iterable[str]) -> str:
    """``base``, or ``base (copy)``, ``base (copy 2)``, … : the first one not in ``taken`` (case-insensitive)."""
    used = {t.casefold() for t in taken}
    if base.casefold() not in used:
        return base
    match = _COPY_SUFFIX.match(base)
    stem = match.group(1) if match else base
    n = 1
    while True:
        suffix = " (copy)" if n == 1 else f" (copy {n})"
        candidate = stem[: MAX_NAME - len(suffix)].rstrip() + suffix
        if candidate.casefold() not in used:
            return candidate
        n += 1


def check_doc(doc: ConfigDoc, device: DeviceProfile) -> ConfigDoc:
    """Shape and range check of a document to be stored as a profile (`validate_doc`)."""
    if problems := validate_doc(doc, device):
        raise DesiredValidationError(f"the document doesn't fit a {device.product_name}", problems)
    return doc


def normalize_delays(doc: ConfigDoc, device: DeviceProfile) -> ConfigDoc:
    """``doc`` with output delays rounded to whole samples, as the device (and a loaded preset) holds them."""
    for n, out in enumerate(doc.outputs):
        if math.isfinite(out.delay_ms) and out.delay_ms >= 0:
            rounded = device.delay_ms_from_samples(device.delay_samples(out.delay_ms))
            if rounded != out.delay_ms:
                doc = set_path(doc, f"outputs.{n}.delay_ms", rounded)
    return doc


def _device_profile(product_name: str | None) -> DeviceProfile | None:
    return profile_for(product_name) if product_name else None


def profile_doc(profile: Profile) -> ConfigDoc:
    """The profile's document, strictly validated (a stored profile is never trusted blindly)."""
    return parse_doc(profile.config)


async def loaded_slots(repo: Repo) -> dict[tuple[int, int], tuple[int, str | None]]:
    """``(serial, preset) -> (profile id, loaded at)`` for every slot a profile was loaded into."""
    stored = await repo.get_setting(LOADED_KEY, {})
    slots: dict[tuple[int, int], tuple[int, str | None]] = {}
    if not isinstance(stored, Mapping):
        return slots
    for key, entry in stored.items():
        serial, _, preset = str(key).partition(":")
        if not (serial.isdigit() and preset.isdigit() and isinstance(entry, Mapping)):
            continue
        profile_id = entry.get("profile")
        if isinstance(profile_id, int):
            slots[(int(serial), int(preset))] = (profile_id, entry.get("at"))
    return slots


async def store_slots(repo: Repo, slots: Mapping[tuple[int, int], tuple[int, str | None]]) -> None:
    await repo.set_setting(
        LOADED_KEY,
        {f"{serial}:{preset}": {"profile": pid, "at": at} for (serial, preset), (pid, at) in sorted(slots.items())},
    )


def _views(profiles: list[Profile], slots: Mapping[tuple[int, int], tuple[int, str | None]]) -> list[ProfileView]:
    by_profile: dict[int, list[LoadedSlot]] = {}
    for (serial, preset), (pid, at) in sorted(slots.items()):
        by_profile.setdefault(pid, []).append(LoadedSlot(serial, preset, at))
    return [ProfileView(p, by_profile.get(p.id, [])) for p in profiles]


async def list_profiles(repo: Repo | None, *, product_name: str | None = None) -> list[ProfileView]:
    repo = require_repo(repo)
    return _views(await repo.list_profiles(product_name=product_name), await loaded_slots(repo))


async def get_profile(repo: Repo | None, profile_id: int) -> ProfileView:
    repo = require_repo(repo)
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise ProfileNotFound(profile_id)
    return _views([profile], await loaded_slots(repo))[0]


async def create_profile(
    repo: Repo | None,
    name: str,
    config: ConfigDoc | Mapping[str, Any],
    *,
    description: str | None = None,
    product_name: str | None = None,
) -> Profile:
    """Store a document as a new profile. With a ``product_name`` the document must fit that device model."""
    repo = require_repo(repo)
    name = _clean_name(name)
    doc = config if isinstance(config, ConfigDoc) else parse_doc(dict(config))
    if (device := _device_profile(product_name)) is not None:
        doc = normalize_delays(check_doc(doc, device), device)
    elif product_name:
        raise ProfileError("unsupported device model")
    return await repo.create_profile(name, doc, description=_clean_description(description), product_name=product_name)


async def save_preset(session: DeviceSession, preset: int, name: str, *, description: str | None = None) -> Profile:
    """ "Save current preset as profile": the preset's desired state (including staged, unsynced edits)."""
    repo = require_repo(session.repo)
    doc = await session.get_desired(preset)
    return await create_profile(repo, name, doc, description=description, product_name=session.profile.product_name)


async def update_profile(
    repo: Repo | None,
    profile_id: int,
    *,
    name: Any = UNSET,
    description: Any = UNSET,
    config: Any = UNSET,
) -> Profile:
    """Rename, re-describe or replace the document of a profile (only the given fields change)."""
    repo = require_repo(repo)
    current = await repo.get_profile(profile_id)
    if current is None:
        raise ProfileNotFound(profile_id)
    fields: dict[str, Any] = {}
    if name is not UNSET:
        fields["name"] = _clean_name(name)
    if description is not UNSET:
        fields["description"] = _clean_description(description)
    if config is not UNSET:
        doc = config if isinstance(config, ConfigDoc) else parse_doc(config)
        if (device := _device_profile(current.product_name)) is not None:
            doc = normalize_delays(check_doc(doc, device), device)
        fields["config"] = doc
    updated = await repo.update_profile(profile_id, **fields)
    if updated is None:
        raise ProfileNotFound(profile_id)
    return updated


async def duplicate_profile(repo: Repo | None, profile_id: int, *, name: str | None = None) -> Profile:
    """Copy a profile; the copy is named ``name`` or "<name> (copy)" (numbered when taken)."""
    repo = require_repo(repo)
    source = await repo.get_profile(profile_id)
    if source is None:
        raise ProfileNotFound(profile_id)
    if name is None:
        name = unique_name(source.name, (p.name for p in await repo.list_profiles()))
    return await repo.create_profile(
        _clean_name(name), source.config, description=source.description, product_name=source.product_name
    )


async def delete_profile(repo: Repo | None, profile_id: int) -> None:
    repo = require_repo(repo)
    if not await repo.delete_profile(profile_id):
        raise ProfileNotFound(profile_id)
    slots = await loaded_slots(repo)
    remaining = {slot: entry for slot, entry in slots.items() if entry[0] != profile_id}
    if remaining != slots:
        await store_slots(repo, remaining)


def export_profile(profile: Profile) -> dict[str, Any]:
    """Full-fidelity JSON of one profile. FIR filters are referenced by blob id only (the taps are not included)."""
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "name": profile.name,
        "description": profile.description,
        "product_name": profile.product_name,
        "exported_at": utcnow_iso(),
        "config": profile.config,
    }


async def import_profile(repo: Repo | None, data: Any, *, name: str | None = None) -> tuple[Profile, list[str]]:
    """Create a profile from `export_profile` JSON. A taken name gets a "(copy)" suffix. Returns the profile and
    warnings (e.g. FIR filters whose taps aren't in this database)."""
    repo = require_repo(repo)
    if not isinstance(data, Mapping) or data.get("format") != EXPORT_FORMAT:
        raise ProfileError(f'not a profile export (expected "format": "{EXPORT_FORMAT}")')
    version = data.get("version")
    if not isinstance(version, int) or not 1 <= version <= EXPORT_VERSION:
        raise ProfileError(f"unsupported profile export version (expected 1..{EXPORT_VERSION})")
    config = data.get("config")
    if not isinstance(config, Mapping):
        raise ProfileError("the export has no config document")
    product_name = data.get("product_name")
    if product_name is not None and not isinstance(product_name, str):
        raise ProfileError("product_name must be text")
    base = _clean_name(name if name is not None else data.get("name"))
    profile = await create_profile(
        repo,
        unique_name(base, (p.name for p in await repo.list_profiles())),
        config,
        description=data.get("description"),
        product_name=product_name,
    )
    doc = profile_doc(profile)
    warnings = []
    for n, out in enumerate(doc.outputs):
        if out.fir.blob_id is not None and await repo.get_fir_blob(out.fir.blob_id) is None:
            warnings.append(
                f"output {n + 1}: its FIR filter's taps are not in this database; upload them before loading"
            )
    return profile, warnings


def _script_device(product_name: str | None) -> DeviceProfile:
    if not product_name:
        return M2X4HD
    if (device := _device_profile(product_name)) is None:
        raise ProfileError("unsupported device model")
    return device


def export_profile_script(profile: Profile, *, mute_wrap: bool = True) -> str:
    """The profile as a ``minidsp -f`` script (full state; it writes to the device's active preset)."""
    device = _script_device(profile.product_name)
    header = (
        f'minidsp-webui profile "{profile.name}".\n'
        "Run it on the DSP host with: minidsp -f <file>\n"
        "The commands write to the device's active preset. Master volume, source and preset aren't included."
    )
    return export_script(profile_doc(profile), device, mute_wrap=mute_wrap, header=header)


async def import_profile_script(
    repo: Repo | None,
    text: str,
    name: str,
    *,
    description: str | None = None,
    product_name: str | None = None,
) -> tuple[Profile, list[str]]:
    """Create a profile from a ``minidsp -f`` script applied to the defaults. A taken name gets a "(copy)" suffix.
    Scripts with errors are refused; returns the profile and a warning per ignored line."""
    repo = require_repo(repo)
    device = _script_device(product_name)
    base = _clean_name(name)
    parsed = parse_script(text, device)
    if parsed.errors:
        raise DesiredValidationError("the script has errors", script_error_problems(parsed.errors))
    if not parsed.ops:
        raise ProfileError("the script contains no preset settings")
    try:
        doc = parsed.apply(default_config_doc(device))
    except (PathError, ValidationError) as exc:
        raise ProfileError(f"the script can't be applied: {describe_apply_error(exc)}") from None
    doc = await asyncio.to_thread(annotate_designs, doc)
    profile = await create_profile(
        repo,
        unique_name(base, (p.name for p in await repo.list_profiles())),
        doc,
        description=description,
        product_name=device.product_name,
    )
    return profile, [f"line {w.line}: {w.message}" for w in parsed.warnings]


# --- loading --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadResult:
    profile: Profile
    preset: int
    edit: EditResult
    activated: bool = False
    master: MasterStatus | None = None


def _check_model(session: DeviceSession, profile: Profile) -> None:
    if profile.product_name and profile.product_name != session.profile.product_name:
        raise ProfileError(
            f"profile {profile.name!r} is for a {profile.product_name}; this device is a {session.profile.product_name}"
        )


async def _target(session: DeviceSession, preset: int | None) -> int:
    if preset is None:
        preset = await session.active_preset()
    await session.get_desired(preset)  # UnknownPreset / StateUnavailable
    return preset


async def _profile_for_load(session: DeviceSession, profile_id: int) -> tuple[Profile, ConfigDoc]:
    repo = require_repo(session.repo)
    profile = await repo.get_profile(profile_id)
    if profile is None:
        raise ProfileNotFound(profile_id)
    _check_model(session, profile)
    return profile, profile_doc(profile)


def change_counts(old: ConfigDoc, new: ConfigDoc) -> tuple[int, int]:
    """(changed settings, changed device writes) going from ``old`` to ``new``; filter bands count once."""
    paths = diff_paths(old, new)
    return len(diff_docs(old, new, paths=paths)), sum(1 for p in paths if is_device_path(p))


async def load_preview(session: DeviceSession, profile_id: int, preset: int | None = None) -> dict[str, Any]:
    """What loading the profile into ``preset`` (default: the active one) would change, without changing it."""
    profile, doc = await _profile_for_load(session, profile_id)
    preset = await _target(session, preset)
    current = await session.get_desired(preset)
    changes, writes = change_counts(current, normalize_delays(doc, session.profile))
    master = session.master
    return {
        "profile": profile.id,
        "name": profile.name,
        "preset": preset,
        "active": master is not None and master.preset == preset,
        "changes": changes,
        "device_changes": writes,
        # Settings with no confirmed device value and nothing queued yet: the load writes them as well.
        "unknown": session.sync_summary(preset)["counts"].get("unknown", 0),
        "mute_wrap": session.settings.mute_wrap_bulk_apply,
    }


async def load_profile(
    session: DeviceSession,
    profile_id: int,
    preset: int | None = None,
    *,
    activate: bool = False,
    confirm: bool = False,
    expected_revision: int | None = None,
    timeout: float | None = BULK_TIMEOUT_S,
) -> LoadResult:
    """Load a profile into ``preset`` (default: the active preset).

    The preset's pending edits are committed first, so the load is its own history revision. Settings the app has
    no confirmed device value for are written too, so the preset holds exactly the profile. An inactive preset
    is staged (written when it becomes active); with ``activate`` the device is then switched to it and the staged
    changes are applied, held muted while they land (``MUTE_WRAP_BULK_APPLY``). Validation errors, the big-step
    guard and FIR loads raise like any other edit (``confirm`` allows the guarded ones).
    """
    profile, doc = await _profile_for_load(session, profile_id)
    preset = await _target(session, preset)
    if expected_revision is None:
        await session.commit(preset)
    edit = await session.replace_desired(
        preset,
        doc,
        confirm=confirm,
        expected_revision=expected_revision,
        source="profile",
        summary=f"Loaded profile “{profile.name}”",
        timeout=timeout,
        push_unknown=True,
    )
    session.log_event(
        "info",
        f"profile “{profile.name}” loaded into preset {preset + 1} ({len(edit.device_paths)} device change(s))",
        kind="profile",
        profile=profile.id,
        preset=preset,
    )
    repo = require_repo(session.repo)
    slots = await loaded_slots(repo)
    slots[(session.serial, preset)] = (profile.id, utcnow_iso())
    await store_slots(repo, slots)
    master = None
    if activate:
        master = await session.activate_preset(preset, flush=True, timeout=timeout)
    return LoadResult(profile, preset, edit, activated=activate, master=master)


def matching_presets(session: DeviceSession, doc: ConfigDoc) -> list[int]:
    """Presets of the session whose desired state equals ``doc``, delays compared in whole samples (empty until the
    state is loaded)."""
    if not session.loaded:
        return []
    doc = normalize_delays(doc, session.profile)
    return [p for p in range(session.profile.n_presets) if session.desired(p) == doc]


# --- history --------------------------------------------------------------------------------------------------------


async def revision(session: DeviceSession, preset: int, number: int) -> Revision:
    """One history revision of a preset, with its document."""
    await session.get_desired(preset)
    repo = require_repo(session.repo)
    row = await repo.get_revision(session.serial, preset, number)
    if row is None:
        raise RevisionNotFound(preset, number)
    return row


async def revert_preview(session: DeviceSession, preset: int, number: int) -> dict[str, Any]:
    row = await revision(session, preset, number)
    changes, writes = change_counts(await session.get_desired(preset), parse_doc(row.desired))
    master = session.master
    return {
        "preset": preset,
        "revision": number,
        "current": session.revisions.get(preset, 0),
        "active": master is not None and master.preset == preset,
        "changes": changes,
        "device_changes": writes,
        "mute_wrap": session.settings.mute_wrap_bulk_apply,
    }


async def revert_to(
    session: DeviceSession,
    preset: int,
    number: int,
    *,
    confirm: bool = False,
    expected_revision: int | None = None,
    timeout: float | None = BULK_TIMEOUT_S,
) -> EditResult:
    """Make revision ``number`` the preset's desired state again, as a new revision (history is never rewritten)."""
    row = await revision(session, preset, number)
    doc = parse_doc(row.desired)
    if expected_revision is None:
        await session.commit(preset)
    result = await session.replace_desired(
        preset,
        doc,
        confirm=confirm,
        expected_revision=expected_revision,
        source="revert",
        summary=f"Reverted to revision {number}",
        timeout=timeout,
    )
    if result.paths:
        session.log_event(
            "info",
            f"preset {preset + 1} reverted to revision {number} ({len(result.device_paths)} device change(s))",
            kind="revert",
            preset=preset,
            revision=number,
        )
    return result


# --- diff -----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DiffRow:
    """One difference. ``path`` is a leaf path, or a whole PEQ band / crossover group (``outputs.0.peq.3``) whose
    ``a``/``b`` are the band/group objects; ``fields`` are the leaf paths that differ."""

    path: str
    a: Any
    b: Any
    fields: list[str]

    @property
    def unit(self) -> bool:
        return _UNIT.match(self.path + ".") is not None

    def to_json(self) -> dict[str, Any]:
        return {"path": self.path, "a": self.a, "b": self.b, "fields": self.fields}


_MISSING = object()


def _value(doc: ConfigDoc, path: str) -> Any:
    try:
        value = get_path(doc, path)
    except (PathError, IndexError, KeyError, AttributeError, ValueError):
        return None
    dump = getattr(value, "model_dump", None)
    if dump is not None:
        return dump(mode="json")
    if isinstance(value, list):
        return [v.model_dump(mode="json") if hasattr(v, "model_dump") else v for v in value]
    return value


def diff_docs(a: ConfigDoc, b: ConfigDoc, *, paths: list[str] | None = None) -> list[DiffRow]:
    """Field differences from ``a`` to ``b`` in document order. A PEQ band or crossover group is one row (its design,
    coefficients and bypass belong together); ``schema_version`` is ignored."""
    rows: list[DiffRow] = []
    units: dict[str, DiffRow] = {}
    for path in paths if paths is not None else diff_paths(a, b):
        if path == "schema_version":
            continue
        match = _UNIT.match(path)
        if match is None:
            rows.append(DiffRow(path, _value(a, path), _value(b, path), [path]))
            continue
        unit = match.group(1)
        row = units.get(unit)
        if row is None:
            row = units[unit] = DiffRow(unit, _value(a, unit), _value(b, unit), [])
            rows.append(row)
        row.fields.append(path)
    return rows


@dataclass(frozen=True)
class DiffSide:
    """A resolved diff source: ``profile:ID``, ``preset:P`` (desired state) or ``revision:P:R`` (0-based P)."""

    source: str
    kind: str
    label: str
    doc: ConfigDoc
    profile_id: int | None = None
    preset: int | None = None
    revision: int | None = None


def parse_source(text: str) -> tuple[str, int, int | None]:
    """``profile:3`` -> ("profile", 3, None); ``preset:1`` -> ("preset", 1, None); ``revision:1:12`` ->
    ("revision", 1, 12)."""
    parts = (text or "").strip().split(":")
    kind = parts[0]
    nums = parts[1:]
    if not all(n.isascii() and n.isdigit() for n in nums):
        raise SourceError("bad diff source: use profile:ID, preset:P or revision:P:R")
    if kind in ("profile", "preset") and len(nums) == 1:
        return kind, int(nums[0]), None
    if kind == "revision" and len(nums) == 2:
        return kind, int(nums[0]), int(nums[1])
    raise SourceError("bad diff source: use profile:ID, preset:P or revision:P:R")


async def resolve_source(text: str, *, repo: Repo | None, session: DeviceSession | None) -> DiffSide:
    kind, n, rev = parse_source(text)
    source = text.strip()
    if kind == "profile":
        view = await get_profile(repo, n)
        return DiffSide(source, kind, f"Profile “{view.profile.name}”", profile_doc(view.profile), profile_id=n)
    if session is None:
        raise SourceError("no device: presets and their history can't be compared")
    if kind == "preset":
        return DiffSide(source, kind, f"Preset {n + 1}", await session.get_desired(n), preset=n)
    assert rev is not None
    row = await revision(session, n, rev)
    return DiffSide(source, kind, f"Preset {n + 1} revision {rev}", parse_doc(row.desired), preset=n, revision=rev)
