"""NetworkManager over D-Bus: the network state, and adding a Wi-Fi network.

- state(): read-only, for `network` in the state object. Reading needs no
  privilege, so the API service (nowairplaying-api) calls it.
- add_wifi(): only ever called as root, by wifi_add.py (Home Assistant over
  SSH with sudo, or the boot-partition Wi-Fi file). No account the node API
  runs as can change networking (docs/SETUP-API.md#wi-fi).

Never nmcli: a password on a command line is readable by every local user
through /proc.
"""
from __future__ import annotations

import asyncio
import logging
import uuid

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio import MessageBus

log = logging.getLogger("speakerd.netman")

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
SETTINGS_PATH = "/org/freedesktop/NetworkManager/Settings"
PROPS = "org.freedesktop.DBus.Properties"
DEVICE_TYPE_ETHERNET = 1
DEVICE_TYPE_WIFI = 2
PROFILE_PREFIX = "NowAirPlaying "


def wifi_settings(ssid: str, password: str | None, hidden: bool) -> dict:
    """A NetworkManager connection for one Wi-Fi network, autoconnecting at
    the default priority: it sits alongside the existing one and NetworkManager
    uses whichever is in range."""
    settings = {
        "connection": {
            "id": Variant("s", PROFILE_PREFIX + ssid),
            "uuid": Variant("s", str(uuid.uuid4())),
            "type": Variant("s", "802-11-wireless"),
            "autoconnect": Variant("b", True),
        },
        "802-11-wireless": {
            "ssid": Variant("ay", ssid.encode()),
            "mode": Variant("s", "infrastructure"),
            "hidden": Variant("b", hidden),
        },
        "ipv4": {"method": Variant("s", "auto")},
        "ipv6": {"method": Variant("s", "auto")},
    }
    if password:
        settings["802-11-wireless-security"] = {
            "key-mgmt": Variant("s", "wpa-psk"),
            "psk": Variant("s", password),
        }
    return settings


class NmClient:
    def __init__(self):
        self._bus: MessageBus | None = None

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()

    async def stop(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None

    async def state(self) -> dict:
        """network in the state object."""
        out = {"link": None, "ssid": None, "signal": None, "ip": None}
        try:
            primary = await self._get(NM_PATH, NM, "PrimaryConnection")
            if primary in (None, "/"):
                return out
            devices = await self._get(primary, f"{NM}.Connection.Active", "Devices")
            if not devices:
                return out
            dev = devices[0]
            dtype = await self._get(dev, f"{NM}.Device", "DeviceType")
            out["link"] = ("wifi" if dtype == DEVICE_TYPE_WIFI
                           else "ethernet" if dtype == DEVICE_TYPE_ETHERNET else None)
            ip4 = await self._get(dev, f"{NM}.Device", "Ip4Config")
            if ip4 and ip4 != "/":
                data = await self._get(ip4, f"{NM}.IP4Config", "AddressData")
                if data:
                    out["ip"] = data[0]["address"].value
            if dtype == DEVICE_TYPE_WIFI:
                ap = await self._get(dev, f"{NM}.Device.Wireless", "ActiveAccessPoint")
                if ap and ap != "/":
                    ssid = await self._get(ap, f"{NM}.AccessPoint", "Ssid")
                    out["ssid"] = bytes(ssid).decode("utf-8", errors="replace")
                    out["signal"] = int(await self._get(ap, f"{NM}.AccessPoint", "Strength"))
        except NmError as e:
            log.debug("network state: %s", e)
        return out

    async def add_wifi(self, ssid: str, password: str | None, hidden: bool) -> str:
        """Add (or replace our earlier profile for) one network. Root only."""
        reply = await self._call(SETTINGS_PATH, f"{NM}.Settings", "ListConnections")
        for path in reply.body[0]:
            settings = await self._call(path, f"{NM}.Settings.Connection", "GetSettings")
            conn = settings.body[0].get("connection", {})
            if conn.get("id") and conn["id"].value == PROFILE_PREFIX + ssid:
                await self._call(path, f"{NM}.Settings.Connection", "Delete")
                log.info("replaced the earlier profile for %r", ssid)
        reply = await self._call(SETTINGS_PATH, f"{NM}.Settings", "AddConnection",
                                 "a{sa{sv}}", [wifi_settings(ssid, password, hidden)])
        return reply.body[0]

    async def _get(self, path: str, iface: str, prop: str):
        reply = await self._call(path, PROPS, "Get", "ss", [iface, prop])
        v = reply.body[0]
        return v.value if isinstance(v, Variant) else v

    async def _call(self, path, interface, member, signature="", body=None) -> Message:
        if self._bus is None:
            raise NmError("not connected")
        try:
            reply = await asyncio.wait_for(self._bus.call(Message(
                destination=NM, path=path, interface=interface, member=member,
                signature=signature, body=body or [])), 20)
        except (asyncio.TimeoutError, OSError) as e:
            raise NmError(f"{member}: {e or 'timed out'}") from None
        if reply is None:
            raise NmError(f"{member}: no reply")
        if reply.message_type == MessageType.ERROR:
            raise NmError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply


class NmError(Exception):
    pass
