"""MQTT bridge to Home Assistant (docs/10): discovery, retained state, availability and command topics.

It is just another client of the session layer: commands go through `DeviceSession` exactly like UI changes, so the
same validation, safety guards and preset-switch blocking apply. State comes from the in-process bus that feeds SSE.
The broker being down never affects the rest of the app; the bridge reconnects with backoff.
"""

import asyncio
import logging
import random
import secrets
import time
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiomqtt

from app.bus import DESIRED, DEVICE, LEVELS, MASTER, SYNC, Bus, Subscription, SubscriptionClosed
from app.config import Settings
from app.daemon.client import DaemonError
from app.model.device_profile import M2X4HD
from app.mqtt import payloads as p
from app.mqtt.profiles import ProfileCatalog, ProfileEntry, load_into_active
from app.sync.session import (
    ConfirmationRequired,
    DeviceSession,
    MasterValidationError,
    PresetSwitchInProgress,
    PresetSwitchTimeout,
    SessionManager,
    SessionState,
    StateUnavailable,
)

log = logging.getLogger(__name__)

STATE_DEBOUNCE_S = 0.25
STATE_MAX_DELAY_S = 1.0
OFFLINE_TIMEOUT_S = 2.0
STABLE_CONNECTION_S = 30.0  # a connection that lasted this long resets the reconnect backoff
LEVELS_INTERVAL_S = 1.0  # levels go to MQTT at most once a second (4 Hz in the UI)
REFRESH_INTERVAL_S = 60.0  # re-check discovery (channel names, ceilings, profiles) even without bus events

type ProfileLoader = Callable[[DeviceSession, ProfileEntry], Any]


class MqttClient(Protocol):
    """The part of `aiomqtt.Client` the bridge uses (tests substitute a fake)."""

    @property
    def messages(self) -> AsyncIterator[aiomqtt.Message]: ...

    async def publish(self, topic: str, payload: Any = None, qos: int = 0, retain: bool = False) -> None: ...

    async def subscribe(self, topic: str, qos: int = 0) -> Any: ...


type ClientFactory = Callable[[aiomqtt.Will], AbstractAsyncContextManager[MqttClient]]


class MqttBridge:
    def __init__(
        self,
        manager: SessionManager,
        bus: Bus,
        settings: Settings,
        *,
        client_factory: ClientFactory | None = None,
        debounce_s: float = STATE_DEBOUNCE_S,
        max_delay_s: float = STATE_MAX_DELAY_S,
        backoff_min_s: float = 1.0,
        backoff_max_s: float = 60.0,
        levels: bool | None = None,
        levels_interval_s: float = LEVELS_INTERVAL_S,
        refresh_interval_s: float = REFRESH_INTERVAL_S,
        profiles: ProfileCatalog | None = None,
        profile_loader: ProfileLoader | None = None,
    ) -> None:
        self.manager = manager
        self.bus = bus
        self.settings = settings
        self.topics = p.Topics(settings.mqtt_base_topic, settings.mqtt_discovery_prefix)
        self.volume_floor_db = settings.mqtt_volume_floor_db
        self._client_factory = client_factory or self._default_client
        self._debounce_s = debounce_s
        self._max_delay_s = max_delay_s
        self._backoff_min_s = backoff_min_s
        self._backoff_max_s = backoff_max_s
        # Publishing levels keeps the device's level stream (USB polling) on, so it is opt-in: ``MQTT_LEVELS``.
        self.levels_enabled = settings.mqtt_levels if levels is None else levels
        self._levels_interval_s = levels_interval_s
        self._refresh_interval_s = refresh_interval_s
        self.profiles = profiles or ProfileCatalog()
        self._profile_loader = profile_loader or load_into_active

        self.connected = asyncio.Event()
        self.last_error: str | None = None
        self._client: MqttClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._sub: Subscription | None = None
        self._announced: dict[int, str] = {}  # serial -> discovery fingerprint published on this connection
        self._published: dict[int, str] = {}  # serial -> last state JSON published on this connection
        self._removed: set[int] = set()  # "Remove from HA": not announced again until the next (re)connect
        self._sync_counts: dict[tuple[int, int], dict[str, int]] = {}  # from bus `sync` events
        self._debounce: dict[int, asyncio.Task] = {}
        self._dirty_since: dict[int, float] = {}
        self._dirty_at: dict[int, float] = {}
        self._force: set[int] = set()
        self._pending_cmd: dict[tuple[int, str], str] = {}
        self._cmd_workers: dict[tuple[int, str], asyncio.Task] = {}
        self._levels_sub: Subscription | None = None
        self._levels: dict[int, dict[str, Any]] = {}  # serial -> newest levels not yet published

    # --- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._tasks:
            return
        self._sub = self.bus.subscribe([MASTER, DEVICE, SYNC, DESIRED])
        self._tasks = [
            asyncio.create_task(self._consume_bus(self._sub), name="mqtt-bus"),
            asyncio.create_task(self._run(), name="mqtt-connection"),
            asyncio.create_task(self._refresh_loop(), name="mqtt-refresh"),
        ]
        if self.levels_enabled:
            self._levels_sub = self.bus.subscribe([LEVELS])
            self._tasks += [
                asyncio.create_task(self._consume_levels(self._levels_sub), name="mqtt-levels"),
                asyncio.create_task(self._levels_loop(), name="mqtt-levels-publish"),
            ]

    async def stop(self) -> None:
        """Publish ``offline`` (a clean disconnect doesn't trigger the LWT) and disconnect."""
        if not self._tasks:
            return
        for sub in (self._sub, self._levels_sub):
            if sub is not None:
                sub.close()
        background = [*self._debounce.values(), *self._cmd_workers.values()]
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        client = self._client
        if client is not None:
            try:
                async with asyncio.timeout(OFFLINE_TIMEOUT_S):
                    for serial in list(self._announced):
                        await client.publish(self.topics.availability(serial), p.OFFLINE, qos=1, retain=True)
                    await client.publish(self.topics.bridge_availability, p.OFFLINE, qos=1, retain=True)
            except (TimeoutError, aiomqtt.MqttError) as exc:
                log.warning("mqtt: could not publish offline: %s", exc)
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._client = None
        self.connected.clear()

    def _default_client(self, will: aiomqtt.Will) -> AbstractAsyncContextManager[MqttClient]:
        s = self.settings
        return aiomqtt.Client(
            s.mqtt_host,
            s.mqtt_port,
            username=s.mqtt_username or None,
            password=s.mqtt_password or None,
            identifier=f"minidsp-webui-{secrets.token_hex(4)}",
            will=will,
            timeout=10,
        )

    async def _run(self) -> None:
        backoff = self._backoff_min_s
        will = aiomqtt.Will(self.topics.bridge_availability, p.OFFLINE, qos=1, retain=True)
        while True:
            connected_at: float | None = None
            try:
                async with self._client_factory(will) as client:
                    await self._on_connect(client)
                    connected_at = time.monotonic()
                    async for message in client.messages:
                        self._on_message(message)
                    raise aiomqtt.MqttError("message stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._client = None
                self.connected.clear()
                self.last_error = str(exc) or type(exc).__name__
                if connected_at is not None and time.monotonic() - connected_at >= STABLE_CONNECTION_S:
                    backoff = self._backoff_min_s
                delay = backoff * random.uniform(0.8, 1.2)
                backoff = min(backoff * 2, self._backoff_max_s)
                log.warning("mqtt: %s; reconnecting in %.1f s", self.last_error, delay)
                await asyncio.sleep(delay)

    async def _on_connect(self, client: MqttClient) -> None:
        self._announced.clear()
        self._published.clear()
        self._removed.clear()
        await client.publish(self.topics.bridge_availability, p.ONLINE, qos=1, retain=True)
        await client.subscribe(self.topics.command_filter, qos=1)
        self._client = client
        self.last_error = None
        for session in self.manager:
            await self._publish_device(session, force=True)
        self.connected.set()
        log.info("mqtt: connected to %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)

    # --- publishing -----------------------------------------------------------------------------------------------

    def device_info(self, session: DeviceSession, profiles: Iterable[str] = ()) -> p.DeviceInfo:
        version = session.device.version if session.device else None
        device = session.profile

        def name(kind: str, index: int) -> str:
            return self.channel_name(session, kind, index)

        return p.DeviceInfo(
            serial=session.serial,
            profile=device,
            sources=tuple(session.available_sources),
            fw=version.fw if version else None,
            configuration_url=self.settings.public_url or None,
            outputs=tuple(
                p.OutputInfo(name("output", n), session.channel_ceiling("output", n)) for n in range(device.n_outputs)
            ),
            inputs=tuple(name("input", n) for n in range(device.n_inputs)),
            profiles=tuple(profiles),
        )

    @staticmethod
    def channel_name(session: DeviceSession, kind: str, index: int) -> str:
        """The name from Settings ("Shakers"), else "In 1" / "Out 1" like the UI."""
        meta = session.channel_meta.get((kind, index))
        return (meta.name if meta is not None and meta.name else None) or p.default_label(kind, index)

    @staticmethod
    def output_states(session: DeviceSession, preset: int | None) -> list[dict[str, Any]]:
        """Gain/mute/delay of each output in the preset's desired state (None while it isn't loaded)."""
        n_outputs = session.profile.n_outputs
        if preset is not None and session.loaded:
            try:
                outputs = session.desired(preset).outputs
            except (StateUnavailable, MasterValidationError):
                outputs = []
            if len(outputs) == n_outputs:
                return [p.output_state(o.gain_db, o.mute, o.delay_ms) for o in outputs]
        return [p.output_state(None, None, None) for _ in range(n_outputs)]

    def state_doc(self, session: DeviceSession, *, profile: str | None = None) -> dict[str, Any]:
        master = session.master
        preset = master.preset if master else None
        return p.build_state(
            connected=session.state == SessionState.CONNECTED,
            state=str(session.state),
            volume=master.volume if master else None,
            mute=master.mute if master else None,
            source=master.source if master else None,
            preset=preset,
            preset_switching=session.preset_switching,
            sync_counts=self._sync_counts_for(session, preset),
            outputs=self.output_states(session, preset),
            profile=profile,
        )

    def _sync_counts_for(self, session: DeviceSession, preset: int | None) -> dict[str, int] | None:
        if preset is None:
            return None
        try:
            return p.sync_counts(session.sync_status(preset).values())
        except NotImplementedError:
            pass
        except Exception as exc:  # e.g. the preset isn't attached to the writer yet
            log.debug("mqtt: sync status for preset %s unavailable: %s", preset, exc)
        return self._sync_counts.get((session.serial, preset))

    async def _publish(self, topic: str, payload: str, *, retain: bool = True) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            await client.publish(topic, payload, qos=1, retain=retain)
        except aiomqtt.MqttError as exc:
            log.debug("mqtt: publish to %s failed: %s", topic, exc)
            return False
        return True

    async def _publish_device(self, session: DeviceSession, *, force: bool = False) -> None:
        """Discovery (when it changed or is new on this connection), availability and state."""
        serial = session.serial
        if serial in self._removed:
            return
        entries = await self._profile_entries(session)
        info = self.device_info(session, [e.name for e in entries])
        configs = p.discovery_configs(info, self.topics, volume_floor_db=self.volume_floor_db)
        fingerprint = p.dumps(configs)
        if self._announced.get(serial) != fingerprint:
            for topic, config in configs.items():
                if not await self._publish(topic, p.dumps(config)):
                    return
            # Optional entities that went away (the Profile select once the last profile is deleted).
            for topic in p.removal_topics(serial, self.topics, session.profile):
                if topic not in configs and not await self._publish(topic, ""):
                    return
            if not await self._publish(self.topics.availability(serial), p.ONLINE):
                return
            self._announced[serial] = fingerprint
        master = session.master
        profile = self.profiles.matching(session, entries, master.preset if master else None)
        state = p.dumps(self.state_doc(session, profile=profile))
        changed = force or self._published.get(serial) != state
        if changed and await self._publish(self.topics.state(serial), state):
            self._published[serial] = state

    async def _profile_entries(self, session: DeviceSession, *, fresh: bool = False) -> list[ProfileEntry]:
        try:
            return await self.profiles.entries(session, fresh=fresh)
        except Exception as exc:  # a database problem must not stop the master controls from being published
            log.warning("mqtt: reading profiles failed: %s", exc)
            return []

    def refresh(self, serial: int | None = None) -> None:
        """Re-read profiles and re-check discovery and state now (after a channel name/ceiling or profile change).
        Only what actually changed is re-published."""
        self.profiles.invalidate()
        for s in [serial] if serial is not None else [session.serial for session in self.manager]:
            self.schedule_state(s)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self._refresh_interval_s)
            if self._client is not None:
                for session in self.manager:
                    self.schedule_state(session.serial)

    async def publish_now(self, serial: int, *, force: bool = False) -> None:
        session = self.manager.get(serial)
        if session is not None:
            await self._publish_device(session, force=force)

    def schedule_state(self, serial: int, *, force: bool = False) -> None:
        """Publish the device's state after a 250 ms quiet period (at most ``max_delay_s`` after the first change)."""
        now = time.monotonic()
        self._dirty_since.setdefault(serial, now)
        self._dirty_at[serial] = now
        if force:
            self._force.add(serial)
        task = self._debounce.get(serial)
        if task is None or task.done():
            self._debounce[serial] = asyncio.create_task(self._debounced(serial), name=f"mqtt-state-{serial}")

    async def _debounced(self, serial: int) -> None:
        # Changes that arrive while a publish is in flight mark the serial dirty again: go round once more.
        while serial in self._dirty_at:
            while True:
                now = time.monotonic()
                deadline = min(self._dirty_at[serial] + self._debounce_s, self._dirty_since[serial] + self._max_delay_s)
                if now >= deadline:
                    break
                await asyncio.sleep(deadline - now)
            del self._dirty_since[serial], self._dirty_at[serial]
            force = serial in self._force
            self._force.discard(serial)
            try:
                await self.publish_now(serial, force=force)
            except Exception:
                log.exception("mqtt: publishing state for %s failed", serial)

    async def _consume_bus(self, sub: Subscription) -> None:
        while True:
            try:
                event = await sub.get()
            except SubscriptionClosed:
                return
            if event.topic == SYNC and event.serial is not None:
                data = event.data
                if isinstance(data.get("preset"), int) and isinstance(data.get("counts"), dict):
                    self._sync_counts[(event.serial, data["preset"])] = dict(data["counts"])
            serials = [event.serial] if event.serial is not None else [s.serial for s in self.manager]
            for serial in serials:
                self.schedule_state(serial)

    # --- levels ---------------------------------------------------------------------------------------------------

    async def _consume_levels(self, sub: Subscription) -> None:
        while True:
            try:
                event = await sub.get()
            except SubscriptionClosed:
                return
            if event.serial is not None:
                self._levels[event.serial] = event.data

    async def _levels_loop(self) -> None:
        """Publish the newest levels of each device at most once per interval (not retained)."""
        while True:
            await asyncio.sleep(self._levels_interval_s)
            pending, self._levels = self._levels, {}
            for serial, data in pending.items():
                if serial in self._announced and serial not in self._removed:
                    await self._publish(self.topics.levels(serial), p.dumps(p.build_levels(data)), retain=False)

    # --- remove from HA -------------------------------------------------------------------------------------------

    async def remove_device(self, serial: int) -> bool:
        """ "Remove from HA": clear the retained discovery configs (HA deletes the entities), state and availability.

        Returns False when not connected. The entities come back on the next reconnect or app restart."""
        if self._client is None:
            return False
        self._removed.add(serial)
        session = self.manager.get(serial)
        ok = True
        for topic in [
            *p.removal_topics(serial, self.topics, session.profile if session is not None else M2X4HD),
            self.topics.state(serial),
            self.topics.availability(serial),
        ]:
            ok = await self._publish(topic, "") and ok
        self._announced.pop(serial, None)
        self._published.pop(serial, None)
        return ok

    # --- commands -------------------------------------------------------------------------------------------------

    def _on_message(self, message: aiomqtt.Message) -> None:
        parsed = self.topics.parse_command(message.topic.value)
        if parsed is None:
            return
        if message.retain:
            # A retained command would be replayed on every reconnect; never act on one.
            log.warning("mqtt: ignoring retained command on %s", message.topic.value)
            return
        serial, entity = parsed
        raw = message.payload
        payload = raw.decode("utf-8", "replace") if isinstance(raw, bytes | bytearray) else str(raw or "")
        self.submit_command(serial, entity, payload)

    def submit_command(self, serial: int, entity: str, payload: str) -> None:
        """Queue a command. Commands for the same entity coalesce: only the newest waiting payload runs."""
        key = (serial, entity)
        self._pending_cmd[key] = payload
        worker = self._cmd_workers.get(key)
        if worker is None or worker.done():
            self._cmd_workers[key] = asyncio.create_task(self._drain(key), name=f"mqtt-cmd-{serial}-{entity}")

    async def _drain(self, key: tuple[int, str]) -> None:
        while key in self._pending_cmd:
            payload = self._pending_cmd.pop(key)
            await self.handle_command(*key, payload)

    async def handle_command(self, serial: int, entity: str, payload: str) -> bool:
        """Apply one command through the session layer. On any rejection the state is re-published so HA snaps back.
        Returns True when the change was applied."""
        session = self.manager.get(serial)
        if session is None:
            log.info("mqtt: command for unknown device %s ignored", serial)
            return False
        profile = session.profile
        try:
            match entity:
                case p.VOLUME:
                    await session.set_volume(p.parse_volume(payload, profile, self.volume_floor_db))
                case p.MUTE:
                    await session.set_mute(p.parse_switch(payload))
                case p.SOURCE:
                    await session.set_source(p.parse_source(payload, profile))
                case p.PRESET:
                    await self._switch_preset(session, p.parse_preset(payload, profile))
                case p.PROFILE:
                    await self._load_profile(session, payload)
                case p.PUSH_ALL:
                    p.parse_press(payload)
                    self._check_not_switching(session)
                    await session.push_all(await session.active_preset())
                case _ if (output := p.parse_output_entity(entity)) is not None:
                    await self._set_output(session, *output, payload)
                case _:
                    raise p.CommandError(f"unknown command {entity!r}")
        except ConfirmationRequired as exc:
            self._reject(session, entity, payload, f"{exc} (there is no confirmation from Home Assistant)")
            return False
        except (p.CommandError, MasterValidationError, PresetSwitchInProgress) as exc:
            self._reject(session, entity, payload, str(exc))
            return False
        except (DaemonError, PresetSwitchTimeout) as exc:
            self._reject(session, entity, payload, f"device write failed: {exc}", level="error")
            return False
        except StateUnavailable as exc:
            self._reject(session, entity, payload, str(exc), level="error")
            return False
        except Exception as exc:
            log.exception("mqtt: %s command failed", entity)
            self._reject(session, entity, payload, f"unexpected error: {exc}", level="error")
            return False
        self.schedule_state(serial)
        return True

    @staticmethod
    def _check_not_switching(session: DeviceSession) -> None:
        if session.preset_switching is not None:
            raise PresetSwitchInProgress(f"switching to preset {session.preset_switching + 1}; try again shortly")

    async def _set_output(self, session: DeviceSession, index: int, kind: str, payload: str) -> None:
        """Gain / mute / delay of one output in the active preset, through the same edit path as the UI.

        A gain raise above the big-step threshold is rejected (HA can't confirm), and so is un-muting an output whose
        gain or mute was never confirmed on the device (the UI asks first)."""
        device = session.profile
        if not 0 <= index < device.n_outputs:
            raise p.CommandError(f"no output {index + 1}")
        await session.ensure_loaded()
        match kind:
            case p.GAIN:
                ceiling = session.channel_ceiling("output", index)
                path, value = f"outputs.{index}.gain_db", p.parse_gain(payload, device, ceiling)
            case p.MUTE:
                path, value = f"outputs.{index}.mute", p.parse_switch(payload)
            case _:
                path, value = f"outputs.{index}.delay_ms", p.parse_delay(payload, device)
        self._check_not_switching(session)
        preset = await session.active_preset()
        if kind == p.MUTE and value is False:
            status = session.sync_status(preset)
            unknown = [q for q in (f"outputs.{index}.gain_db", path) if status.get(q, "unknown") == "unknown"]
            if unknown:
                raise ConfirmationRequired(
                    f"output {index + 1} has never been synced ({', '.join(unknown)} unknown on the device); "
                    "un-mute it from the web UI"
                )
        name = self.channel_name(session, "output", index)
        await session.set_value(
            preset, path, value, source="mqtt", summary=f"Home Assistant: {name} {kind} {payload.strip()[:16]}"
        )

    async def _load_profile(self, session: DeviceSession, payload: str) -> None:
        entries = await self.profiles.entries(session, fresh=True)
        name = p.parse_profile(payload, [e.name for e in entries])
        self._check_not_switching(session)
        entry = next(e for e in entries if e.name == name)
        await self._profile_loader(session, entry)

    async def _switch_preset(self, session: DeviceSession, preset: int) -> None:
        """Switch & flush staged edits when the session supports it (Phase 2), else a plain switch."""
        try:
            await session.activate_preset(preset)
        except NotImplementedError:
            await session.set_preset(preset)

    def _reject(
        self, session: DeviceSession, entity: str, payload: str, reason: str, *, level: str = "warning"
    ) -> None:
        session.log_event(
            level, f"MQTT {entity} command {payload[:64]!r} rejected: {reason}", source="mqtt", entity=entity
        )
        self.schedule_state(session.serial, force=True)


@dataclass(frozen=True)
class RemovalResult:
    ok: bool
    removed: list[int] = field(default_factory=list)
    message: str = ""


async def remove_from_home_assistant(bridge: MqttBridge | None, serial: int | None = None) -> RemovalResult:
    """The Settings "Remove from HA" action: clear the retained discovery configs (and state/availability) of one
    device, or of every known device. HA deletes the entities; they come back on the next reconnect or restart."""
    if bridge is None:
        return RemovalResult(False, message="MQTT is not enabled")
    if not bridge.connected.is_set():
        return RemovalResult(False, message=f"not connected to the MQTT broker ({bridge.last_error or 'connecting'})")
    serials = [serial] if serial is not None else [session.serial for session in bridge.manager]
    if not serials:
        return RemovalResult(False, message="no device to remove")
    removed = [s for s in serials if await bridge.remove_device(s)]
    if len(removed) != len(serials):
        return RemovalResult(False, removed, "some topics could not be cleared; try again")
    for s in removed:
        session = bridge.manager.get(s)
        if session is not None:
            session.log_event("info", "removed from Home Assistant (MQTT discovery cleared)", kind="mqtt")
    return RemovalResult(True, removed, f"removed {len(removed)} device(s) from Home Assistant")


def create_bridge(manager: SessionManager, bus: Bus, settings: Settings) -> MqttBridge | None:
    """The bridge, or None when MQTT is disabled (``MQTT_ENABLED``) or no ``MQTT_HOST`` is set."""
    if not settings.mqtt_enabled:
        return None
    if not settings.mqtt_host:
        log.error("MQTT_ENABLED is set but MQTT_HOST is empty; MQTT is disabled")
        return None
    return MqttBridge(manager, bus, settings)
