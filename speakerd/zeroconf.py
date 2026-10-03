"""The node's zeroconf record, published through avahi-daemon over D-Bus.

`_nowairplaying._tcp` on the HTTPS API port, with the TXT keys in
docs/SETUP-API.md#discovery. publish() replaces the record whenever the name or
a TXT value changes; an avahi-daemon restart republishes it, and a name
collision takes avahi's alternative name ("Bathroom Speaker #2").

Uses its own system-bus connection, so avahi's NameOwnerChanged never reaches
the BlueZ engine's handler.
"""
from __future__ import annotations

import asyncio
import logging

from dbus_next import BusType, Message, MessageType
from dbus_next.aio import MessageBus

log = logging.getLogger("speakerd.zeroconf")

AVAHI = "org.freedesktop.Avahi"
SERVER_IFACE = "org.freedesktop.Avahi.Server"
GROUP_IFACE = "org.freedesktop.Avahi.EntryGroup"
SERVICE_TYPE = "_nowairplaying._tcp"
IF_UNSPEC = -1
PROTO_UNSPEC = -1
STATE_COLLISION = 3
STATE_FAILURE = 4


def txt_records(txt: dict[str, str]) -> list[bytes]:
    return [f"{k}={v}".encode() for k, v in txt.items()]


class Zeroconf:
    def __init__(self):
        self._bus: MessageBus | None = None
        self._group: str | None = None
        self._name: str | None = None
        self._port = 0
        self._txt: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._bus.add_message_handler(self._on_message)
        for rule in (
            f"type='signal',sender='{AVAHI}',interface='{GROUP_IFACE}',member='StateChanged'",
            "type='signal',sender='org.freedesktop.DBus',interface='org.freedesktop.DBus',"
            f"member='NameOwnerChanged',arg0='{AVAHI}'",
        ):
            await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                             "org.freedesktop.DBus", "AddMatch", "s", [rule])

    async def stop(self) -> None:
        if self._bus is not None:
            if self._group is not None:
                try:
                    await self._call(AVAHI, self._group, GROUP_IFACE, "Free")
                except Exception:
                    pass
            self._bus.disconnect()
            self._bus = None

    async def publish(self, name: str, port: int, txt: dict[str, str]) -> None:
        """Publish, or replace, the record. A failure is logged, not raised:
        the node keeps working without zeroconf, HA just can't find it."""
        self._name, self._port, self._txt = name, port, dict(txt)
        await self._republish()

    async def _republish(self) -> None:
        if self._bus is None or self._name is None:
            return
        async with self._lock:
            try:
                if self._group is None:
                    reply = await self._call(AVAHI, "/", SERVER_IFACE, "EntryGroupNew")
                    self._group = reply.body[0]
                else:
                    await self._call(AVAHI, self._group, GROUP_IFACE, "Reset")
                await self._call(
                    AVAHI, self._group, GROUP_IFACE, "AddService", "iiussssqaay",
                    [IF_UNSPEC, PROTO_UNSPEC, 0, self._name, SERVICE_TYPE, "", "",
                     self._port, txt_records(self._txt)])
                await self._call(AVAHI, self._group, GROUP_IFACE, "Commit")
                log.info("zeroconf: %r on port %d %s", self._name, self._port, self._txt)
            except _CallError as e:
                log.warning("zeroconf publish failed (%s) — is avahi-daemon running?", e)
                self._group = None

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_message(self, msg: Message):
        if msg.message_type != MessageType.SIGNAL:
            return None
        if msg.member == "NameOwnerChanged":
            _name, _old, new = msg.body
            self._group = None  # groups die with the daemon
            if new:
                log.info("avahi-daemon (re)started — republishing")
                self._spawn(self._republish())
        elif (msg.interface == GROUP_IFACE and msg.member == "StateChanged"
              and msg.path == self._group):
            state = msg.body[0]
            if state == STATE_COLLISION:
                self._spawn(self._rename_after_collision())
            elif state == STATE_FAILURE:
                log.warning("zeroconf: avahi reports failure: %s", msg.body[1])
        return None

    async def _rename_after_collision(self) -> None:
        try:
            reply = await self._call(AVAHI, "/", SERVER_IFACE,
                                     "GetAlternativeServiceName", "s", [self._name])
        except _CallError as e:
            log.warning("zeroconf: name collision and no alternative (%s)", e)
            return
        log.warning("zeroconf: %r is taken on the network — using %r",
                    self._name, reply.body[0])
        self._name = reply.body[0]
        await self._republish()

    async def _call(self, destination, path, interface, member, signature="", body=None):
        try:
            reply = await asyncio.wait_for(self._bus.call(Message(
                destination=destination, path=path, interface=interface, member=member,
                signature=signature, body=body or [])), 10)
        except (asyncio.TimeoutError, OSError) as e:
            raise _CallError(f"{member}: {e or 'timed out'}") from None
        if reply is None:
            raise _CallError(f"{member}: no reply")
        if reply.message_type == MessageType.ERROR:
            raise _CallError(f"{reply.error_name}: {reply.body[0] if reply.body else ''}")
        return reply


class _CallError(Exception):
    pass
