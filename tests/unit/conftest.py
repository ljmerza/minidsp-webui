import pytest
import respx

from app.bus import Bus
from app.config import Settings
from app.daemon.client import DaemonClient

# Nothing listens on port 1, so the monitor's websocket fails fast; HTTP is mocked with respx.
DAEMON_URL = "http://127.0.0.1:1"
SERIAL = 911111

DEVICE_JSON = {
    "url": "mock:?serial=911111",
    "product_name": "2x4HD",
    "version": {"hw_id": 10, "dsp_version": 100, "serial": SERIAL, "fw_major": 1, "fw_minor": 75},
}

MASTER_JSON = {"preset": 0, "source": "Analog", "volume": -20.0, "mute": False, "dirac": False}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        minidspd_url=DAEMON_URL,
        db_path=str(tmp_path / "state.sqlite3"),
        preset_switch_timeout_s=2.0,
        monitor_backoff_min_s=0.05,
        monitor_backoff_max_s=0.2,
        levels_idle_grace_s=0.05,
        discovery_interval_s=30.0,
    )


@pytest.fixture
def bus() -> Bus:
    return Bus()


@pytest.fixture
async def client():
    c = DaemonClient(DAEMON_URL, timeout=1.0, preset_timeout=2.0)
    yield c
    await c.aclose()


@pytest.fixture
def daemon():
    with respx.mock(base_url=DAEMON_URL, assert_all_called=False) as mock:
        yield mock
