"""Seeding the app under test through its own HTTP API (no browser), so a test starts from a known device state."""

import time
from collections.abc import Callable
from typing import Any

import httpx


def wait_until[T](check: Callable[[], T], timeout: float = 10.0, what: str = "the condition", page: Any = None) -> T:
    """Polls ``check`` until it returns something truthy (and returns that), for writes that land asynchronously.

    Pass ``page`` when ``check`` depends on a Playwright event handler (e.g. dialogs): the sync API only delivers
    events while it is waiting itself, so the pause is then ``page.wait_for_timeout`` instead of ``time.sleep``."""
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:g} s waiting for {what}")
        if page is not None:
            page.wait_for_timeout(100)
        else:
            time.sleep(0.1)


class Api:
    def __init__(self, base_url: str, serial: int) -> None:
        self.serial = serial
        self.http = httpx.Client(base_url=base_url, timeout=15)
        self.dev = f"/api/v1/devices/{serial}"

    def close(self) -> None:
        self.http.close()

    def _ok(self, response: httpx.Response) -> Any:
        if response.status_code >= 300:
            raise AssertionError(
                f"{response.request.method} {response.request.url}: {response.status_code} {response.text}"
            )
        return response.json() if response.content else None

    # --- device ------------------------------------------------------------------------------------------------------

    def adopt_factory_defaults(self) -> dict:
        """Set up the mock as a device at factory defaults ("trust" with the explicit factory-defaults confirmation)."""
        return self._ok(self.http.post(f"{self.dev}/adopt", json={"mode": "trust", "confirm_defaults": True}))

    def name_channel(self, kind: str, index: int, name: str) -> None:
        """Rename a channel the way its page header does (kind "input" or "output", index from 0)."""
        response = self.http.post(
            f"/ui/devices/{self.serial}/channels/{kind}/{index}/name",
            data={"name": name},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200, response.text

    def master(self, **values: Any) -> dict:
        return self._ok(self.http.put(f"{self.dev}/master", json=values))

    # --- presets (numbered from 0, like the API) ----------------------------------------------------------------------

    def preset(self, preset: int = 0) -> dict:
        """The preset's sync summary plus its ``desired`` document."""
        return self._ok(self.http.get(f"{self.dev}/presets/{preset}"))

    def desired(self, preset: int = 0) -> dict:
        return self.preset(preset)["desired"]

    def wait_synced(self, preset: int = 0, timeout: float = 10.0) -> dict:
        """Waits until nothing of the preset is still being sent to the device."""

        def done() -> dict | None:
            summary = self.preset(preset)
            return summary if summary["counts"]["pending"] == 0 else None

        return wait_until(done, timeout, "the writes to finish")

    def patch_output(self, index: int, patch: dict, preset: int = 0) -> dict:
        """A committed, pre-confirmed edit of one output (e.g. {"gain_db": 12})."""
        return self._ok(
            self.http.patch(
                f"{self.dev}/presets/{preset}/outputs/{index}", params={"confirm": "true", "commit": "true"}, json=patch
            )
        )

    def peq_band(self, side: str, channel: int, band: int, preset: int = 0) -> dict:
        """One PEQ band as the app stores it (side "inputs" or "outputs", everything from 0)."""
        return self._ok(self.http.get(f"{self.dev}/presets/{preset}/{side}/{channel}/peq/{band}"))

    def put_peq_band(self, side: str, channel: int, band: int, body: dict, preset: int = 0) -> dict:
        """Set one PEQ band, pre-confirmed (e.g. {"design": {"type": "peaking", ...}, "bypass": False})."""
        return self._ok(
            self.http.put(
                f"{self.dev}/presets/{preset}/{side}/{channel}/peq/{band}",
                params={"confirm": "true", "commit": "true"},
                json=body,
            )
        )

    def compressor(self, channel: int, preset: int = 0) -> dict:
        return self._ok(self.http.get(f"{self.dev}/presets/{preset}/outputs/{channel}/compressor"))

    # --- profiles ----------------------------------------------------------------------------------------------------

    def create_profile(self, name: str, config: dict) -> dict:
        return self._ok(self.http.post("/api/v1/profiles", json={"name": name, "config": config}))
