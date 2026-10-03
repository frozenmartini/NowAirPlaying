"""BlueZ system-bus engine.

Purely event-driven: one ObjectManager scan at startup (and after a
bluetoothd restart), then D-Bus signals only. Object paths for transports
and players churn on every reconnect, so nothing below caches a path beyond
its InterfacesRemoved.

Publishing goes through a `sink` object providing:
    device_changed(slug: str, connected: bool)
    streaming_changed(on: bool)            # already debounced here
    now_playing_changed(payload: dict)
    devices_changed()                      # the adapter's device list changed
    bluez_ready()                          # after every full rescan

Two views of devices: the roster's (the amp and config.toml's [[devices]]),
which get per-device connect state and auto-reconnect; and every device on
our adapter (`devices()`), which is where phones, the scan list and the amp's
paired state come from. BlueZ's bond list is the source of truth for phones.
"""
from __future__ import annotations

import asyncio
import logging
import re

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio import MessageBus

from .config import Config
from .roster import Roster

log = logging.getLogger("speakerd.bluez")

BLUEZ = "org.bluez"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
DEVICE_IFACE = "org.bluez.Device1"
TRANSPORT_IFACE = "org.bluez.MediaTransport1"
PLAYER_IFACE = "org.bluez.MediaPlayer1"
ADAPTER_IFACE = "org.bluez.Adapter1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

# Device1 properties kept for every device on our adapter; RSSI churns during
# a scan and is not worth a devices_changed() on its own
_DEVICE_PROPS = ("Address", "Alias", "Name", "Paired", "Trusted", "Connected",
                 "RSSI", "Class", "UUIDs")
_QUIET_PROPS = {"RSSI"}
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"

_DEV_PATH_RE = re.compile(r"/dev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})")

STREAMING_STATES = ("pending", "active")

CONNECT_TIMEOUT_S = 30


def _mac_from_path(path: str) -> str | None:
    m = _DEV_PATH_RE.search(path)
    return m.group(1).replace("_", ":").upper() if m else None


class BluezEngine:
    def __init__(self, cfg: Config, sink, roster: Roster | None = None):
        self._cfg = cfg
        self._sink = sink
        self._roster = roster if roster is not None else Roster(cfg)
        self._bus: MessageBus | None = None
        self._adapter_path = f"/org/bluez/{cfg.adapter}"
        # path -> {prop: value} for every device on our adapter
        self._devices: dict[str, dict] = {}

        # mac -> {path: connected} (a device can exist on several adapters)
        self._dev_paths: dict[str, dict[str, bool]] = {}
        self._connected: dict[str, bool] = {}          # mac -> published value
        self._transports: dict[str, tuple[str, str]] = {}  # path -> (mac, state)
        self._players: dict[str, dict] = {}            # path -> {mac,status,track,position,seq}
        self._seq = 0

        self._amp_audio_published: bool | None = None
        self._streaming_current = False
        self._streaming_published = False
        self._streaming_task: asyncio.Task | None = None
        self._last_now_playing_key: tuple | None = None
        self._rescan_lock = asyncio.Lock()
        self._sig_buffer: list[Message] | None = None
        self._tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> asyncio.Task:
        # keep a strong reference: the loop only holds weak refs to tasks
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus.add_message_handler(self._handle_signal)
        for rule in (
            f"type='signal',sender='{BLUEZ}',interface='{PROPS_IFACE}',member='PropertiesChanged'",
            f"type='signal',sender='{BLUEZ}',interface='{OM_IFACE}'",
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
            f"member='NameOwnerChanged',arg0='{BLUEZ}'",
        ):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                             "org.freedesktop.DBus", "AddMatch", "s", [rule])
        await self.rescan()

    async def wait_for_disconnect(self) -> None:
        await self._bus.wait_for_disconnect()

    async def rescan(self) -> None:
        """Full state rebuild from GetManagedObjects; retries while bluetoothd is down."""
        async with self._rescan_lock:
            await self._rescan_locked()

    async def _rescan_locked(self) -> None:
        # Signals can arrive in the same socket drain as the GetManagedObjects
        # reply and would be wiped by the clear+ingest below — buffer them
        # while the snapshot is in flight and replay them on top of it.
        self._sig_buffer = []
        try:
            objects = None
            while objects is None:
                try:
                    reply = await self._call(BLUEZ, "/", OM_IFACE, "GetManagedObjects")
                    objects = reply.body[0]
                except _DBusCallError as e:
                    log.warning("GetManagedObjects failed (%s) — bluetoothd down? retrying in 3s", e)
                    self._sig_buffer.clear()  # stale pre-snapshot signals
                    await asyncio.sleep(3)

            self._dev_paths.clear()
            self._transports.clear()
            self._players.clear()
            self._devices.clear()
            for path, ifaces in objects.items():
                self._ingest(path, ifaces)
            buffered = self._sig_buffer
        finally:
            self._sig_buffer = None
        for msg in buffered:
            self._process_signal(msg)
        log.info("rescan: %d device paths, %d transports, %d players",
                 sum(len(v) for v in self._dev_paths.values()),
                 len(self._transports), len(self._players))
        self._publish_all(force=True)
        self._notify("devices_changed")
        self._notify("bluez_ready")

    def _notify(self, name: str) -> None:
        cb = getattr(self._sink, name, None)
        if cb is not None:
            try:
                cb()
            except Exception:
                log.exception("sink %s failed", name)

    # ------------------------------------------------------------- ingestion

    def _ingest(self, path: str, ifaces: dict) -> None:
        dev = ifaces.get(DEVICE_IFACE)
        if dev is not None and "Address" in dev:
            mac = str(dev["Address"].value).upper()
            if mac in self._roster.by_mac:
                connected = bool(dev["Connected"].value) if "Connected" in dev else False
                self._dev_paths.setdefault(mac, {})[path] = connected
            if path.startswith(self._adapter_path + "/"):
                self._devices[path] = {k: _plain(dev[k].value) for k in _DEVICE_PROPS
                                       if k in dev}

        # transports and players: any device on the adapter. Phones paired
        # over the API are sources too, not only config.toml's [[devices]]
        if TRANSPORT_IFACE in ifaces:
            mac = _mac_from_path(path)
            if mac is not None:
                state = str(ifaces[TRANSPORT_IFACE].get("State").value) \
                    if ifaces[TRANSPORT_IFACE].get("State") else "idle"
                self._transports[path] = (mac, state)

        if PLAYER_IFACE in ifaces:
            mac = _mac_from_path(path)
            if mac is not None:
                props = ifaces[PLAYER_IFACE]
                self._seq += 1
                self._players[path] = {
                    "mac": mac,
                    "status": str(props["Status"].value) if "Status" in props else "stopped",
                    "track": self._unwrap_track(props["Track"].value) if "Track" in props else {},
                    "position": int(props["Position"].value) if "Position" in props else None,
                    "seq": self._seq,
                }

    @staticmethod
    def _unwrap_track(track_variant_dict) -> dict:
        out = {}
        for k, v in track_variant_dict.items():
            out[k] = v.value if hasattr(v, "value") else v
        return out

    # ------------------------------------------------------------- signals

    def _handle_signal(self, msg: Message):
        # dbus-next dispatches this on the asyncio loop thread — safe to mutate state.
        if msg.message_type != MessageType.SIGNAL:
            return None
        if msg.member == "NameOwnerChanged":
            _name, _old, new = msg.body
            self._spawn(self._on_bluez_owner_changed(new))
            return None
        if self._sig_buffer is not None:
            self._sig_buffer.append(msg)
            return None
        self._process_signal(msg)
        return None

    def _process_signal(self, msg: Message) -> None:
        try:
            if msg.interface == PROPS_IFACE and msg.member == "PropertiesChanged":
                iface, changed, _invalidated = msg.body
                self._on_props_changed(msg.path, iface, changed)
            elif msg.interface == OM_IFACE and msg.member == "InterfacesAdded":
                path, ifaces = msg.body
                self._on_interfaces_added(path, ifaces)
            elif msg.interface == OM_IFACE and msg.member == "InterfacesRemoved":
                path, ifaces = msg.body
                self._on_interfaces_removed(path, ifaces)
        except Exception:
            log.exception("error handling D-Bus signal %s.%s at %s",
                          msg.interface, msg.member, msg.path)

    def _on_props_changed(self, path: str, iface: str, changed: dict) -> None:
        if iface == DEVICE_IFACE:
            entry = self._devices.get(path)
            if entry is not None:
                loud = False
                for k in _DEVICE_PROPS:
                    if k in changed:
                        v = _plain(changed[k].value)
                        if entry.get(k) != v:
                            entry[k] = v
                            loud = loud or k not in _QUIET_PROPS
                if loud:
                    self._notify("devices_changed")
            if "Connected" in changed:
                mac = _mac_from_path(path)
                if mac in self._roster.by_mac:
                    self._dev_paths.setdefault(mac, {})[path] = bool(changed["Connected"].value)
                    self._publish_device(mac)

        elif iface == TRANSPORT_IFACE and "State" in changed:
            # only a transport we know: a late State for one already removed
            # (rescan replays buffered signals) must not bring it back, or a
            # missing audio link would read as present
            if path in self._transports:
                mac = self._transports[path][0]
                self._transports[path] = (mac, str(changed["State"].value))
                self._recompute_streaming()

        elif iface == PLAYER_IFACE:
            mac = _mac_from_path(path)
            if mac is None:
                return
            player = self._players.get(path)
            if player is None:
                self._seq += 1
                player = {"mac": mac, "status": "stopped", "track": {},
                          "position": None, "seq": self._seq}
                self._players[path] = player
            material = False
            if "Status" in changed:
                player["status"] = str(changed["Status"].value)
                material = True
            if "Track" in changed:
                player["track"] = self._unwrap_track(changed["Track"].value)
                material = True
            if "Position" in changed:
                player["position"] = int(changed["Position"].value)
            if material:
                self._seq += 1
                player["seq"] = self._seq
                self._publish_now_playing()

    def _on_interfaces_added(self, path: str, ifaces: dict) -> None:
        self._ingest(path, ifaces)
        if DEVICE_IFACE in ifaces:
            mac = _mac_from_path(path)
            if mac in self._roster.by_mac:
                self._publish_device(mac)
            if path in self._devices:
                self._notify("devices_changed")
        if TRANSPORT_IFACE in ifaces:
            self._recompute_streaming()
            self._publish_amp_audio()
        if PLAYER_IFACE in ifaces:
            self._publish_now_playing()

    def _on_interfaces_removed(self, path: str, ifaces: list) -> None:
        if DEVICE_IFACE in ifaces:
            mac = _mac_from_path(path)
            if mac in self._roster.by_mac and mac in self._dev_paths:
                self._dev_paths[mac].pop(path, None)
                self._publish_device(mac)
            if self._devices.pop(path, None) is not None:
                self._notify("devices_changed")
        if TRANSPORT_IFACE in ifaces and path in self._transports:
            del self._transports[path]
            self._recompute_streaming()
            self._publish_amp_audio()
        if PLAYER_IFACE in ifaces and path in self._players:
            del self._players[path]
            self._publish_now_playing()

    async def _on_bluez_owner_changed(self, new_owner: str) -> None:
        if new_owner:
            log.warning("bluetoothd (re)started — rescanning in 1s")
            await asyncio.sleep(1)
            await self.rescan()
        else:
            log.warning("bluetoothd went away — clearing state")
            self._dev_paths.clear()
            self._transports.clear()
            self._players.clear()
            self._devices.clear()
            self._publish_all(force=False)
            self._notify("devices_changed")

    # ------------------------------------------------------------- publishing

    def _publish_all(self, force: bool) -> None:
        for dev in self._roster.all_devices:
            self._publish_device(dev.mac, force=force)
        self._publish_amp_audio(force=force)
        self._recompute_streaming()
        if force:
            # seed the retained topic even when the value never changed (startup)
            self._sink.streaming_changed(self._streaming_published)
        self._publish_now_playing(force=force)

    def _publish_device(self, mac: str, force: bool = False) -> None:
        connected = any(self._dev_paths.get(mac, {}).values())
        if force or self._connected.get(mac) != connected:
            self._connected[mac] = connected
            self._sink.device_changed(self._roster.by_mac[mac].slug, connected)

    @property
    def amp_audio(self) -> bool:
        """The one copy of this value: App.amp_audio reads it here."""
        return bool(self._amp_audio_published)

    def _publish_amp_audio(self, force: bool = False) -> None:
        """Whether the amp has an A2DP transport: the Pi's audio link to it.
        Connected alone isn't enough. The ACL link and AVRCP can stay up with
        the audio link gone (a WirePlumber restart drops it), and then AirPlay
        plays into nothing while everything reads "connected"."""
        amp = self._roster.amp_mac
        has = amp is not None and any(mac == amp for mac, _ in self._transports.values())
        if force or has != self._amp_audio_published:
            self._amp_audio_published = has
            cb = getattr(self._sink, "amp_audio_changed", None)
            if cb is not None:
                cb(has)

    def _recompute_streaming(self) -> None:
        # every device but the amp is a source: the amp's own transport is the
        # Pi streaming TO it, not a phone streaming to the Pi
        amp = self._roster.amp_mac
        val = any(mac != amp and state in STREAMING_STATES
                  for mac, state in self._transports.values())
        self._streaming_current = val
        if self._streaming_task is not None:
            self._streaming_task.cancel()
            self._streaming_task = None
        if val == self._streaming_published:
            return
        if val:
            # rising edge: publish immediately
            self._streaming_published = True
            self._sink.streaming_changed(True)
        else:
            # falling edge: debounce track-change flaps
            self._streaming_task = self._spawn(self._publish_streaming_off_later())

    async def _publish_streaming_off_later(self) -> None:
        try:
            await asyncio.sleep(self._cfg.streaming_debounce_s)
        except asyncio.CancelledError:
            return
        if not self._streaming_current and self._streaming_published:
            self._streaming_published = False
            self._sink.streaming_changed(False)

    def _current_player(self) -> dict | None:
        amp = self._roster.amp_mac
        players = [p for p in self._players.values() if p["mac"] != amp]
        if not players:
            return None
        playing = [p for p in players if p["status"] == "playing"]
        pool = playing or players
        return max(pool, key=lambda p: p["seq"])

    def _publish_now_playing(self, force: bool = False) -> None:
        p = self._current_player()
        if p is None:
            payload = {"status": "idle", "title": None, "artist": None,
                       "album": None, "duration": None, "position": None,
                       "device": None}
            key = ("idle",)
        else:
            track = p["track"]
            payload = {
                "status": p["status"],
                "title": track.get("Title"),
                "artist": track.get("Artist"),
                "album": track.get("Album"),
                "duration": track.get("Duration"),
                "position": p["position"],
                "device": self._device_name(p["mac"]),
            }
            key = (p["status"], payload["title"], payload["artist"],
                   payload["album"], payload["device"])
        if force or key != self._last_now_playing_key:
            self._last_now_playing_key = key
            self._sink.now_playing_changed(payload)

    # ------------------------------------------------------------- commands

    async def connect_device(self, slug: str) -> tuple[bool, str | None]:
        return await self._device_call(slug, "Connect")

    async def disconnect_device(self, slug: str) -> tuple[bool, str | None]:
        return await self._device_call(slug, "Disconnect")

    async def fix_metadata(self) -> tuple[bool, str | None]:
        """Kohler recipe: disconnect amp, wait, connect amp (phone must stay connected)."""
        ok, err = await self._device_call("amp", "Disconnect")
        if not ok:
            log.warning("fix_metadata: amp disconnect failed (%s), connecting anyway", err)
        await asyncio.sleep(self._cfg.fix_metadata_delay_s)
        return await self._device_call("amp", "Connect")

    async def connect_amp_audio(self) -> tuple[bool, str | None]:
        """ConnectProfile(A2DP sink) on the amp: the audio link alone, without
        dropping the rest of the connection."""
        return await self._device_call("amp", "ConnectProfile", "s", [A2DP_SINK_UUID])

    async def transport_command(self, cmd: str) -> tuple[bool, str | None]:
        player = self._current_player()
        if player is None:
            return False, "no active Bluetooth player"
        path = next(p for p, v in self._players.items() if v is player)
        if cmd == "playpause":
            cmd = "pause" if player["status"] == "playing" else "play"
        member = {"play": "Play", "pause": "Pause", "stop": "Stop",
                  "next": "Next", "previous": "Previous"}.get(cmd)
        if member is None:
            return False, f"unknown transport command {cmd!r}"
        try:
            await self._call(BLUEZ, path, PLAYER_IFACE, member, timeout=10)
            return True, None
        except _DBusCallError as e:
            return False, str(e)

    def _call_path(self, mac: str) -> str:
        """Where to send a device call: the path the device is connected on,
        if any (it can exist on several adapters), else our own adapter's."""
        connected = sorted(p for p, c in self._dev_paths.get(mac, {}).items() if c)
        return connected[0] if connected else self.device_path(mac)

    async def _device_call(self, slug: str, member: str, signature: str = "",
                           body: list | None = None) -> tuple[bool, str | None]:
        dev = self._roster.by_slug.get(slug)
        if dev is None:
            return False, f"unknown device {slug!r}"
        path = self._call_path(dev.mac)
        log.info("%s %s (%s)", member, dev.name, path)
        try:
            await self._call(BLUEZ, path, DEVICE_IFACE, member, signature, body,
                             timeout=CONNECT_TIMEOUT_S)
            return True, None
        except _DBusCallError as e:
            log.warning("%s %s failed: %s", member, dev.name, e)
            return False, str(e)

    # ------------------------------------------------------ all devices, adapter

    def device_path(self, mac: str) -> str:
        return f"{self._adapter_path}/dev_{mac.upper().replace(':', '_')}"

    def _device_name(self, mac: str) -> str | None:
        dev = self._roster.by_mac.get(mac)
        if dev is not None:
            return dev.name
        entry = self._devices.get(self.device_path(mac))
        return (entry or {}).get("Alias") or (entry or {}).get("Name")

    def devices(self) -> list[dict]:
        """Every device on our adapter: mac, name, paired, trusted, connected,
        rssi, likely_amp (an audio sink, as a sorting hint only)."""
        out = []
        for entry in self._devices.values():
            mac = str(entry.get("Address", "")).upper()
            if not mac:
                continue
            cls = entry.get("Class")
            uuids = [str(u).lower() for u in entry.get("UUIDs") or ()]
            out.append({
                "mac": mac,
                "name": entry.get("Alias") or entry.get("Name"),
                "paired": bool(entry.get("Paired")),
                "trusted": bool(entry.get("Trusted")),
                "connected": bool(entry.get("Connected")),
                "rssi": entry.get("RSSI"),
                # major device class 0x04 = audio/video
                "likely_amp": (A2DP_SINK_UUID in uuids
                               or (isinstance(cls, int) and (cls >> 8) & 0x1F == 0x04)),
            })
        return sorted(out, key=lambda d: d["mac"])

    def device(self, mac: str) -> dict | None:
        mac = mac.upper()
        return next((d for d in self.devices() if d["mac"] == mac), None)

    async def roster_changed(self) -> None:
        """The amp was paired or forgotten: rebuild everything from BlueZ."""
        self._connected = {m: v for m, v in self._connected.items()
                           if m in self._roster.by_mac}
        await self.rescan()

    async def get_adapter(self, prop: str):
        reply = await self._call(BLUEZ, self._adapter_path, PROPS_IFACE, "Get", "ss",
                                 [ADAPTER_IFACE, prop])
        return _plain(reply.body[0].value)

    async def set_adapter(self, prop: str, signature: str, value) -> None:
        await self._set(self._adapter_path, ADAPTER_IFACE, prop, signature, value)

    async def set_device(self, mac: str, prop: str, signature: str, value) -> None:
        await self._set(self.device_path(mac), DEVICE_IFACE, prop, signature, value)

    async def _set(self, path: str, iface: str, prop: str, signature: str, value) -> None:
        await self._call(BLUEZ, path, PROPS_IFACE, "Set", "ssv",
                         [iface, prop, Variant(signature, value)])

    async def start_discovery(self) -> None:
        try:
            await self._call(BLUEZ, self._adapter_path, ADAPTER_IFACE, "SetDiscoveryFilter",
                             "a{sv}", [{"Transport": Variant("s", "bredr")}])
        except _DBusCallError as e:
            log.warning("SetDiscoveryFilter failed (%s) — scanning unfiltered", e)
        await self._call(BLUEZ, self._adapter_path, ADAPTER_IFACE, "StartDiscovery")

    async def stop_discovery(self) -> None:
        try:
            await self._call(BLUEZ, self._adapter_path, ADAPTER_IFACE, "StopDiscovery")
        except _DBusCallError as e:
            log.info("StopDiscovery: %s", e)  # already stopped

    async def device_method(self, mac: str, member: str,
                            timeout: float = CONNECT_TIMEOUT_S) -> tuple[bool, str | None]:
        """Pair / Connect / Disconnect / CancelPairing on any device by MAC."""
        try:
            await self._call(BLUEZ, self.device_path(mac), DEVICE_IFACE, member,
                             timeout=timeout)
            return True, None
        except _DBusCallError as e:
            log.warning("%s %s failed: %s", member, mac, e)
            return False, str(e)

    async def remove_device(self, mac: str) -> tuple[bool, str | None]:
        try:
            await self._call(BLUEZ, self._adapter_path, ADAPTER_IFACE, "RemoveDevice", "o",
                             [self.device_path(mac)])
            return True, None
        except _DBusCallError as e:
            return False, str(e)

    async def register_agent(self, path: str, capability: str) -> None:
        await self._call(BLUEZ, "/org/bluez", AGENT_MANAGER_IFACE, "RegisterAgent", "os",
                         [path, capability])
        await self._call(BLUEZ, "/org/bluez", AGENT_MANAGER_IFACE, "RequestDefaultAgent",
                         "o", [path])

    @property
    def bus(self) -> MessageBus | None:
        return self._bus

    # ------------------------------------------------------------- low level

    async def _call(self, destination: str, path: str, interface: str, member: str,
                    signature: str = "", body: list | None = None,
                    timeout: float = 15) -> Message:
        if self._bus is None:
            raise _DBusCallError(f"{member}: not connected to D-Bus yet")
        try:
            reply = await asyncio.wait_for(
                self._bus.call(Message(destination=destination, path=path,
                                       interface=interface, member=member,
                                       signature=signature, body=body or [])),
                timeout)
        except asyncio.TimeoutError:
            raise _DBusCallError(f"{member}: timed out after {timeout}s") from None
        except OSError as e:
            raise _DBusCallError(f"{member}: {e}") from None
        if reply is None:
            raise _DBusCallError(f"{member}: no reply")
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else ""
            raise _DBusCallError(f"{reply.error_name}: {detail}")
        return reply


def _plain(v):
    """A D-Bus value with any Variant wrapping removed, lists as tuples."""
    if isinstance(v, Variant):
        v = v.value
    if isinstance(v, list):
        return tuple(_plain(x) for x in v)
    return v


class _DBusCallError(Exception):
    pass


DBusCallError = _DBusCallError
