import re

import httpx
import pytest

from app.config import Settings
from app.model.master import DaemonDevice, MasterStatus, StatusSummary
from app.sync.session import SessionState

from .conftest import SERIAL, FakeManager, FakeSession, make_app


def _attr(html: str, element_id: str) -> str:
    """The opening tag of the element with this id."""
    match = re.search(rf'<[a-z]+[^>]*\bid="{element_id}"[^>]*>', html, re.S)
    assert match, f"no element with id {element_id}"
    return match.group(0)


async def test_dashboard_renders_header_meters_and_cards(http):
    response = await http.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text

    # persistent header
    assert f"#{SERIAL}" in html and "2x4HD" in html
    assert 'class="conn conn-ok"' in html and "Connected" in html
    assert 'value="-20.0"' in _attr(html, "master-volume")
    assert 'hx-trigger="input changed delay:80ms"' in _attr(html, "master-volume")
    assert f'hx-post="/ui/devices/{SERIAL}/master/volume"' in _attr(html, "master-volume")
    assert 'aria-valuetext="−20.0 dB"' in _attr(html, "master-volume")
    for n in range(1, 5):
        assert f'id="preset-{n}"' in html
    active = _attr(html, "preset-2")  # device preset 1 is shown as "2"
    assert 'aria-pressed="true"' in active and "disabled" in active
    assert 'aria-pressed="false"' in _attr(html, "preset-1")
    assert "hx-confirm=" in _attr(html, "preset-1")
    assert re.search(r'<option value="Analog" selected>', html)
    mute = _attr(html, "master-mute")
    assert 'aria-pressed="false"' in mute and '"mute": "true"' in mute
    assert "not tracked yet" in html  # sync summary placeholder

    # meters: 2 inputs + 4 outputs, fed by SSE levels
    assert 'hx-trigger="sse:levels"' in html
    for name in ("In 1", "In 2", "Out 1", "Out 2", "Out 3", "Out 4"):
        assert f'aria-label="{name} level"' in html
    # idle clip boxes are neutral and always read "CLIP"; meters.js turns one red (is-on) when it latches
    assert html.count('class="clip-led"') == 6
    assert html.count('<span aria-hidden="true">CLIP</span>') == 6
    assert "&ndash;" not in html.split('id="levels-title"')[1].split("</section>")[0]
    assert "A grey CLIP box turns red" in html
    assert html.count('class="output-card"') == 4

    # output cards: re-fetched on sync / desired events; dashboard.js handles preset switches and the quick controls
    box = re.search(r"<div[^>]*data-output-cards[^>]*>", html, re.S).group(0)
    assert f'hx-get="/ui/dashboard/outputs?serial={SERIAL}"' in box
    assert 'hx-trigger="sse:sync delay:300ms, sse:desired delay:300ms, refresh"' in box
    assert 'x-data="outputCards" hx-trigger="sse:master"' in html
    assert "/static/js/dashboard.js" in html
    assert 'data-action="mute"' not in html  # no preset state loaded: no quick controls

    # live feed + vendored assets only
    topics = "levels%2Cmaster%2Cdevice%2Csync%2Cdesired"
    assert f'sse-connect="/api/v1/events?topics={topics}&amp;serial={SERIAL}"' in html
    assert f'hx-get="/ui/header?serial={SERIAL}"' in html
    for src in re.findall(r'<script[^>]*src="([^"]+)"', html):
        assert src.startswith("/static/"), src
    assert "/static/vendor/htmx/htmx.min.js" in html and "/static/vendor/alpine/alpine.min.js" in html


async def test_dashboard_initial_levels_are_passed_to_the_meter_component(session, http):
    session.status = StatusSummary(master=session.master, input_levels=[-18.0, -104.0], output_levels=[-6.5, -20.0])
    html = (await http.get("/")).text
    assert '"i": [-18.0, -104.0]' in html and '"o": [-6.5, -20.0]' in html


async def test_dashboard_without_device_when_daemon_is_down():
    manager = FakeManager(daemon_up=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(manager)), base_url="http://t") as http:
        response = await http.get("/")
    assert response.status_code == 200
    html = response.text
    assert "minidspd unreachable" in html and 'role="alert"' in html
    assert 'id="master-volume"' not in html and 'id="preset-1"' not in html
    assert 'data-serial=""' in html
    assert 'sse-connect="/api/v1/events?topics=levels%2Cmaster%2Cdevice%2Csync%2Cdesired"' in html


async def test_unsupported_device_names_are_listed_and_escaped():
    manager = FakeManager(unsupported=[DaemonDevice(url="usb:1", product_name="<script>x</script>")])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(manager)), base_url="http://t") as http:
        html = (await http.get("/")).text
        device = (await http.get("/device")).text
    assert "No supported device found" in html
    assert "<script>x</script>" not in html and "&lt;script&gt;x&lt;/script&gt;" in html
    assert "Unsupported devices" in device and "<script>x</script>" not in device


@pytest.mark.parametrize(
    ("state", "switching", "monitor", "level", "label"),
    [
        (SessionState.CONNECTED, None, False, "warn", "Connecting to device stream"),
        (SessionState.ABSENT, None, True, "bad", "Device not found on minidspd"),
        (SessionState.DAEMON_DOWN, None, True, "bad", "minidspd unreachable"),
        (SessionState.STARTING, None, True, "warn", "Starting"),
        (SessionState.CONNECTED, 2, True, "busy", "Switching to preset 3"),
    ],
)
async def test_header_connection_states(session, http, state, switching, monitor, level, label):
    session.state = state
    session.preset_switching = switching
    session.monitor_connected = monitor
    html = (await http.get("/ui/header")).text
    assert f'class="conn conn-{level}"' in html and label in html
    if state != SessionState.CONNECTED or switching is not None:
        for element_id in ("master-volume", "master-mute", "master-source", "preset-1", "volume-up"):
            assert "disabled" in _attr(html, element_id), element_id


async def test_header_fragment_is_a_fragment(http):
    response = await http.get("/ui/header", params={"serial": SERIAL})
    assert response.status_code == 200
    assert "<html" not in response.text and "<body" not in response.text
    assert f'data-serial="{SERIAL}"' in response.text
    assert response.headers["cache-control"] == "no-store"


async def test_header_with_unknown_master_disables_volume(session, http):
    session.master = None
    html = (await http.get("/ui/header")).text
    assert "disabled" in _attr(html, "master-volume")
    assert "x-data" not in html.split('id="master-volume"')[0].rsplit("hdr-volume", 1)[1]
    assert re.search(r'<option value="" selected disabled>', html)
    assert "hx-vals" not in _attr(html, "master-mute")  # unknown state: the button toggles


async def test_muted_header(session, http):
    session.master = MasterStatus(preset=0, source="Usb", volume=-127.0, mute=True)
    html = (await http.get("/ui/header")).text
    mute = _attr(html, "master-mute")
    assert 'aria-pressed="true"' in mute and "is-muted" in mute and '"mute": "false"' in mute
    assert re.search(r'<option value="Usb" selected>', html)


async def test_device_page(http):
    response = await http.get("/device")
    assert response.status_code == 200
    html = response.text
    for text in (
        str(SERIAL),
        "1.75",
        "Hardware ID",
        "DSP version",
        "http://daemon.invalid:5380",
        "mock:?serial=911111",
    ):
        assert text in html, text
    assert "2 in, 4 out" in html and "96 kHz" in html and "79.8 ms" in html
    assert "−127.0 dB to 0.0 dB" in html
    assert 'hx-trigger="sse:device"' in html
    # the device page doesn't ask for levels, so the daemon can drop them; sync/log/verify share the one feed
    topics = "master%2Cdevice%2Csync%2Clog%2Cverify"
    assert f'sse-connect="/api/v1/events?topics={topics}&amp;serial={SERIAL}"' in html
    assert html.count("sse-connect=") == 1


async def test_device_info_fragment(http):
    response = await http.get("/ui/device-info")
    assert response.status_code == 200
    assert "<html" not in response.text and "Firmware" in response.text


async def test_device_page_shows_daemon_version_and_connection_uptime(session, manager, http):
    html = (await http.get("/ui/device-info")).text
    assert re.search(r"<dt>minidspd version</dt><dd>—</dd>", html)  # not read (yet)
    assert "Connection uptime" not in html  # the fake session doesn't report it

    manager.daemon_version = "0.1.12"
    info = session.info()
    session.info = lambda: {**info, "connected_for_s": 3 * 3600 + 12 * 60 + 5}
    html = (await http.get("/ui/device-info")).text
    assert re.search(r"<dt>minidspd version</dt><dd>0\.1\.12</dd>", html)
    assert re.search(r"<dt>Connection uptime</dt><dd>connected for 3 h 12 min</dd>", html)

    session.monitor_connected = False
    session.info = lambda: {**info, "monitor_connected": False, "connected_for_s": None}
    assert "Connection uptime" not in (await http.get("/ui/device-info")).text


async def test_daemon_version_without_a_device():
    manager = FakeManager(daemon_up=True)
    manager.daemon_version = "0.1.12"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(manager)), base_url="http://t") as http:
        html = (await http.get("/device")).text
    assert "No device" in html and re.search(r"<dt>minidspd version</dt><dd>0\.1\.12</dd>", html)


async def test_dashboard_outputs_fragment(http):
    response = await http.get("/ui/dashboard/outputs", params={"serial": SERIAL})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    html = response.text
    assert "<html" not in html and html.count('class="output-card"') == 4
    assert "<h3>Out 1</h3>" in html and "not tracked yet" in html
    assert 'data-preset=""' in html and "data-edit-url" not in html


async def test_graph_defaults_reach_every_page(manager):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(manager)), base_url="http://t") as http:
        html = (await http.get("/")).text
    body = re.search(r"<body[^>]*>", html, re.S).group(0)
    assert 'data-graph-view=""' in body and 'data-graph-db="24"' in body  # "auto": each page keeps its own view
    assert '"view": "sub"' in html and "5–300 Hz, active preset" in html

    settings = Settings(_env_file=None, minidspd_url="http://daemon.invalid:5380", graph_view="full", graph_db_range=30)
    app = make_app(manager, settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        html = (await http.get("/")).text
        device = (await http.get("/device")).text
    body = re.search(r"<body[^>]*>", html, re.S).group(0)
    assert 'data-graph-view="full"' in body and 'data-graph-db="30"' in body
    assert '"view": "full"' in html and "10 Hz–20 kHz, active preset" in html
    assert 'data-graph-view="full"' in device


async def test_device_page_without_device():
    manager = FakeManager(daemon_up=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(manager)), base_url="http://t") as http:
        html = (await http.get("/device")).text
    assert "No device" in html and "connecting" in html


@pytest.mark.parametrize("path", ["/", "/device"])
async def test_unknown_serial_is_404(http, path):
    response = await http.get(path, params={"serial": 1})
    assert response.status_code == 404
    assert "Unknown device" in response.text


async def test_explicit_serial_selects_the_session():
    first, second = FakeSession(serial=1), FakeSession(serial=2)
    manager = FakeManager(sessions={1: first, 2: second})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(manager)), base_url="http://t") as http:
        html = (await http.get("/", params={"serial": 2})).text
    assert 'data-serial="2"' in html and 'hx-post="/ui/devices/2/master/volume"' in html


async def test_nav_links_only_served_pages(http):
    html = (await http.get("/device")).text
    nav = html.split('id="sidenav"', 1)[1].split("</nav>", 1)[0]
    assert '<a href="/"' in nav
    assert '<a href="/device" aria-current="page">Device</a>' in nav
    for label in ("Routing", "System", "Profiles", "Settings", "Out 1", "In 2"):
        assert re.search(rf'<span class="nav-disabled"[^>]*>{label}</span>', nav), label


async def test_root_path_prefixes_urls(manager):
    app = make_app(manager)
    transport = httpx.ASGITransport(app=app, root_path="/dsp")
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as http:
        html = (await http.get("/dsp/")).text
    assert 'href="/dsp/static/css/app.css' in html
    assert f'hx-post="/dsp/ui/devices/{SERIAL}/master/volume"' in html
    assert 'sse-connect="/dsp/api/v1/events?' in html


async def test_static_assets_are_served(http):
    for path in (
        "/static/css/app.css",
        "/static/js/app.js",
        "/static/js/master.js",
        "/static/js/meters.js",
        "/static/js/dashboard.js",
        "/static/vendor/htmx/htmx.min.js",
        "/static/vendor/htmx/sse.min.js",
        "/static/vendor/alpine/alpine.min.js",
        "/static/vendor/uplot/uPlot.iife.min.js",
        "/static/vendor/uplot/uPlot.min.css",
    ):
        response = await http.get(path)
        assert response.status_code == 200, path
