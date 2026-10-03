"""speakerd entry point: wires BlueZ engine + shairport D-Bus link + MQTT link + HA
discovery, and the control socket for the node API service (node.py,
control.py) when [control] is enabled."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from collections import defaultdict
from datetime import datetime, timezone

from . import config as config_mod
from .airplay import AirplayState
from .amp_export import AmpMetadataExport
from .bluez import BluezEngine
from .config import AMP_SLUG, Config
from .discovery import build_announce, build_discovery
from .mqtt_link import EV_CONNECTED, EV_MESSAGE, MqttLink, NullMqttLink
from .roster import Roster, load_state, update_state
from .shairport import ShairportLink
from .system import power

log = logging.getLogger("speakerd")

# how long the amp must stay connected without its A2DP link, counted from its
# latest connect, before speakerd restores it: a normal connect brings the
# audio up within a second or two (about 60 ms on .156)
AMP_AUDIO_GRACE_S = 10
# how long a restore attempt waits for the transport after BlueZ said yes
AMP_AUDIO_WAIT_S = 5
# after the restore gives up: ask for the audio link alone this often, for as
# long as the amp stays connected without it. ConnectProfile never disconnects
# the amp, and nothing can be playing in that state, so it disturbs nothing
AMP_AUDIO_SLOW_RETRY_S = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_auto_reconnect(path: str, default: bool = True) -> bool:
    """Gate state survives daemon restarts (and broker wipes) in a local file."""
    try:
        with open(path, "rb") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        return bool(data.get("amp_auto_reconnect", default))
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as e:
        log.warning("state file %s unreadable (%s) — defaulting auto-reconnect %s",
                    path, e, "on" if default else "off")
        return default


def _save_auto_reconnect(path: str, enabled: bool) -> None:
    # merged into the state file, which also holds the amp and the node name
    update_state(path, amp_auto_reconnect=enabled)


class App:
    """Owns retained-state cache and MQTT publishing; BluezEngine's sink."""

    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop):
        self.cfg = cfg
        self.queue: asyncio.Queue = asyncio.Queue()
        self.roster = Roster(cfg)
        self.engine = BluezEngine(cfg, sink=self, roster=self.roster)
        self.airplay = AirplayState()
        # AirPlay state and commands travel over shairport's own D-Bus
        # interface, never MQTT: the amp screen must not depend on a broker
        self.shairport = (ShairportLink(self._airplay_changed)
                          if cfg.airplay_enabled else None)
        self._bt_now_playing: dict = {"status": "idle"}
        self.amp_export = (
            AmpMetadataExport(cfg, on_command=self._amp_export_command,
                              on_state=self._amp_export_state)
            if cfg.amp_metadata_export else None)
        self._retained: dict[str, str] = {}  # topic -> last payload (all retained)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._bt_streaming = False
        self._discovery = build_discovery(cfg)
        self._announce = build_announce(cfg)
        # off while Home Assistant has claimed the node over the API: HA owns
        # the device card then, and a broker user must not get a second one.
        # Remembered across restarts, so a restart never flashes a card in HA
        # before the API service reconnects and confirms it
        self._mqtt_discovery = bool(load_state(cfg.state_file).get("mqtt_discovery", True))
        self._last_results: dict[str, dict] = {}  # slug -> last connect result
        self._tasks: set[asyncio.Task] = set()

        # amp auto-reconnect policy (HA-gated; see _maybe_auto_reconnect_amp)
        self._amp_connected = False
        self._amp_user_disconnected = False
        self._auto_reconnect = _load_auto_reconnect(cfg.state_file)
        self._amp_reconnect_task: asyncio.Task | None = None
        # the amp's A2DP link, apart from Connected (see amp_audio_changed);
        # the engine holds the value itself, App only reacts to its edges
        self._amp_audio_task: asyncio.Task | None = None
        self._amp_connects = 0  # counts Connected=True edges: restarts the grace
        self._amp_edge = asyncio.Event()  # replaced on every amp edge, see _amp_edge_fire

        subscriptions = [
            (cfg.topic("device", "+", "set"), 1),
            # QoS 0, like the power buttons below: a transport press queued
            # while the daemon is down would replay as a stale skip/pause
            (cfg.topic("bt", "transport", "set"), 0),
            (cfg.topic("amp", "fix_metadata"), 1),
            (cfg.topic("amp", "auto_reconnect", "set"), 1),
            (f"{cfg.discovery_prefix}/status", 1),
        ]
        if cfg.power_commands:
            # QoS 0 on purpose: with our persistent session, a QoS-1 press
            # queued by the broker while the daemon is offline would replay a
            # reboot/shutdown right after the next connect (delivered with
            # retain=False, so the retained-command guard can't see it). A
            # press while the daemon is down doing nothing is the safe
            # failure mode for power buttons.
            subscriptions += [(cfg.topic("system", "reboot"), 0),
                              (cfg.topic("system", "shutdown"), 0)]
        if cfg.airplay_enabled:
            # HA's AirPlay buttons; speakerd relays them to shairport over D-Bus.
            # QoS 0 for the same reason as bt/transport/set
            subscriptions.append((cfg.topic("airplay", "remote"), 0))
        link = MqttLink if cfg.mqtt_enabled else NullMqttLink
        self.mqtt = link(cfg, loop, self.queue, subscriptions)

        self.node = None
        if cfg.control_enabled:
            from .node import AudioNode
            self.node = AudioNode(self)

    def _spawn(self, coro) -> asyncio.Task:
        # keep a strong reference: the loop only holds weak refs to tasks
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ---------------------------------------------------------- publishing

    def _publish_retained(self, topic: str, payload: str) -> None:
        self._retained[topic] = payload
        self.mqtt.publish(topic, payload, retain=True)

    def republish_all(self) -> None:
        """On every MQTT (re)connect and on HA birth."""
        self.mqtt.publish(self.cfg.availability_topic, "online", retain=True)
        if self._mqtt_discovery:
            # the node announcement goes out before the entities it describes
            self.mqtt.publish(self._announce[0], self._announce[1], retain=True)
            for topic, payload in self._discovery:
                self.mqtt.publish(topic, payload, retain=True)
        for topic, payload in self._retained.items():
            self.mqtt.publish(topic, payload, retain=True)

    def set_mqtt_discovery(self, enabled: bool) -> None:
        """Off on a claim, on again on release. Off clears what was published
        with empty retained payloads, so no ghost entities remain."""
        if enabled == self._mqtt_discovery:
            return
        self._mqtt_discovery = enabled
        update_state(self.cfg.state_file, mqtt_discovery=enabled)
        if enabled:
            log.info("MQTT discovery on (not claimed by Home Assistant)")
            self.republish_all()
            return
        log.info("MQTT discovery off (claimed by Home Assistant) — clearing it")
        self.mqtt.publish(self._announce[0], "", retain=True)
        for topic, payload in self._discovery:
            if payload:
                self.mqtt.publish(topic, "", retain=True)

    # node API view -------------------------------------------------------------

    def _changed(self) -> None:
        if self.node is not None:
            self.node.changed()

    @property
    def amp_connected(self) -> bool:
        return self._amp_connected

    @property
    def amp_audio(self) -> bool:
        return self.roster.amp is not None and self.engine.amp_audio

    @property
    def auto_reconnect(self) -> bool:
        return self._auto_reconnect

    @property
    def bt_streaming(self) -> bool:
        return self._bt_streaming

    @property
    def bt_now_playing(self) -> dict:
        return self._bt_now_playing

    @property
    def source(self) -> str:
        bt, ap = self._bt_streaming, self._airplay_active()
        return "both" if (bt and ap) else "bluetooth" if bt else "airplay" if ap else "idle"

    def last_result(self, slug: str) -> dict | None:
        return self._last_results.get(slug)

    def _record_result(self, slug: str, action: str, ok: bool, err: str | None,
                       **extra) -> None:
        ts = _now_iso()
        self._last_results[slug] = {"ok": ok, "error": err, "at": ts}
        self._publish_retained(
            self.cfg.topic("device", slug, "result"),
            json.dumps({"action": action, "ok": ok, "error": err, **extra, "ts": ts}))
        self._changed()

    async def amp_command(self, action: str) -> tuple[bool, str | None]:
        """The API's amp connect / disconnect / reconnect."""
        if action == "reconnect":
            return await self._fix_metadata()
        return await self._device_command(AMP_SLUG, action)

    def set_auto_reconnect(self, enabled: bool) -> None:
        self._set_auto_reconnect(enabled)
        self._changed()

    def amp_roster_changed(self) -> None:
        """The API paired or forgot the amp."""
        if self.roster.amp is None:
            for task in (self._amp_reconnect_task, self._amp_audio_task):
                if task is not None:
                    task.cancel()
            self._amp_connected = False
            self._last_results.pop(AMP_SLUG, None)
            self._publish_retained(self.cfg.topic("device", AMP_SLUG, "connected"), "OFF")
        self._changed()

    # BluezEngine sink interface -------------------------------------------

    def device_changed(self, slug: str, connected: bool) -> None:
        log.info("device %s: %s", slug, "connected" if connected else "disconnected")
        self._publish_retained(self.cfg.topic("device", slug, "connected"),
                               "ON" if connected else "OFF")
        if slug == AMP_SLUG:
            self._amp_connected = connected
            if connected:
                self._amp_connects += 1
                self._amp_user_disconnected = False
                self._maybe_restore_amp_audio()
            else:
                self._maybe_auto_reconnect_amp()
            self._amp_edge_fire()
        self._changed()

    def amp_audio_changed(self, has: bool) -> None:
        """BluezEngine: the amp's A2DP transport came or went. Connected alone
        says nothing about it: the ACL link and AVRCP can stay up with the
        audio link gone (a WirePlumber restart drops it), and then AirPlay
        plays into nothing while everything reads "connected"."""
        if self.roster.amp is None:
            return
        log.info("amp audio link: %s", "up" if has else "down")
        if not has:
            self._maybe_restore_amp_audio()
        self._amp_edge_fire()
        self._changed()

    def _amp_edge_fire(self) -> None:
        """Wake whoever waits in _wait_amp: the amp connected, dropped, or
        its audio link came or went."""
        edge, self._amp_edge = self._amp_edge, asyncio.Event()
        edge.set()

    def streaming_changed(self, on: bool) -> None:
        log.info("bluetooth streaming: %s", on)
        self._bt_streaming = on
        self._publish_retained(self.cfg.topic("bt", "streaming"), "ON" if on else "OFF")
        self._publish_source()
        self._changed()

    def now_playing_changed(self, payload: dict) -> None:
        self._bt_now_playing = payload
        self._publish_retained(self.cfg.topic("bt", "now_playing"), json.dumps(payload))
        self._update_amp_export()
        self._changed()

    def devices_changed(self) -> None:
        if self.node is not None:
            self.node.devices_changed()

    def bluez_ready(self) -> None:
        if self.node is not None:
            self._spawn(self.node.bluez_ready())

    # ----------------------------------------------------------------------

    def _airplay_active(self) -> bool:
        return self.cfg.airplay_enabled and self.airplay.status != "idle"

    def _publish_source(self) -> None:
        self._publish_retained(self.cfg.topic("source"), self.source)

    def _airplay_changed(self, snapshot: dict) -> None:
        """ShairportLink callback: a shairport-sync property changed."""
        if self.airplay.apply(snapshot):
            self._publish_airplay()

    def _publish_airplay(self) -> None:
        self._publish_retained(self.cfg.topic("airplay", "now_playing"),
                               json.dumps(self.airplay.now_playing()))
        self._publish_source()
        self._update_amp_export()
        self._changed()

    # ------------------------------------------------- amp metadata export

    def _amp_export_source(self) -> str | None:
        """Who owns the amp screen: a playing source wins; Bluetooth breaks
        ties (its transport is what the amp's own buttons reach natively);
        a merely-paused source keeps the screen rather than blanking it."""
        bt_status = self._bt_now_playing.get("status")
        if bt_status == "playing":
            return "bluetooth"
        if self._airplay_active() and self.airplay.status == "playing":
            return "airplay"
        if bt_status not in (None, "idle"):
            return "bluetooth"
        if self._airplay_active():
            return "airplay"
        return None

    def _update_amp_export(self) -> None:
        if self.amp_export is None:
            return
        src = self._amp_export_source()
        if src == "bluetooth":
            p = self._bt_now_playing
            self.amp_export.update("bluetooth", p.get("status"), p.get("title"),
                                   p.get("artist"), p.get("album"),
                                   p.get("duration"))
        elif src == "airplay":
            a = self.airplay
            self.amp_export.update("airplay", a.status, a.title, a.artist,
                                   a.album, None)
        else:
            self.amp_export.update(None, None, None, None, None, None)

    def _amp_export_state(self, registered: bool) -> None:
        self._publish_retained(self.cfg.topic("amp", "metadata_export"),
                               "ON" if registered else "OFF")

    def _amp_export_command(self, cmd: str) -> None:
        """Amp button press, routed to whichever source owns the screen."""
        if self._amp_export_source() == "airplay":
            if self.shairport is not None:
                self.shairport.command(cmd)
        else:
            self._spawn(self._transport_command(cmd))

    # ---------------------------------------------------------- commands

    async def _device_command(self, slug: str, action: str) -> tuple[bool, str | None]:
        async with self._locks[slug]:
            if slug == AMP_SLUG:
                # record intent under the lock, at execution time: a deliberate
                # OFF suppresses auto-reconnect until something turns the amp
                # back on; a deliberate ON re-arms it. Written any earlier, a
                # concurrent op's Connected=True edge would clobber the OFF.
                self._amp_user_disconnected = action == "disconnect"
            if action == "connect":
                ok, err = await self.engine.connect_device(slug)
            else:
                ok, err = await self.engine.disconnect_device(slug)
        self._record_result(slug, action, ok, err)
        if slug == AMP_SLUG:
            # a drop edge that landed while we held the lock was suppressed;
            # re-evaluate now that the lock is free
            self._maybe_auto_reconnect_amp()
            self._maybe_restore_amp_audio()
        return ok, err

    async def _fix_metadata(self) -> tuple[bool, str | None]:
        async with self._locks["amp"]:
            # under the lock for the same reason as in _device_command
            self._amp_user_disconnected = False  # the user wants the amp up
            ok, err = await self.engine.fix_metadata()
        self._record_result(AMP_SLUG, "fix_metadata", ok, err)
        # a drop edge (or gate-ON) that landed while we held the lock was
        # suppressed; re-evaluate now that the lock is free
        self._maybe_auto_reconnect_amp()
        self._maybe_restore_amp_audio()
        return ok, err

    # ------------------------------------------------- amp recovery
    #
    # Two recoveries share one retry loop (_amp_retry): auto-reconnect for an
    # amp that dropped, and the audio-link restore for an amp that stayed
    # connected without its A2DP transport. Both obey the same gates (the
    # auto-reconnect switch, a deliberate disconnect) and re-check them under
    # the amp lock before every attempt, so a manual command, fix_metadata or
    # the amp itself fixing things first always wins.

    def _amp_gates_open(self) -> bool:
        return (self.roster.amp is not None and self._auto_reconnect
                and not self._amp_user_disconnected)

    def _amp_needs_reconnect(self) -> bool:
        return self._amp_gates_open() and not self._amp_connected

    def _amp_needs_audio(self) -> bool:
        return self._amp_gates_open() and self._amp_connected and not self.amp_audio

    async def _wait_amp(self, cond, timeout: float) -> bool:
        """Until cond() holds, woken by amp edges rather than polling; False
        if it still doesn't after `timeout` seconds."""
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond():
            left = end - loop.time()
            if left <= 0:
                return False
            try:
                await asyncio.wait_for(self._amp_edge.wait(), left)
            except asyncio.TimeoutError:
                return cond()
        return True

    async def _amp_retry(self, what: str, needed, attempt_fn, done,
                         wait_s: float, fail_msg: str) -> bool | None:
        """Up to amp_reconnect_tries attempts. Each re-checks needed() under
        the amp lock, runs attempt_fn(attempt) there, then waits up to wait_s
        for done(). True on success, False on giving up, None when it stopped
        mattering (the gate closed, or someone else fixed it)."""
        cfg = self.cfg
        err: str | None = None
        for attempt in range(1, cfg.amp_reconnect_tries + 1):
            async with self._locks[AMP_SLUG]:
                if not needed():
                    return None
                log.info("%s: attempt %d/%d", what, attempt, cfg.amp_reconnect_tries)
                ok, err = await attempt_fn(attempt)
            # a drop edge that landed while we held the lock was suppressed
            self._maybe_auto_reconnect_amp()
            # a failed call can still race a link that came up on its own
            # (InProgress): check, but don't sit out the wait for nothing
            if done() or (ok and await self._wait_amp(done, wait_s)):
                self._record_result(AMP_SLUG, what, True, None, attempt=attempt)
                return True
            if ok:
                err = fail_msg
            if attempt < cfg.amp_reconnect_tries:
                await asyncio.sleep(cfg.amp_reconnect_retry_delay_s)
        log.error("%s: giving up after %d attempts (%s)", what, cfg.amp_reconnect_tries, err)
        self._record_result(AMP_SLUG, what, False, err, attempt=cfg.amp_reconnect_tries)
        return False

    def _maybe_auto_reconnect_amp(self) -> None:
        """Schedule a reconnect cycle after an unexpected amp drop.

        Covers link loss, post-boot bring-up (the startup rescan force-publishes
        a disconnected amp) and bluetoothd restarts. Deliberate drops are
        filtered out: the amp lock is held during fix_metadata / manual
        commands, and a user OFF sets _amp_user_disconnected.
        """
        if not self._amp_needs_reconnect():
            return
        if self._locks[AMP_SLUG].locked():
            return  # deliberate amp operation in flight
        if self._amp_reconnect_task is not None and not self._amp_reconnect_task.done():
            return  # a cycle is already running for this outage
        self._amp_reconnect_task = self._spawn(self._amp_auto_reconnect())

    async def _amp_auto_reconnect(self) -> None:
        await asyncio.sleep(self.cfg.amp_reconnect_debounce_s)

        async def connect(_attempt):
            return await self.engine.connect_device(AMP_SLUG)

        # BlueZ can send the Connect reply before the batched Connected=True
        # PropertiesChanged: wait briefly for the edge rather than misread a
        # good connect as an instant drop
        await self._amp_retry("auto_reconnect", self._amp_needs_reconnect, connect,
                              lambda: self._amp_connected, 1.0,
                              "link dropped immediately after connect")

    def _maybe_restore_amp_audio(self) -> None:
        """Schedule a restore when the amp is connected without its A2DP
        link.

        Unlike _maybe_auto_reconnect_amp there is no "lock held: return"
        here, on purpose: the Connected=True edge of an auto-reconnect lands
        while that cycle holds the lock, and suppressing it would leave an
        amp that came back without audio with nobody to restore it. The task
        waits out its grace first and then takes the lock, so scheduling it
        early is harmless. One task covers the whole outage, its slow retry
        included."""
        if not self._amp_needs_audio():
            return
        if self._amp_audio_task is not None and not self._amp_audio_task.done():
            return
        self._amp_audio_task = self._spawn(self._restore_amp_audio())

    async def _restore_amp_audio(self) -> None:
        # the grace counts from the LATEST connect: a reconnect while we wait
        # starts it again, so its A2DP gets the same moment to come up
        while True:
            connects = self._amp_connects
            await asyncio.sleep(AMP_AUDIO_GRACE_S)
            if self._amp_connects == connects:
                break
        if not self._amp_needs_audio():
            return

        async def restore(attempt):
            log.warning("amp connected without its audio link: restoring it")
            # first the audio link alone; then the whole connection, the
            # same Disconnect + Connect as POST /amp/reconnect
            if attempt == 1:
                return await self.engine.connect_amp_audio()
            return await self.engine.fix_metadata()

        result = await self._amp_retry("audio_reconnect", self._amp_needs_audio, restore,
                                       lambda: self.amp_audio, AMP_AUDIO_WAIT_S,
                                       "no audio link after the reconnect")
        if result is not False:
            return
        # gave up: keep asking for the audio link alone, slowly, for as long as
        # the amp stays connected without it (docs/SETUP-API.md auto-reconnect)
        log.warning("amp audio link: retrying every %d s while the amp stays connected "
                    "without it", AMP_AUDIO_SLOW_RETRY_S)
        while True:
            await asyncio.sleep(AMP_AUDIO_SLOW_RETRY_S)
            async with self._locks[AMP_SLUG]:
                if not self._amp_needs_audio():
                    return
                ok, err = await self.engine.connect_amp_audio()
            if self.amp_audio or (ok and await self._wait_amp(lambda: self.amp_audio,
                                                              AMP_AUDIO_WAIT_S)):
                log.info("amp audio link: restored by the slow retry")
                self._record_result(AMP_SLUG, "audio_reconnect", True, None, slow=True)
                return
            log.info("amp audio link: slow retry failed (%s)",
                     err if not ok else "no audio link after ConnectProfile")

    def _set_auto_reconnect(self, enabled: bool) -> None:
        if enabled != self._auto_reconnect:
            log.info("amp auto-reconnect %s", "enabled" if enabled else "disabled")
        self._auto_reconnect = enabled
        _save_auto_reconnect(self.cfg.state_file, enabled)
        self._publish_retained(self.cfg.topic("amp", "auto_reconnect"),
                               "ON" if enabled else "OFF")
        if enabled:
            # turning the gate on expresses "keep the amp connected" — re-arm
            # even after a deliberate OFF, and act now if the amp is down
            self._amp_user_disconnected = False
            self._maybe_auto_reconnect_amp()
            self._maybe_restore_amp_audio()

    # ------------------------------------------------- system power commands

    async def _system_command(self, action: str) -> None:
        async with self._locks["system"]:
            log.warning("system %s requested via MQTT", action)
            # logind decides, through polkit: no sudo (system.py). Only the
            # API service's account holds that grant, so on a node installed
            # by install.sh this fails cleanly; the API's /node/reboot is the
            # way there
            ok, err = await power(action)
            if not ok:
                log.error("system %s failed: %s", action, err)
            topic = self.cfg.topic("system", "result")
            payload = json.dumps({"action": action, "ok": ok, "error": err,
                                  "ts": _now_iso()})
            self._retained[topic] = payload
            info = self.mqtt.publish(topic, payload, retain=True)
            # the Pi is about to go down — flush the result before MQTT dies
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: info.wait_for_publish(3))
            except Exception:
                pass

    async def _transport_command(self, cmd: str) -> None:
        ok, err = await self.engine.transport_command(cmd)
        if not ok:
            log.warning("transport command %r failed: %s", cmd, err)

    # ---------------------------------------------------------- MQTT inbound

    def handle_message(self, topic: str, payload: bytes, retained: bool) -> None:
        text = payload.decode("utf-8", errors="replace").strip()

        if topic == f"{self.cfg.discovery_prefix}/status":
            if text == "online":
                log.info("Home Assistant born — republishing discovery + state")
                self.republish_all()
            return

        base = self.cfg.base_topic + "/"
        if not topic.startswith(base):
            return
        parts = topic[len(base):].split("/")

        # a retained command is a stale leftover, never a fresh request
        # (for system/* it would even replay a shutdown on every startup)
        if retained and (parts[-1] == "set" or parts == ["amp", "fix_metadata"]
                         or (parts[0] == "system" and parts[-1] != "result")):
            log.warning("ignoring retained command on %s", topic)
            return

        if len(parts) == 3 and parts[0] == "device" and parts[2] == "set":
            slug = parts[1]
            if slug not in self.roster.by_slug:
                log.warning("command for unknown device slug %r", slug)
                return
            action = {"ON": "connect", "OFF": "disconnect"}.get(text.upper())
            if action is None:
                log.warning("unknown device command payload %r", text)
                return
            self._spawn(self._device_command(slug, action))

        elif parts == ["amp", "fix_metadata"]:
            self._spawn(self._fix_metadata())

        elif parts == ["amp", "auto_reconnect", "set"]:
            enabled = {"ON": True, "OFF": False}.get(text.upper())
            if enabled is None:
                log.warning("unknown auto_reconnect payload %r", text)
                return
            self._set_auto_reconnect(enabled)

        elif parts in (["system", "reboot"], ["system", "shutdown"]):
            if not self.cfg.power_commands:
                log.warning("power command %s received but [system] power_commands "
                            "is off", topic)
                return
            if text != "PRESS":
                log.warning("ignoring %s with payload %r (want PRESS)", topic, text)
                return
            self._spawn(self._system_command(parts[1]))

        elif parts == ["bt", "transport", "set"]:
            self._spawn(self._transport_command(text.lower()))

        elif parts == ["airplay", "remote"]:
            if retained:
                log.warning("ignoring retained command on %s", topic)
            elif self.shairport is None:
                log.warning("AirPlay command received but [airplay] is disabled")
            else:
                self.shairport.command(text.lower())

    # ---------------------------------------------------------- main loop

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        exit_code = 0
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        if self.shairport is not None:
            self._publish_airplay()  # seed retained topics before first AirPlay event
            await self.shairport.start()
        # seed the gate state so the HA switch is never unknown
        self._publish_retained(self.cfg.topic("amp", "auto_reconnect"),
                               "ON" if self._auto_reconnect else "OFF")
        if self.amp_export is not None:
            # seed OFF; flips ON when RegisterPlayer succeeds
            self._publish_retained(self.cfg.topic("amp", "metadata_export"), "OFF")
            await self.amp_export.start()

        # MQTT comes up first so availability/commands stay live (with clean
        # error results) even while bluetoothd is down and the engine retries.
        self.mqtt.start()

        control = None
        if self.node is not None:
            from .control import ControlServer
            control = ControlServer(self.node, self.cfg.control_socket)
            try:
                await control.start()
            except OSError as e:
                log.error("control socket %s: %s", self.cfg.control_socket, e)
                control = None

        async def run_engine():
            nonlocal exit_code
            try:
                await self.engine.start()
            except Exception:
                log.exception("BlueZ engine failed to start")
                exit_code = 1
                stop.set()
                return
            try:
                await self.engine.wait_for_disconnect()
            except Exception:
                pass
            log.error("system D-Bus connection lost — exiting for systemd restart")
            exit_code = 1
            stop.set()

        async def consume():
            while True:
                kind, topic, payload = await self.queue.get()
                try:
                    if kind == EV_CONNECTED:
                        self.republish_all()
                    elif kind == EV_MESSAGE:
                        self.handle_message(topic, payload[0], payload[1])
                except Exception:
                    log.exception("error handling %s %s", kind, topic)

        engine_runner = asyncio.create_task(run_engine())
        consumer = asyncio.create_task(consume())
        await stop.wait()
        log.info("shutting down")
        if control is not None:
            await control.stop()
        if self.node is not None:
            self.node.stop()
        if self.amp_export is not None:
            await self.amp_export.stop()
        if self.shairport is not None:
            await self.shairport.stop()
        for task in (engine_runner, consumer):
            task.cancel()
        self.mqtt.stop()
        return exit_code


async def _amain(cfg: Config) -> int:
    app = App(cfg, asyncio.get_running_loop())
    return await app.run()


def main() -> None:
    parser = argparse.ArgumentParser(prog="speakerd")
    parser.add_argument("--config", required=True, help="path to config.toml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        cfg = config_mod.load(args.config)
    except (OSError, config_mod.ConfigError) as e:
        log.error("bad config: %s", e)
        sys.exit(2)

    sys.exit(asyncio.run(_amain(cfg)))


if __name__ == "__main__":
    main()
