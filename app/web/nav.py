"""Sidebar navigation (docs/05 global layout; phase 9 design §3.4).

Entries are enabled only when the app actually serves their path, so pages added in later phases light up just by
registering their routes. Entries marked ``optional`` (pages that later phase 9 PRs add, such as Guided tasks) are left
out altogether until then. Channel pages use 1-based numbers in the URL (``/outputs/1`` is "Out 1").

`nav_groups` splits the entries into the sidebar's three groups: Everyday, All settings and Help. Both setting groups
are always visible; nothing is hidden behind a mode switch (design §15).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from starlette.routing import Match

from app.model.device_profile import DeviceProfile

# The sidebar groups (design §3.4), by entry path. Anything not listed goes under "All settings".
EVERYDAY = "Everyday"
ALL_SETTINGS = "All settings"
HELP = "Help"
GROUP_OF = {"/": EVERYDAY, "/tasks": EVERYDAY, "/profiles": EVERYDAY, "/help": HELP}


@dataclass(frozen=True, slots=True)
class NavItem:
    label: str
    path: str
    enabled: bool = True
    active: bool = False
    children: tuple["NavItem", ...] = field(default_factory=tuple)

    @property
    def expanded(self) -> bool:
        return self.active or any(c.active for c in self.children)


@dataclass(frozen=True, slots=True)
class NavGroup:
    label: str
    items: tuple[NavItem, ...]

    @property
    def id(self) -> str:
        return "nav-" + self.label.lower().replace(" ", "-")


def _served(request: Request, path: str) -> bool:
    scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in request.app.routes)


def channel_names(prefix: str, count: int) -> list[str]:
    return [f"{prefix} {n}" for n in range(1, count + 1)]


def _named(defaults: list[str], kind: str, channel_meta: Mapping[tuple[str, int], Any]) -> list[str]:
    """Replace the default "In 1" / "Out 1" labels with names saved in Settings ("Shakers")."""
    names = []
    for i, default in enumerate(defaults):
        meta = channel_meta.get((kind, i))
        names.append(getattr(meta, "name", None) or default)
    return names


def build_nav(
    request: Request, profile: DeviceProfile | None, channel_meta: Mapping[tuple[str, int], Any] | None = None
) -> list[NavItem]:
    current = request.url.path.removeprefix(request.scope.get("root_path", "")) or "/"
    n_inputs = profile.n_inputs if profile else 0
    n_outputs = profile.n_outputs if profile else 0

    def item(label: str, path: str, children: tuple[NavItem, ...] = ()) -> NavItem:
        return NavItem(
            label=label,
            path=path,
            enabled=_served(request, path),
            active=current == path,
            children=children,
        )

    def optional(label: str, path: str) -> list[NavItem]:
        """An entry for a page a later phase 9 PR adds: listed only once the app serves it."""
        return [item(label, path)] if _served(request, path) else []

    meta = channel_meta or {}
    input_names = _named(channel_names("In", n_inputs), "input", meta)
    output_names = _named(channel_names("Out", n_outputs), "output", meta)
    inputs = tuple(item(name, f"/inputs/{n}") for n, name in enumerate(input_names, start=1))
    outputs = tuple(item(name, f"/outputs/{n}") for n, name in enumerate(output_names, start=1))
    return [
        # Everyday
        item("Home", "/"),
        *optional("Guided tasks", "/tasks"),
        item("Profiles", "/profiles"),
        # All settings
        *optional("Meters & graph", "/meters"),
        item("Routing", "/routing"),
        item("Inputs", "/inputs", inputs),
        item("Outputs", "/outputs", outputs),
        item("Combined response", "/system"),
        item("Device & sync", "/device"),
        item("Settings", "/settings"),
        # Help
        item("Glossary", "/help"),
    ]


def nav_groups(items: list[NavItem]) -> list[NavGroup]:
    """The sidebar's groups in order (Everyday, All settings, Help), each with its entries in `build_nav` order."""
    order = (EVERYDAY, ALL_SETTINGS, HELP)
    grouped: dict[str, list[NavItem]] = {label: [] for label in order}
    for entry in items:
        grouped[GROUP_OF.get(entry.path, ALL_SETTINGS)].append(entry)
    return [NavGroup(label, tuple(grouped[label])) for label in order if grouped[label]]
