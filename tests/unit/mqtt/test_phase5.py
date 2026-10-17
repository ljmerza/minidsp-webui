"""Phase 5 MQTT entities against a loaded session with a real database and the stateful fake minidspd: per-output
gain/mute/delay, the Profile select, push-all, level sensors and "Remove from HA"."""

import asyncio
import json

import pytest

from app.bus import LEVELS, LOG
from app.model.config_doc import default_config_doc, set_path
from app.model.device_profile import M2X4HD
from app.model.master import DaemonDevice, MasterStatus
from app.mqtt import MqttBridge, ProfileCatalog, remove_from_home_assistant
from app.mqtt import payloads as p
from app.state import Database, Repo
from app.sync import profiles as profiles_svc
from app.sync.session import DeviceSession, SessionManager, SessionState
from tests.unit.conftest import DEVICE_JSON, SERIAL
from tests.unit.mqtt.test_bridge import FakeBroker, until
from tests.unit.session.conftest import FakeDaemon

BASE = "minidsp-webui"
STATE = f"{BASE}/{SERIAL}/state"
LEVELS_TOPIC = f"{BASE}/{SERIAL}/levels"
DISCOVERY = f"homeassistant/{{}}/minidsp_{SERIAL}/{{}}/config"
PROFILE_TOPIC = DISCOVERY.format("select", "profile")


def cmd(entity: str) -> str:
    return f"{BASE}/{SERIAL}/cmd/{entity}"


@pytest.fixture
def fake(daemon) -> FakeDaemon:
    return FakeDaemon(daemon)


@pytest.fixture
async def repo(tmp_path):
    db = await Database.open(tmp_path / "state" / "test.sqlite3")
    yield Repo(db)
    await db.close()


@pytest.fixture
def mqtt_settings(settings):
    return settings.model_copy(
        update={
            "mqtt_enabled": True,
            "mqtt_host": "broker.invalid",
            "write_coalesce_ms": 5,
            "mute_wrap_bulk_apply": False,
        }
    )


@pytest.fixture
async def session(client, bus, mqtt_settings, repo, fake):
    s = DeviceSession(
        SERIAL,
        M2X4HD,
        client=client,
        bus=bus,
        settings=mqtt_settings,
        device=DaemonDevice.model_validate(DEVICE_JSON),
        daemon_index=0,
        repo=repo,
        commit_idle_s=0.05,
    )
    s.state = SessionState.CONNECTED
    s.monitor.connected = True
    s.monitor.update_master(MasterStatus.model_validate(fake.master))
    await s.ensure_loaded()
    s.writer.start()
    yield s
    await s.stop()


@pytest.fixture
def manager(client, bus, mqtt_settings, session):
    m = SessionManager(client, bus, mqtt_settings)
    m.sessions[SERIAL] = session
    return m


@pytest.fixture
def broker():
    return FakeBroker()


def make_bridge(manager, bus, settings, broker, **kwargs) -> MqttBridge:
    return MqttBridge(
        manager,
        bus,
        settings,
        client_factory=broker.factory,
        debounce_s=0.02,
        max_delay_s=0.1,
        backoff_min_s=0.01,
        backoff_max_s=0.05,
        **kwargs,
    )


@pytest.fixture
async def bridge(manager, bus, mqtt_settings, broker):
    b = make_bridge(manager, bus, mqtt_settings, broker)
    b.start()
    await asyncio.wait_for(b.connected.wait(), 2)
    yield b
    await b.stop()


def discovery(broker: FakeBroker, component: str, object_id: str) -> dict:
    return json.loads(broker.retained[DISCOVERY.format(component, object_id)])


def logs(bus):
    return bus.subscribe([LOG])


def messages(sub) -> list[str]:
    out = []
    while (event := sub.get_nowait()) is not None:
        out.append(event.data["message"])
    return out


async def settle(session: DeviceSession) -> None:
    await session.writer.wait_idle(0, timeout=3)


async def synced(session: DeviceSession) -> None:
    """Every path of preset 1 confirmed on the device, as after adoption."""
    await session.push_all(0)
    await settle(session)


# --- per-output controls ------------------------------------------------------------------------------------------


async def test_output_state_comes_from_the_active_presets_desired_state(bridge, broker, session):
    assert broker.state()["outputs"] == [{"gain": 0.0, "mute": False, "delay": 0.0}] * 4
    await session.set_value(0, "outputs.1.gain_db", -4.0, source="ui")  # a UI edit (bus `desired`) re-publishes
    await until(lambda: broker.state()["outputs"][1]["gain"] == -4.0)
    await session.set_value(2, "outputs.1.gain_db", -9.0, source="ui")  # an inactive preset changes nothing
    await asyncio.sleep(0.15)
    assert broker.state()["outputs"][1]["gain"] == -4.0


async def test_gain_command_goes_through_the_edit_path(bridge, broker, session, fake):
    assert await bridge.handle_command(SERIAL, "output_1_gain", "-3.5")
    await settle(session)
    assert {"index": 0, "gain": -3.5} in fake.outputs_written()
    assert session.desired(0).outputs[0].gain_db == -3.5
    await until(lambda: broker.state()["outputs"][0]["gain"] == -3.5)
    await session.commit(0)
    assert (await session.history(0))[0].source == "mqtt"


async def test_gain_big_step_raise_is_rejected_and_logged(bridge, broker, session, fake, bus):
    sub = logs(bus)
    await session.set_value(0, "outputs.0.gain_db", -20.0, source="ui")
    await settle(session)
    written = len(fake.configs)
    before = len(broker.payloads(STATE))
    assert not await bridge.handle_command(SERIAL, "output_1_gain", "-10")  # +10 dB > the 6 dB threshold
    await until(lambda: len(broker.payloads(STATE)) > before)  # re-published so HA snaps back
    assert broker.state()["outputs"][0]["gain"] == -20.0
    assert session.desired(0).outputs[0].gain_db == -20.0
    assert len(fake.configs) == written
    assert any("output_1_gain" in m and "rejected" in m and "confirmation" in m for m in messages(sub))
    # Lowering, or a raise within the threshold, goes through.
    assert await bridge.handle_command(SERIAL, "output_1_gain", "-15")
    assert await bridge.handle_command(SERIAL, "output_1_gain", "-30")
    assert session.desired(0).outputs[0].gain_db == -30.0


async def test_gain_range_follows_the_channel_ceiling(bridge, broker, session, fake):
    assert discovery(broker, "number", "output_2_gain")["max"] == M2X4HD.gain_max_db
    await session.set_channel_meta("output", 1, name="Shakers", max_gain_db=4.0)
    bridge.refresh()
    await until(lambda: discovery(broker, "number", "output_2_gain")["max"] == 4.0)
    gain = discovery(broker, "number", "output_2_gain")
    assert gain["name"] == "Shakers gain" and gain["min"] == M2X4HD.gain_min_db
    assert discovery(broker, "switch", "output_2_mute")["name"] == "Shakers mute"
    assert discovery(broker, "sensor", "output_2_level")["name"] == "Shakers level"
    assert not await bridge.handle_command(SERIAL, "output_2_gain", "4.5")  # above the ceiling: rejected, not clamped
    assert session.desired(0).outputs[1].gain_db == 0.0
    assert await bridge.handle_command(SERIAL, "output_2_gain", "4")
    assert session.desired(0).outputs[1].gain_db == 4.0


async def test_unmuting_a_never_synced_output_is_rejected(bridge, session, fake, bus):
    sub = logs(bus)
    await session.set_value(0, "outputs.2.mute", True, source="ui")
    await settle(session)  # mute is synced now, but the gain was never written
    assert not await bridge.handle_command(SERIAL, "output_3_mute", "OFF")
    assert session.desired(0).outputs[2].mute is True
    assert any("never been synced" in m for m in messages(sub))
    assert await bridge.handle_command(SERIAL, "output_3_gain", "-10")
    await settle(session)
    assert await bridge.handle_command(SERIAL, "output_3_mute", "OFF")
    await settle(session)
    assert session.desired(0).outputs[2].mute is False
    assert {"index": 2, "mute": False} in fake.outputs_written()


async def test_muting_is_always_allowed(bridge, session, fake):
    assert await bridge.handle_command(SERIAL, "output_4_mute", "ON")
    await settle(session)
    assert {"index": 3, "mute": True} in fake.outputs_written()


async def test_delay_command_rounds_to_whole_samples(bridge, broker, session, fake):
    assert await bridge.handle_command(SERIAL, "output_1_delay", "1.5")
    await settle(session)
    assert {"index": 0, "delay": {"secs": 0, "nanos": 1_500_000}} in fake.outputs_written()
    assert await bridge.handle_command(SERIAL, "output_1_delay", "1.004")  # 96.384 samples -> 96 = 1.0 ms
    assert session.desired(0).outputs[0].delay_ms == 1.0
    await until(lambda: broker.state()["outputs"][0]["delay"] == 1.0)
    assert not await bridge.handle_command(SERIAL, "output_1_delay", "80")
    assert not await bridge.handle_command(SERIAL, "output_1_delay", "-1")
    assert session.desired(0).outputs[0].delay_ms == 1.0


@pytest.mark.parametrize(
    ("entity", "payload"),
    [("output_5_gain", "0"), ("output_1_gain", "abc"), ("output_1_mute", "toggle"), ("output_1_delay", "nan")],
)
async def test_invalid_output_commands_never_write(bridge, session, fake, entity, payload):
    assert not await bridge.handle_command(SERIAL, entity, payload)
    await asyncio.sleep(0.03)
    assert fake.configs == []


async def test_output_commands_during_a_preset_switch_are_rejected(bridge, session, fake):
    session.preset_switching = 1
    try:
        assert not await bridge.handle_command(SERIAL, "output_1_gain", "-3")
        assert not await bridge.handle_command(SERIAL, p.PUSH_ALL, "PRESS")
    finally:
        session.preset_switching = None
    assert session.desired(0).outputs[0].gain_db == 0.0


async def test_output_commands_target_the_active_preset(bridge, session, fake):
    fake.master["preset"] = 2
    session.monitor.update_master(MasterStatus(preset=2))
    assert await bridge.handle_command(SERIAL, "output_1_gain", "-7")
    assert session.desired(2).outputs[0].gain_db == -7.0
    assert session.desired(0).outputs[0].gain_db == 0.0


async def test_output_commands_via_the_broker(bridge, broker, session):
    broker.send(cmd("output_2_gain"), "-5")
    await until(lambda: session.desired(0).outputs[1].gain_db == -5.0)
    await until(lambda: broker.state()["outputs"][1]["gain"] == -5.0)


# --- profiles -----------------------------------------------------------------------------------------------------


def games_doc():
    doc = default_config_doc(M2X4HD)
    doc = set_path(doc, "outputs.1.gain_db", -2.0)
    return set_path(doc, "outputs.1.delay_ms", 9.0)


async def test_profile_select_appears_with_profiles(bridge, broker, repo):
    assert PROFILE_TOPIC not in broker.retained
    await repo.create_profile("Movies", default_config_doc(M2X4HD).model_dump(mode="json"), product_name="2x4HD")
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    await repo.create_profile("Other model", default_config_doc(M2X4HD).model_dump(mode="json"), product_name="4x10")
    await repo.create_profile("Broken", {"outputs": "nope"})
    bridge.refresh()
    await until(lambda: PROFILE_TOPIC in broker.retained)
    assert json.loads(broker.retained[PROFILE_TOPIC])["options"] == ["Games", "Movies"]
    # The default state equals "Movies".
    await until(lambda: broker.state()["profile"] == "Movies")


async def test_selecting_a_profile_loads_it_into_the_active_preset(bridge, broker, repo, session, fake, bus):
    await synced(session)
    sub = logs(bus)
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    bridge.refresh()
    await until(lambda: PROFILE_TOPIC in broker.retained)
    assert await bridge.handle_command(SERIAL, p.PROFILE, "Games")
    assert session.desired(0).outputs[1].gain_db == -2.0
    assert session.desired(0).outputs[1].delay_ms == 9.0
    written = {k: v for o in fake.outputs_written() if o["index"] == 1 for k, v in o.items()}
    assert written["gain"] == -2.0 and written["delay"] == {"secs": 0, "nanos": 9_000_000}
    await until(lambda: broker.state()["profile"] == "Games")
    assert (await session.history(0))[0].source == "profile"
    assert any("Games" in m and "loaded" in m for m in messages(sub))
    # Loaded through the profiles service, so the Profiles page shows which preset holds it.
    games = next(pr for pr in await repo.list_profiles() if pr.name == "Games")
    assert (await profiles_svc.loaded_slots(repo))[(SERIAL, 0)][0] == games.id
    # An edit that diverges from the profile clears the select.
    await session.set_value(0, "outputs.1.gain_db", -3.0, source="ui")
    await until(lambda: broker.state()["profile"] is None)


async def test_unknown_profile_is_rejected(bridge, broker, repo, session):
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    before = len(broker.payloads(STATE))
    assert not await bridge.handle_command(SERIAL, p.PROFILE, "Music")
    await until(lambda: len(broker.payloads(STATE)) > before)
    assert session.desired(0).outputs[1].gain_db == 0.0


async def test_profile_created_after_the_cache_is_found_on_select(bridge, broker, repo, session):
    await synced(session)
    await bridge.publish_now(SERIAL)  # caches "no profiles"
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    assert await bridge.handle_command(SERIAL, p.PROFILE, "games")  # a command always reads the profiles afresh
    assert session.desired(0).outputs[1].gain_db == -2.0


async def test_profile_load_respects_channel_ceilings(bridge, repo, session, bus):
    await synced(session)
    sub = logs(bus)
    await session.set_channel_meta("output", 1, max_gain_db=1.0)
    loud = set_path(games_doc(), "outputs.1.gain_db", 3.0)
    await repo.create_profile("Loud", loud.model_dump(mode="json"))
    assert not await bridge.handle_command(SERIAL, p.PROFILE, "Loud")  # +3 dB is above the +1 dB ceiling
    assert session.desired(0).outputs[1].gain_db == 0.0
    assert any("ceiling" in m for m in messages(sub))


async def test_profile_load_with_a_big_raise_is_rejected(bridge, repo, session, fake, bus):
    await session.set_value(0, "outputs.0.gain_db", -40.0, source="ui")
    await synced(session)
    sub = logs(bus)
    written = len(fake.configs)
    loud = set_path(default_config_doc(M2X4HD), "outputs.0.gain_db", 6.0)
    await repo.create_profile("Loud", loud.model_dump(mode="json"))
    assert not await bridge.handle_command(SERIAL, p.PROFILE, "Loud")  # +46 dB: HA can't confirm it
    assert session.desired(0).outputs[0].gain_db == -40.0
    assert len(fake.configs) == written
    assert any("Loud" in m and "rejected" in m and "confirmation" in m for m in messages(sub))
    assert (await profiles_svc.loaded_slots(repo)) == {}


async def test_profile_load_that_unmutes_a_never_synced_output_is_rejected(bridge, repo, session, fake, bus):
    sub = logs(bus)
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    assert not await bridge.handle_command(SERIAL, p.PROFILE, "Games")  # nothing on the device is confirmed yet
    assert session.desired(0).outputs[1].gain_db == 0.0
    assert fake.configs == []
    assert any("never synced" in m for m in messages(sub))
    # A profile that keeps the unknown outputs muted loads.
    quiet = games_doc()
    for n in range(M2X4HD.n_outputs):
        quiet = set_path(quiet, f"outputs.{n}.mute", True)
    await repo.create_profile("Quiet", quiet.model_dump(mode="json"))
    assert await bridge.handle_command(SERIAL, p.PROFILE, "Quiet")
    assert all(out.mute for out in session.desired(0).outputs)


async def test_profile_select_is_removed_with_the_last_profile(bridge, broker, repo):
    profile = await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    bridge.refresh()
    await until(lambda: PROFILE_TOPIC in broker.retained)
    await repo.delete_profile(profile.id)
    bridge.refresh()
    await until(lambda: PROFILE_TOPIC not in broker.retained)
    assert broker.payloads(PROFILE_TOPIC)[-1] == ""


async def test_custom_profile_loader(manager, bus, mqtt_settings, broker, repo, session):
    loaded = []

    async def loader(s, entry):
        loaded.append((s.serial, entry.name))

    b = make_bridge(manager, bus, mqtt_settings, broker, profile_loader=loader)
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    assert await b.handle_command(SERIAL, p.PROFILE, "Games")
    assert loaded == [(SERIAL, "Games")]


async def test_profile_cache_ttl(repo, session):
    now = [0.0]
    catalog = ProfileCatalog(ttl_s=10, clock=lambda: now[0])
    assert await catalog.entries(session) == []
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    assert await catalog.entries(session) == []
    now[0] = 11.0
    assert [e.name for e in await catalog.entries(session)] == ["Games"]
    await repo.create_profile("Movies", default_config_doc(M2X4HD).model_dump(mode="json"))
    catalog.invalidate()
    assert [e.name for e in await catalog.entries(session)] == ["Games", "Movies"]


async def test_profile_match_requires_a_unique_profile(repo, session):
    catalog = ProfileCatalog()
    doc = default_config_doc(M2X4HD).model_dump(mode="json")
    await repo.create_profile("A", doc)
    await repo.create_profile("B", doc)
    entries = await catalog.entries(session)
    assert catalog.matching(session, entries, 0) is None
    assert catalog.matching(session, entries, None) is None


# --- push all -----------------------------------------------------------------------------------------------------


async def test_push_all_button(bridge, session, fake, bus):
    sub = logs(bus)
    assert await bridge.handle_command(SERIAL, p.PUSH_ALL, "PRESS")
    assert len(fake.outputs_written()) >= M2X4HD.n_outputs
    assert any("push-all preset 1" in m for m in messages(sub))
    written = len(fake.configs)
    assert not await bridge.handle_command(SERIAL, p.PUSH_ALL, "ON")
    assert len(fake.configs) == written


# --- levels -------------------------------------------------------------------------------------------------------


async def test_levels_are_off_unless_enabled(bridge, bus, broker):
    assert bus.subscriber_count(LEVELS) == 0
    bus.publish(LEVELS, {"i": [-20.0, -20.0], "o": [-1.0] * 4}, serial=SERIAL)
    await asyncio.sleep(0.05)
    assert broker.payloads(LEVELS_TOPIC) == []


async def test_levels_throttled_and_not_retained(manager, bus, mqtt_settings, broker):
    b = make_bridge(manager, bus, mqtt_settings, broker, levels=True, levels_interval_s=0.1)
    b.start()
    try:
        await asyncio.wait_for(b.connected.wait(), 2)
        assert bus.subscriber_count(LEVELS) == 1  # keeps the device's level stream on
        for k in range(20):  # ~20 Hz for ~0.4 s
            bus.publish(LEVELS, {"i": [-20.0 - k, -30.0], "o": [-1.04, -2.0, -3.0, -4.0]}, serial=SERIAL)
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)
        sent = [(payload, retain) for t, payload, _, retain in broker.published if t == LEVELS_TOPIC]
        assert 2 <= len(sent) <= 6
        assert not any(retain for _, retain in sent)
        assert json.loads(sent[-1][0]) == {"i": [-39.0, -30.0], "o": [-1.0, -2.0, -3.0, -4.0]}
        assert LEVELS_TOPIC not in broker.retained
        count = len(sent)
        await asyncio.sleep(0.25)  # no new levels -> nothing re-sent
        assert len(broker.payloads(LEVELS_TOPIC)) == count
    finally:
        await b.stop()
    assert bus.subscriber_count(LEVELS) == 0


async def test_levels_setting(manager, bus, mqtt_settings, broker):
    assert make_bridge(manager, bus, mqtt_settings, broker).levels_enabled is False
    settings = mqtt_settings.model_copy()
    object.__setattr__(settings, "mqtt_levels", True)  # the MQTT_LEVELS setting, once Settings has it
    assert make_bridge(manager, bus, settings, broker).levels_enabled is True


# --- remove from HA -----------------------------------------------------------------------------------------------


async def test_remove_from_home_assistant_clears_every_entity(bridge, broker, repo, session, bus):
    await repo.create_profile("Games", games_doc().model_dump(mode="json"))
    bridge.refresh()
    await until(lambda: PROFILE_TOPIC in broker.retained)
    sub = logs(bus)
    result = await remove_from_home_assistant(bridge)
    assert result.ok and result.removed == [SERIAL]
    assert not [t for t in broker.retained if t.startswith("homeassistant/")]
    assert STATE not in broker.retained
    assert any("removed from Home Assistant" in m for m in messages(sub))
    cleared = {t for t, payload, _, retain in broker.published if payload == "" and retain}
    assert DISCOVERY.format("number", "output_4_delay") in cleared
    assert DISCOVERY.format("sensor", "input_1_level") in cleared


async def test_remove_from_home_assistant_without_mqtt(manager, bus, mqtt_settings, broker):
    result = await remove_from_home_assistant(None)
    assert not result.ok and "not enabled" in result.message
    b = make_bridge(manager, bus, mqtt_settings, broker)
    result = await remove_from_home_assistant(b, SERIAL)
    assert not result.ok and "not connected" in result.message


async def test_refresh_loop_rechecks_discovery(manager, bus, mqtt_settings, broker, session):
    b = make_bridge(manager, bus, mqtt_settings, broker, refresh_interval_s=0.05)
    b.start()
    try:
        await asyncio.wait_for(b.connected.wait(), 2)
        await session.set_channel_meta("output", 0, name="Sub")
        b.profiles.invalidate()
        await until(lambda: discovery(broker, "number", "output_1_gain")["name"] == "Sub gain")
    finally:
        await b.stop()
