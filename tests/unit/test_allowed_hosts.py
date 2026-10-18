"""ALLOWED_HOSTS: refuse requests whose Host header is a name the user didn't list (DNS rebinding). On by default;
``*`` opts out."""

import pytest

from .test_app_wiring import running

REBOUND = {"Host": "attacker.example:8480", "Origin": "http://attacker.example:8480", "Sec-Fetch-Site": "same-origin"}


async def test_a_rebound_host_is_refused_when_allowed_hosts_is_set(settings, client, daemon):
    settings.allowed_hosts = "minidsp.example.lan"
    async with running(settings, client, daemon) as (_, http):
        assert (await http.get("/api/v1/backup/db", headers=REBOUND)).status_code == 400
        assert (await http.post("/api/v1/backup/import/preview", content=b"{}", headers=REBOUND)).status_code == 400
        for host in ("minidsp.example.lan:8480", "MINIDSP.example.lan", "192.0.2.10:8480", "[2001:db8::1]:8480"):
            assert (await http.get("/api/v1/devices", headers={"Host": host})).status_code == 200, host
        assert (await http.get("/healthz", headers={"Host": "127.0.0.1:8480"})).status_code == 200
        assert (await http.get("/healthz", headers={"Host": "localhost"})).status_code == 200


async def test_by_default_only_ip_addresses_and_localhost_are_served(settings, client, daemon):
    assert settings.allowed_hosts == ""
    async with running(settings, client, daemon) as (_, http):
        for host in ("192.0.2.10:8480", "192.0.2.10", "[2001:db8::1]:8480", "[::1]", "localhost:8480", "LOCALHOST"):
            assert (await http.get("/api/v1/devices", headers={"Host": host})).status_code == 200, host
        response = await http.get("/api/v1/devices", headers={"Host": "attacker.example:8480"})
        assert response.status_code == 400 and "ALLOWED_HOSTS" in response.text
        assert (await http.get("/api/v1/backup/db", headers=REBOUND)).status_code == 400
        assert (await http.get("/", headers={"Host": "minidsp.example.lan"})).status_code == 400
        # the image's HEALTHCHECK asks 127.0.0.1
        assert (await http.get("/healthz", headers={"Host": "127.0.0.1:8480"})).status_code == 200


async def test_a_star_turns_the_check_off(settings, client, daemon):
    settings.allowed_hosts = "*"
    async with running(settings, client, daemon) as (_, http):
        assert (await http.get("/api/v1/devices", headers={"Host": "attacker.example:8480"})).status_code == 200


@pytest.mark.parametrize(
    "value, expected",
    [
        ("", frozenset()),
        ("  ", frozenset()),
        ("Minidsp.LAN., other.example", frozenset({"minidsp.lan", "other.example"})),
        ("*", None),
        ("minidsp.lan, *", None),
    ],
)
def test_parse_allowed_hosts(value, expected):
    from app.api.hosts import parse_allowed_hosts

    assert parse_allowed_hosts(value) == expected


@pytest.mark.parametrize(
    "host, allowed",
    [
        ("10.0.0.5", True),
        ("10.0.0.5:8480", True),
        ("[::1]:8480", True),
        ("localhost:8480", True),
        ("minidsp.lan.", True),
        ("evil.example", False),
        ("10.0.0.5.nip.io", False),
        ("", True),  # no Host header: not a browser
    ],
)
def test_host_allowed(host, allowed):
    from app.api.hosts import host_allowed

    assert host_allowed(host, {"minidsp.lan"}) is allowed
