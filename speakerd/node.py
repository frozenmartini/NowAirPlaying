"""speakerd's side of the node API: the audio state and the audio commands.

Runs in speakerd, as the audio account `nowairplaying`, which holds no polkit
grants. The API service (apiserver.py, account `nowairplaying-api`) owns the
claim, HTTPS, zeroconf, updates and power, and reaches this over the local
control socket (control.py). Everything here is Bluetooth, AirPlay and the
node name: the pairing window and agent policy, scanning and pairing the amp,
phones, media keys, and renaming.

The state object and every rule are docs/SETUP-API.md's.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

from .agent import AGENT_PATH, CAPABILITY, PairingAgent
from .bluez import DBusCallError
from .errors import ApiError, canon_mac, int_in, now_iso, valid_name
from .roster import load_state, update_state
from .system import run, session_checks

log = logging.getLogger("speakerd.node")

SCAN_MAX_S = 30
PAIRING_MAX_S = 300
PAIR_TIMEOUT_S = 60


class AudioNode:
    def __init__(self, app):
        self.app = app
        self.cfg = app.cfg
        self.roster = app.roster
        self.engine = app.engine
        self.agent = PairingAgent(self)
        self.name: str = (load_state(self.cfg.state_file).get("name")
                          or self._shairport_name() or self.cfg.node_name)
        self.adapter_mac: str | None = None
        self.on_change = None  # set by the control server

        self._phone_results: dict[str, dict] = {}
        self._known_paired: set[str] | None = None
        self._pairing_until: float | None = None
        self._pairing_until_iso: str | None = None
        self._last_paired: str | None = None
        self._pairing_task: asyncio.Task | None = None
        self._scan_task: asyncio.Task | None = None
        self._amp_pairing_mac: str | None = None
        self._busy = asyncio.Lock()  # scan and pair
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()

    def changed(self) -> None:
        if self.on_change is not None:
            self.on_change()

    # ------------------------------------------------------------- the RPC surface

    async def rpc(self, method: str, p: dict):
        """One control-socket call. Raises ApiError for the API to return."""
        if method == "state":
            return self.audio_state()
        if method == "verify":
            return await self.verify()
        if method == "mqtt.discovery":
            self.app.set_mqtt_discovery(bool(p.get("enabled")))
            return True
        if method == "node.rename":
            await self.rename(p.get("name"))
            return True
        if method == "amp.scan":
            await self.scan(p.get("seconds", 20))
            return True
        if method == "amp.found":
            return self.found()
        if method == "amp.pair":
            await self.pair_amp(p.get("mac"))
            return True
        if method == "amp.forget":
            await self.forget_amp()
            return True
        if method == "amp.command":
            await self.amp_command(p.get("action"))
            return True
        if method == "amp.auto_reconnect":
            on = p.get("on")
            if not isinstance(on, bool):
                raise ApiError(400, "bad_request", "on: true or false")
            self.app.set_auto_reconnect(on)
            return True
        if method == "phones.pairing":
            await self.set_pairing(p)
            return True
        if method == "phones.command":
            await self.phone_command(p.get("mac"), p.get("action"))
            return True
        if method == "phones.forget":
            await self.forget_phone(p.get("mac"))
            return True
        if method == "media":
            await self.media(p.get("source"), p.get("action"))
            return True
        raise ApiError(404, "not_found", f"no control method {method!r}")

    # ------------------------------------------------------------- BlueZ hooks

    async def bluez_ready(self) -> None:
        """After every BlueZ rescan: agent, adapter name and address."""
        bus = self.engine.bus
        if bus is not None:
            try:
                bus.export(AGENT_PATH, self.agent)
            except Exception:
                pass  # already exported on this connection
            try:
                await self.engine.register_agent(AGENT_PATH, CAPABILITY)
                log.info("pairing agent registered (%s)", CAPABILITY)
            except DBusCallError as e:
                if "AlreadyExists" not in str(e):
                    log.error("could not register the pairing agent: %s", e)
        try:
            self.adapter_mac = str(await self.engine.get_adapter("Address")).upper()
            await self._apply_alias()
            await self.engine.set_adapter("Pairable", "b", True)
        except DBusCallError as e:
            log.warning("adapter setup: %s", e)
        self.changed()

    def devices_changed(self) -> None:
        """Trust a phone that just paired in the window, then push state."""
        amp = self.roster.amp_mac
        paired = {d["mac"] for d in self.engine.devices() if d["paired"]}
        if self._known_paired is not None:
            for mac in sorted(paired - self._known_paired):
                if mac == amp or mac == self._amp_pairing_mac:
                    continue
                if self.pairing_open:
                    log.info("phone %s paired in the window — trusting it", mac)
                    self._last_paired = mac
                    self._spawn(self._trust(mac))
        self._known_paired = paired
        self.changed()

    async def _trust(self, mac: str) -> None:
        try:
            await self.engine.set_device(mac, "Trusted", "b", True)
        except DBusCallError as e:
            log.warning("could not trust %s: %s", mac, e)

    # agent policy -------------------------------------------------------------

    @property
    def pairing_open(self) -> bool:
        loop = asyncio.get_running_loop()
        return self._pairing_until is not None and loop.time() < self._pairing_until

    def pin_for(self, mac: str) -> str | None:
        if mac == self._amp_pairing_mac or self.pairing_open:
            return "0000"  # the Kohler's fixed legacy PIN
        return None

    def accept_pairing(self, mac: str) -> bool:
        return mac == self._amp_pairing_mac or self.pairing_open

    def accept_service(self, mac: str, uuid: str) -> bool:
        dev = self.engine.device(mac)
        return bool(dev and dev["paired"]) or self.accept_pairing(mac)

    # ------------------------------------------------------------- the state

    def audio_state(self) -> dict:
        """speakerd's part of the state object; the API service adds the rest."""
        app = self.app
        amp = self.roster.amp
        amp_state = None
        if amp is not None:
            dev = self.engine.device(amp.mac) or {}
            amp_state = {"mac": amp.mac, "name": amp.name,
                         "paired": bool(dev.get("paired")),
                         "connected": bool(app.amp_connected),
                         "auto_reconnect": bool(app.auto_reconnect),
                         "last_result": app.last_result("amp")}
        phones = [{"mac": d["mac"], "name": d["name"], "connected": d["connected"],
                   "last_result": self._phone_results.get(d["mac"])}
                  for d in self.engine.devices()
                  if d["paired"] and d["mac"] != self.roster.amp_mac]
        return {
            "name": self.name,
            "adapter_mac": self.adapter_mac,
            "amp": amp_state,
            "phones": {"pairing": {"open": self._pairing_until is not None,
                                   "until": self._pairing_until_iso,
                                   "last_paired": self._last_paired},
                       "devices": phones},
            "source": app.source,
            "bluetooth_streaming": bool(app.bt_streaming),
            "now_playing": {"airplay": app.airplay.now_playing(),
                            "bluetooth": dict(app.bt_now_playing)},
        }

    async def verify(self) -> list[dict]:
        amp = self.roster.amp
        dev = self.engine.device(amp.mac) if amp else None
        app = self.app
        return await session_checks({
            "shairport_present": bool(app.shairport and app.shairport.snapshot()["present"]),
            "amp_configured": amp is not None,
            "amp_paired": bool(dev and dev["paired"] and dev["trusted"]),
            "amp_connected": bool(app.amp_connected),
            "amp_player": bool(app.amp_export and app.amp_export.registered),
            "mqtt_enabled": self.cfg.mqtt_enabled,
            "mqtt_connected": bool(getattr(app.mqtt, "connected", False)),
        })

    # ------------------------------------------------------------- amp

    async def scan(self, seconds) -> None:
        seconds = int_in(seconds, 1, SCAN_MAX_S, "seconds")
        if self._busy.locked():
            raise ApiError(409, "busy", "a scan or pair is already running")
        await self._busy.acquire()
        try:
            await self.engine.start_discovery()
        except DBusCallError as e:
            self._busy.release()
            raise ApiError(502, "failed", str(e))
        self._scan_task = self._spawn(self._end_scan(seconds))

    async def _end_scan(self, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
        finally:
            await self.engine.stop_discovery()
            self._scan_task = None
            self._busy.release()

    def found(self) -> dict:
        devices = [{k: d[k] for k in ("mac", "name", "rssi", "likely_amp")}
                   for d in self.engine.devices() if not d["paired"]]
        devices.sort(key=lambda d: (not d["likely_amp"], -(d["rssi"] or -999)))
        return {"scanning": self._scan_task is not None, "devices": devices}

    async def pair_amp(self, raw_mac) -> None:
        mac = canon_mac(raw_mac)
        if self._scan_task is not None:
            self._scan_task.cancel()  # its finally stops discovery and frees _busy
            try:
                await self._scan_task
            except asyncio.CancelledError:
                pass
        if self._busy.locked():
            raise ApiError(409, "busy", "a pair is already running")
        async with self._busy:
            self._amp_pairing_mac = mac
            try:
                dev = self.engine.device(mac)
                if dev is None:
                    raise ApiError(404, "not_found",
                                   f"{mac} has not been seen: put the amp in pairing mode and scan")
                if not dev["paired"]:
                    ok, err = await self.engine.device_method(mac, "Pair", timeout=PAIR_TIMEOUT_S)
                    if not ok and "AlreadyExists" not in (err or ""):
                        raise ApiError(502, "pair_failed", err or "pairing failed")
                await self.engine.set_device(mac, "Trusted", "b", True)
                ok, err = await self.engine.device_method(mac, "Connect")
                if not ok:
                    raise ApiError(502, "pair_failed", f"paired, but connect failed: {err}")
                name = dev.get("name") or self.cfg.amp_name
                self.roster.set_amp(mac, name)
                log.warning("amp paired: %s (%s)", mac, name)
                await self.engine.roster_changed()
            except DBusCallError as e:
                raise ApiError(502, "pair_failed", str(e))
            finally:
                self._amp_pairing_mac = None
        self.app.amp_roster_changed()
        self.changed()

    async def forget_amp(self) -> None:
        amp = self.roster.amp
        if amp is None:
            raise ApiError(404, "not_found", "no amp is paired")
        ok, err = await self.engine.remove_device(amp.mac)
        if not ok and "DoesNotExist" not in (err or ""):
            raise ApiError(502, "failed", err or "RemoveDevice failed")
        self.roster.set_amp(None)
        log.warning("amp forgotten: %s", amp.mac)
        await self.engine.roster_changed()
        self.app.amp_roster_changed()
        self.changed()

    async def amp_command(self, action) -> None:
        if action not in ("connect", "disconnect", "reconnect"):
            raise ApiError(400, "bad_request", f"no amp action {action!r}")
        if self.roster.amp is None:
            raise ApiError(404, "not_found", "no amp is paired")
        ok, err = await self.app.amp_command(action)
        if not ok:
            raise ApiError(502, "failed", err or f"{action} failed")

    # ------------------------------------------------------------- phones

    async def set_pairing(self, body: dict) -> None:
        if body.get("open") is False:
            await self._close_pairing()
            return
        seconds = int_in(body.get("seconds", 120), 1, PAIRING_MAX_S, "seconds")
        try:
            await self._apply_alias()
            await self.engine.set_adapter("Pairable", "b", True)
            await self.engine.set_adapter("DiscoverableTimeout", "u", seconds)
            await self.engine.set_adapter("Discoverable", "b", True)
        except DBusCallError as e:
            raise ApiError(502, "failed", str(e))
        self._pairing_until = asyncio.get_running_loop().time() + seconds
        self._pairing_until_iso = datetime.fromtimestamp(
            datetime.now().timestamp() + seconds, timezone.utc).astimezone().isoformat(
            timespec="seconds")
        if self._pairing_task is not None:
            self._pairing_task.cancel()
        self._pairing_task = self._spawn(self._pairing_timer(seconds))
        log.info("phone pairing window open for %ds", seconds)
        self.changed()

    async def _pairing_timer(self, seconds: int) -> None:
        await asyncio.sleep(seconds)
        self._pairing_task = None
        await self._close_pairing()

    async def _close_pairing(self) -> None:
        if self._pairing_task is not None:
            self._pairing_task.cancel()
            self._pairing_task = None
        self._pairing_until = self._pairing_until_iso = None
        try:
            await self.engine.set_adapter("Discoverable", "b", False)
        except DBusCallError as e:
            log.warning("could not end discoverable mode: %s", e)
        self.changed()

    def _phone(self, raw_mac) -> str:
        mac = canon_mac(raw_mac)
        dev = self.engine.device(mac)
        if dev is None or not dev["paired"] or mac == self.roster.amp_mac:
            raise ApiError(404, "not_found", f"no paired phone {mac}")
        return mac

    async def phone_command(self, raw_mac, action) -> None:
        member = {"connect": "Connect", "disconnect": "Disconnect"}.get(action)
        if member is None:
            raise ApiError(400, "bad_request", f"no phone action {action!r}")
        mac = self._phone(raw_mac)
        ok, err = await self.engine.device_method(mac, member)
        self._phone_results[mac] = {"ok": ok, "error": err, "at": now_iso()}
        self.changed()
        if not ok:
            raise ApiError(502, "failed", err or f"{member} failed")

    async def forget_phone(self, raw_mac) -> None:
        mac = self._phone(raw_mac)
        ok, err = await self.engine.remove_device(mac)
        if not ok:
            raise ApiError(502, "failed", err or "RemoveDevice failed")
        self._phone_results.pop(mac, None)
        if self._last_paired == mac:
            self._last_paired = None
        self.changed()

    # ------------------------------------------------------------- media

    async def media(self, source, action) -> None:
        if action not in ("playpause", "next", "previous"):
            raise ApiError(404, "not_found", f"no action {action!r}")
        if source == "airplay":
            if self.app.shairport is None or not self.app.shairport.command(action):
                raise ApiError(502, "failed", "shairport-sync is not on the bus")
        elif source == "bluetooth":
            ok, err = await self.engine.transport_command(action)
            if not ok:
                raise ApiError(502, "failed", err or "no Bluetooth player")
        else:
            raise ApiError(404, "not_found", f"no source {source!r}")

    # ------------------------------------------------------------- name

    async def rename(self, raw) -> None:
        name = valid_name(raw)
        self.name = name
        update_state(self.cfg.state_file, name=name)
        self._rewrite_shairport_name(name)
        try:
            await self._apply_alias()
        except DBusCallError as e:
            log.warning("could not rename the Bluetooth adapter: %s", e)
        rc, out = await run("systemctl", "--user", "restart", "shairport-sync.service")
        if rc != 0:
            log.warning("shairport-sync restart after rename: %s", out)
        log.info("renamed to %r", name)
        self.changed()

    def _shairport_name(self) -> str | None:
        """The AirPlay name the install wrote (its --name), until a rename
        stores one in state.json: so the API, zeroconf and the Bluetooth name
        all start out as the name iPhones already show."""
        try:
            with open(self.cfg.shairport_conf, encoding="utf-8") as f:
                m = re.search(r'^\s*name\s*=\s*"([^"]*)"', f.read(), flags=re.M)
        except OSError:
            return None
        if not m or "%" in m.group(1):  # shairport-sync's own %H-style names
            return None
        try:
            return valid_name(m.group(1))
        except ApiError:
            return None

    def _rewrite_shairport_name(self, name: str) -> None:
        path = self.cfg.shairport_conf
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            log.warning("cannot read %s: %s", path, e)
            return
        new, n = re.subn(r'^(\s*name\s*=\s*)"[^"]*"', lambda m: f'{m.group(1)}"{name}"',
                         text, count=1, flags=re.M)
        if n == 0:
            log.warning("%s has no name line to set", path)
            return
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new)
        os.replace(tmp, path)

    async def _apply_alias(self) -> None:
        await self.engine.set_adapter("Alias", "s", f"Bluetooth {self.name}")
