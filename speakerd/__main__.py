"""speakerd entry point: wires BlueZ engine + shairport D-Bus link + MQTT link + HA discovery."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
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
from .shairport import ShairportLink

log = logging.getLogger("speakerd")


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
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"amp_auto_reconnect": enabled}, f)
            f.flush()
            os.fsync(f.fileno())  # survive a power cut right after the toggle
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not persist state to %s: %s", path, e)


class App:
    """Owns retained-state cache and MQTT publishing; BluezEngine's sink."""

    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop):
        self.cfg = cfg
        self.queue: asyncio.Queue = asyncio.Queue()
        self.engine = BluezEngine(cfg, sink=self)
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
        self._tasks: set[asyncio.Task] = set()

        # amp auto-reconnect policy (HA-gated; see _maybe_auto_reconnect_amp)
        self._amp_connected = False
        self._amp_user_disconnected = False
        self._auto_reconnect = _load_auto_reconnect(cfg.state_file)
        self._amp_reconnect_task: asyncio.Task | None = None

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
        # the node announcement goes out before the entities it describes
        self.mqtt.publish(self._announce[0], self._announce[1], retain=True)
        for topic, payload in self._discovery:
            self.mqtt.publish(topic, payload, retain=True)
        for topic, payload in self._retained.items():
            self.mqtt.publish(topic, payload, retain=True)

    # BluezEngine sink interface -------------------------------------------

    def device_changed(self, slug: str, connected: bool) -> None:
        log.info("device %s: %s", slug, "connected" if connected else "disconnected")
        self._publish_retained(self.cfg.topic("device", slug, "connected"),
                               "ON" if connected else "OFF")
        if slug == AMP_SLUG:
            self._amp_connected = connected
            if connected:
                self._amp_user_disconnected = False
            else:
                self._maybe_auto_reconnect_amp()

    def streaming_changed(self, on: bool) -> None:
        log.info("bluetooth streaming: %s", on)
        self._bt_streaming = on
        self._publish_retained(self.cfg.topic("bt", "streaming"), "ON" if on else "OFF")
        self._publish_source()

    def now_playing_changed(self, payload: dict) -> None:
        self._bt_now_playing = payload
        self._publish_retained(self.cfg.topic("bt", "now_playing"), json.dumps(payload))
        self._update_amp_export()

    # ----------------------------------------------------------------------

    def _airplay_active(self) -> bool:
        return self.cfg.airplay_enabled and self.airplay.status != "idle"

    def _publish_source(self) -> None:
        bt, ap = self._bt_streaming, self._airplay_active()
        source = "both" if (bt and ap) else "bluetooth" if bt else "airplay" if ap else "idle"
        self._publish_retained(self.cfg.topic("source"), source)

    def _airplay_changed(self, snapshot: dict) -> None:
        """ShairportLink callback: a shairport-sync property changed."""
        if self.airplay.apply(snapshot):
            self._publish_airplay()

    def _publish_airplay(self) -> None:
        self._publish_retained(self.cfg.topic("airplay", "now_playing"),
                               json.dumps(self.airplay.now_playing()))
        self._publish_source()
        self._update_amp_export()

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

    async def _device_command(self, slug: str, action: str) -> None:
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
        self._publish_retained(
            self.cfg.topic("device", slug, "result"),
            json.dumps({"action": action, "ok": ok, "error": err, "ts": _now_iso()}))
        if slug == AMP_SLUG:
            # a drop edge that landed while we held the lock was suppressed;
            # re-evaluate now that the lock is free
            self._maybe_auto_reconnect_amp()

    async def _fix_metadata(self) -> None:
        async with self._locks["amp"]:
            # under the lock for the same reason as in _device_command
            self._amp_user_disconnected = False  # the user wants the amp up
            ok, err = await self.engine.fix_metadata()
        self._publish_retained(
            self.cfg.topic("device", "amp", "result"),
            json.dumps({"action": "fix_metadata", "ok": ok, "error": err, "ts": _now_iso()}))
        # a drop edge (or gate-ON) that landed while we held the lock was
        # suppressed; re-evaluate now that the lock is free
        self._maybe_auto_reconnect_amp()

    # ------------------------------------------------- amp auto-reconnect

    def _maybe_auto_reconnect_amp(self) -> None:
        """Schedule a reconnect cycle after an unexpected amp drop.

        Covers link loss, post-boot bring-up (the startup rescan force-publishes
        a disconnected amp) and bluetoothd restarts. Deliberate drops are
        filtered out: the amp lock is held during fix_metadata / manual
        commands, and a user OFF sets _amp_user_disconnected.
        """
        if not self._auto_reconnect or self._amp_user_disconnected or self._amp_connected:
            return
        if self._locks[AMP_SLUG].locked():
            return  # deliberate amp operation in flight
        if self._amp_reconnect_task is not None and not self._amp_reconnect_task.done():
            return  # a cycle is already running for this outage
        self._amp_reconnect_task = self._spawn(self._amp_auto_reconnect())

    async def _amp_auto_reconnect(self) -> None:
        cfg = self.cfg
        await asyncio.sleep(cfg.amp_reconnect_debounce_s)
        err: str | None = None
        for attempt in range(1, cfg.amp_reconnect_tries + 1):
            if not self._auto_reconnect or self._amp_user_disconnected:
                return
            async with self._locks[AMP_SLUG]:
                # re-check under the lock: the gate may have been switched off
                # while we waited on it, or a manual command, fix_metadata, or
                # the amp itself may have restored the link while we slept
                if (not self._auto_reconnect or self._amp_connected
                        or self._amp_user_disconnected):
                    return
                log.info("amp auto-reconnect: attempt %d/%d",
                         attempt, cfg.amp_reconnect_tries)
                ok, err = await self.engine.connect_device(AMP_SLUG)
            if ok and not self._amp_connected:
                # BlueZ can send the Connect reply before the batched
                # Connected=True PropertiesChanged: wait briefly for the edge
                # rather than misread a good connect as an instant drop
                for _ in range(10):
                    await asyncio.sleep(0.1)
                    if self._amp_connected:
                        break
            if ok and not self._amp_connected:
                # connect-ok + instant drop: the drop edge was suppressed
                # (this cycle is still running) — a failed attempt, not a success
                ok, err = False, "link dropped immediately after connect"
            if ok:
                self._publish_retained(
                    self.cfg.topic("device", AMP_SLUG, "result"),
                    json.dumps({"action": "auto_reconnect", "ok": True, "error": None,
                                "attempt": attempt, "ts": _now_iso()}))
                return
            if attempt < cfg.amp_reconnect_tries:
                await asyncio.sleep(cfg.amp_reconnect_retry_delay_s)
        log.error("amp auto-reconnect: giving up after %d attempts (%s)",
                  cfg.amp_reconnect_tries, err)
        self._publish_retained(
            self.cfg.topic("device", AMP_SLUG, "result"),
            json.dumps({"action": "auto_reconnect", "ok": False, "error": err,
                        "attempt": cfg.amp_reconnect_tries, "ts": _now_iso()}))

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

    # ------------------------------------------------- system power commands

    async def _system_command(self, action: str) -> None:
        verb = {"reboot": "reboot", "shutdown": "poweroff"}[action]
        async with self._locks["system"]:
            log.warning("system %s requested via MQTT", action)
            err: str | None = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    "sudo", "-n", "systemctl", verb,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE)
                try:
                    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except OSError:
                        pass  # sudo runs setuid root; kill may be EPERM
                    raise
                ok = proc.returncode == 0
                if not ok:
                    err = (stderr.decode("utf-8", errors="replace").strip()
                           or f"systemctl {verb} exited {proc.returncode}")
            except (OSError, asyncio.TimeoutError) as e:
                ok, err = False, f"systemctl {verb}: {e}"
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
            if slug not in self.cfg.by_slug:
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
