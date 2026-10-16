"""Async client for the minidspd HTTP API.

Every call either returns a parsed result or raises a `DaemonError` subclass. For writes, any exception means the
outcome is unknown: the daemon applies a config payload in order and aborts at the first error.
"""

import json
import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ValidationError

from app.model.master import DaemonDevice, MasterStatus, StatusSummary

log = logging.getLogger(__name__)

# Daemon error types (FormattedError.error.type) that mean "this device isn't there right now".
_DEVICE_GONE = {"DeviceNotReady", "DeviceIndexOutOfRange"}

# The daemon's master volume range (dB) and the device's volume step.
MASTER_VOLUME_MIN_DB = -127.0
MASTER_VOLUME_MAX_DB = 0.0
MASTER_VOLUME_STEP_DB = 0.5

# minidsp-rs `Source` variants, keyed by lowercase name.
_SOURCE_NAMES = {
    name.lower(): name
    for name in (
        "NotInstalled",
        "Analog",
        "Toslink",
        "Spdif",
        "Usb",
        "Aesebu",
        "Rca",
        "Xlr",
        "Lan",
        "I2S",
        "Bluetooth",
        "Hdmi",
    )
}


class DaemonError(Exception):
    """Base class for every daemon client failure."""


class DaemonUnavailable(DaemonError):
    """The daemon could not be reached, timed out, or is still initializing."""


class DeviceNotFound(DaemonError):
    """The requested device (by index or serial) is not present on the daemon."""

    def __init__(self, message: str, *, body: str | None = None) -> None:
        super().__init__(message)
        self.body = body


class DaemonRejected(DaemonError):
    """The daemon answered with an error status. ``body`` is the raw response text."""

    def __init__(self, status_code: int, body: str, *, error_type: str | None = None, message: str | None = None):
        super().__init__(message or body or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body
        self.error_type = error_type
        self.message = message


def _parse_error(body: str) -> tuple[str | None, str | None]:
    try:
        data = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    error = data.get("error")
    error_type = error.get("type") if isinstance(error, dict) else None
    message = data.get("message")
    return error_type, message if isinstance(message, str) else None


class DaemonClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 5.0,
        preset_timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._preset_timeout = preset_timeout
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "DaemonClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # --- low level ------------------------------------------------------------------------------------------------

    async def _request(self, method: str, path: str, *, json_body: Any = None, timeout: float | None = None) -> str:
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise DaemonUnavailable(f"minidspd timed out on {method} {path}") from exc
        except httpx.TransportError as exc:
            raise DaemonUnavailable(f"minidspd unreachable at {self.base_url}: {exc}") from exc

        body = response.text
        if response.is_success:
            return body
        error_type, message = _parse_error(body)
        if error_type in _DEVICE_GONE:
            raise DeviceNotFound(message or error_type, body=body)
        if error_type == "ApplicationStillInitializing":
            raise DaemonUnavailable(message or "minidspd is still initializing")
        raise DaemonRejected(response.status_code, body, error_type=error_type, message=message)

    async def _get_json(self, path: str) -> Any:
        return _loads(await self._request("GET", path), path)

    async def _post_master_json(self, path: str, body: Any = None, *, timeout: float | None = None) -> MasterStatus:
        text = await self._request("POST", path, json_body=body, timeout=timeout)
        return _parse(MasterStatus, _loads(text, path), path) if text.strip() else MasterStatus()

    # --- API ------------------------------------------------------------------------------------------------------

    async def list_devices(self) -> list[DaemonDevice]:
        data = await self._get_json("/devices")
        if not isinstance(data, list):
            raise DaemonRejected(200, json.dumps(data), message="unexpected /devices response from minidspd")
        return [_parse(DaemonDevice, d, "/devices") for d in data]

    async def get_status(self, index: int) -> StatusSummary:
        path = f"/devices/{index}"
        return _parse(StatusSummary, await self._get_json(path), path)

    async def post_master(self, index: int, master: MasterStatus | Mapping[str, Any]) -> MasterStatus:
        """Apply master fields (preset, source, volume, mute, dirac); returns the master status read back."""
        body = master.model_dump(exclude_none=True) if isinstance(master, MasterStatus) else dict(master)
        timeout = self._preset_timeout if body.get("preset") is not None else None
        return await self._post_master_json(f"/devices/{index}", body, timeout=timeout)

    async def post_config(self, index: int, config: Mapping[str, Any]) -> None:
        """Apply a (partial) Config payload. On any exception, treat every path in the payload as unknown."""
        await self._request("POST", f"/devices/{index}/config", json_body=dict(config))

    # minidspd 0.1.12 (the current release) only has POST /devices/{i} for master writes: the /volume/*, /source/*
    # and /preset/* shortcuts were added upstream later and return 404 there. These helpers are built on post_master.

    async def volume_up(self, index: int, *, current: float | None = None) -> MasterStatus:
        return await self._volume_step(index, MASTER_VOLUME_STEP_DB, current)

    async def volume_down(self, index: int, *, current: float | None = None) -> MasterStatus:
        return await self._volume_step(index, -MASTER_VOLUME_STEP_DB, current)

    async def _volume_step(self, index: int, delta: float, current: float | None) -> MasterStatus:
        """Absolute write of ``current + delta`` clamped to the daemon's range (it doesn't clamp, and loses the sign
        of positive values). ``current`` defaults to a fresh status read."""
        if current is None:
            current = (await self.get_status(index)).master.volume
            if current is None:
                raise DaemonRejected(200, "", message="minidspd did not report the master volume")
        target = min(MASTER_VOLUME_MAX_DB, max(MASTER_VOLUME_MIN_DB, current + delta))
        return await self.post_master(index, MasterStatus(volume=target))

    async def toggle_mute(self, index: int, *, current: bool | None = None) -> MasterStatus:
        if current is None:
            current = (await self.get_status(index)).master.mute
            if current is None:
                raise DaemonRejected(200, "", message="minidspd did not report the master mute state")
        return await self.post_master(index, MasterStatus(mute=not current))

    async def set_source(self, index: int, source: str) -> MasterStatus:
        # JSON bodies need the serde variant name ("Toslink"); "toslink" is a ParseError.
        name = _SOURCE_NAMES.get(source.lower(), source)
        return await self.post_master(index, MasterStatus(source=name))

    async def set_preset(self, index: int, preset: int) -> MasterStatus:
        return await self.post_master(index, MasterStatus(preset=preset))

    async def resolve_index(self, serial: int) -> tuple[int, DaemonDevice]:
        """Daemon index of the device with this serial (indexes can change after USB re-enumeration)."""
        for index, device in enumerate(await self.list_devices()):
            if device.serial == serial:
                return index, device
        raise DeviceNotFound(f"device {serial} is not connected to minidspd")

    def ws_url(self, index: int, *, levels: bool, poll: bool) -> str:
        """Websocket URL for the status stream of device ``index``."""
        parts = urlsplit(self.base_url)
        scheme = "wss" if parts.scheme == "https" else "ws"
        params = [name for name, on in (("levels", levels), ("poll", poll)) if on]
        query = "&".join(f"{p}=true" for p in params)
        return urlunsplit((scheme, parts.netloc, f"{parts.path}/devices/{index}", query, ""))


def _loads(text: str, path: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise DaemonRejected(200, text, message=f"invalid JSON from minidspd on {path}") from exc


def _parse[M: BaseModel](model: type[M], data: Any, path: str) -> M:
    """Validate a daemon response; a payload of the wrong shape is a `DaemonRejected` like any other bad answer."""
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        message = f"unexpected response from minidspd on {path}: {exc}"
        raise DaemonRejected(200, json.dumps(data), message=message) from exc
