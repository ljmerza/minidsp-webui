"""Sidebar navigation (docs/05 global layout).

Entries are enabled only when the app actually serves their path, so pages added in later phases light up just by
registering their routes. Channel pages use 1-based numbers in the URL (``/outputs/1`` is "Out 1").
"""

from dataclasses import dataclass, field

from fastapi import Request
from starlette.routing import Match

from app.model.device_profile import DeviceProfile


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


def _served(request: Request, path: str) -> bool:
    scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in request.app.routes)


def channel_names(prefix: str, count: int) -> list[str]:
    return [f"{prefix} {n}" for n in range(1, count + 1)]


def build_nav(request: Request, profile: DeviceProfile | None) -> list[NavItem]:
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

    inputs = tuple(item(name, f"/inputs/{n}") for n, name in enumerate(channel_names("In", n_inputs), start=1))
    outputs = tuple(item(name, f"/outputs/{n}") for n, name in enumerate(channel_names("Out", n_outputs), start=1))
    return [
        item("Dashboard", "/"),
        item("Routing", "/routing"),
        item("Inputs", "/inputs", inputs),
        item("Outputs", "/outputs", outputs),
        item("System", "/system"),
        item("Profiles", "/profiles"),
        item("Device", "/device"),
        item("Settings", "/settings"),
    ]
