"""shairport-sync <-> speakerd over shairport's native D-Bus interface.

shairport-sync (built --with-dbus-interface, run with dbus_service_bus =
"session") owns org.gnome.ShairportSync on the user's session bus, the same
bus speakerd's user unit sees. That one interface carries both directions:

- in:  Active (org.gnome.ShairportSync), PlayerState, ClientName and Metadata
       (org.gnome.ShairportSync.RemoteControl), pushed as PropertiesChanged;
- out: RemoteControl.Play/Pause/PlayPause/Stop/Next/Previous, which shairport
       forwards to the sender over DACP.

No broker sits between the two, so the amp screen gets AirPlay metadata with
MQTT switched off. This is NOT shairport's MPRIS interface: that one must stay
out of the build, because a second player next to the amp export is the
failure the export exists to remove.

shairport-sync may start, stop or restart at any time; the name watch below
resyncs from GetAll on every new owner and reports "gone" when it vanishes.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Callable

from dbus_next import Message, MessageType, Variant
from dbus_next.aio import MessageBus

log = logging.getLogger("speakerd.shairport")

SS_NAME = "org.gnome.ShairportSync"
SS_PATH = "/org/gnome/ShairportSync"
IF_MAIN = SS_NAME
IF_RC = f"{SS_NAME}.RemoteControl"
PROPS_IFACE = "org.freedesktop.DBus.Properties"

# speakerd's verbs (amp buttons, HA's airplay/remote payloads) -> RemoteControl
# methods. HA's buttons send shairport's DACP verbs, so both spellings map.
COMMANDS = {
    "play": "Play",
    "pause": "Pause",
    "playpause": "PlayPause",
    "stop": "Stop",
    "next": "Next", "nextitem": "Next",
    "previous": "Previous", "previtem": "Previous",
}


def _session_bus_address() -> str:
    # systemd --user exports this when dbus-user-session is installed; the
    # fallback is where that same bus lives, for a daemon started by hand
    return (os.environ.get("DBUS_SESSION_BUS_ADDRESS")
            or f"unix:path=/run/user/{os.getuid()}/bus")


def _unwrap(v):
    """Variant (possibly nested in dicts/lists) -> plain Python value."""
    if isinstance(v, Variant):
        return _unwrap(v.value)
    if isinstance(v, dict):
        return {k: _unwrap(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_unwrap(x) for x in v]
    return v


class ShairportLink:
    """Watches shairport-sync on the session bus; reports a snapshot dict
    {present, active, player_state, client_name, metadata} on every change."""

    def __init__(self, on_change: Callable[[dict], None],
                 bus_address: str | None = None, retry_s: float = 3):
        self._on_change = on_change
        self._bus_address = bus_address
        self._retry_s = retry_s
        self._bus: MessageBus | None = None
        self._owner: str | None = None   # unique name of the live shairport
        self._gen = 0                    # bumps per owner; stale resyncs drop
        self._props: dict[str, dict] = {IF_MAIN: {}, IF_RC: {}}
        self._run_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        self._run_task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        tasks = [t for t in (self._run_task, *self._tasks) if t is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._bus is not None:
            try:
                self._bus.disconnect()
            except Exception:
                pass

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self) -> None:
        while True:
            try:
                self._bus = await MessageBus(
                    bus_address=self._bus_address or _session_bus_address()).connect()
                self._bus.add_message_handler(self._on_message)
                for rule in (
                        "type='signal',sender='org.freedesktop.DBus',"
                        "interface='org.freedesktop.DBus',member='NameOwnerChanged',"
                        f"arg0='{SS_NAME}'",
                        f"type='signal',sender='{SS_NAME}',path='{SS_PATH}',"
                        f"interface='{PROPS_IFACE}',member='PropertiesChanged'"):
                    await self._call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                     "org.freedesktop.DBus", "AddMatch", "s", [rule])
                try:
                    reply = await self._call(
                        "org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "GetNameOwner", "s", [SS_NAME])
                    # the match is live before this call, so a NameOwnerChanged
                    # may already have handled this owner: resync only once
                    if reply.body[0] != self._owner:
                        self._owner_changed(reply.body[0])
                except RuntimeError:
                    log.info("shairport-sync not on the session bus yet — waiting")
                    if self._owner is None:
                        self._emit()  # report "absent" once at startup
                await self._bus.wait_for_disconnect()
                log.error("session bus connection lost — reconnecting in %ss",
                          self._retry_s)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("session bus setup failed — retrying in %ss", self._retry_s)
                if self._bus is not None:
                    try:
                        self._bus.disconnect()
                    except Exception:
                        pass
            self._bus = None
            self._owner_changed(None)
            await asyncio.sleep(self._retry_s)

    # ------------------------------------------------------------- state in

    def _owner_changed(self, owner: str | None) -> None:
        self._gen += 1
        self._owner = owner or None
        self._props = {IF_MAIN: {}, IF_RC: {}}
        if self._owner is None:
            self._emit()
            return
        log.info("shairport-sync on the session bus (%s)", self._owner)
        self._spawn(self._resync(self._gen))

    async def _resync(self, gen: int, tries: int = 3) -> None:
        for attempt in range(1, tries + 1):
            try:
                for iface in (IF_MAIN, IF_RC):
                    reply = await self._call(SS_NAME, SS_PATH, PROPS_IFACE,
                                             "GetAll", "s", [iface])
                    if gen != self._gen:
                        return  # the owner changed while we waited; its own resync runs
                    # signals for THIS interface that arrived while its GetAll
                    # was in flight are older than the reply (one sender,
                    # ordered delivery), so the reply wins. Applied per
                    # interface: a signal for IF_MAIN landing while IF_RC's
                    # GetAll is in flight is newer and must survive.
                    self._props[iface] = _unwrap(reply.body[0])
                self._emit()
                return
            except Exception as e:
                if gen != self._gen:
                    return
                log.warning("shairport-sync GetAll failed (%d/%d): %s", attempt, tries, e)
                if attempt < tries:
                    await asyncio.sleep(self._retry_s)
        # still present with whatever signals delivered; report that rather
        # than leave the app on the previous owner's state
        self._emit()

    def _on_message(self, msg: Message):
        if msg.message_type != MessageType.SIGNAL:
            return None
        if msg.member == "NameOwnerChanged" and msg.body and msg.body[0] == SS_NAME:
            _name, _old, new = msg.body
            if (new or None) != self._owner:
                if not new:
                    log.warning("shairport-sync left the session bus")
                self._owner_changed(new)
        elif (msg.member == "PropertiesChanged" and msg.path == SS_PATH
              and msg.sender == self._owner and self._owner is not None):
            iface, changed, invalidated = msg.body
            if iface not in self._props:
                return None
            if invalidated:
                self._spawn(self._resync(self._gen))  # values not sent: re-read all
                return None
            self._props[iface].update(_unwrap(changed))
            self._emit()
        return None

    def snapshot(self) -> dict:
        main, rc = self._props[IF_MAIN], self._props[IF_RC]
        return {
            "present": self._owner is not None,
            "active": bool(main.get("Active", False)),
            "player_state": rc.get("PlayerState") or "Not Available",
            "client_name": rc.get("ClientName") or None,
            "metadata": rc.get("Metadata") or {},
        }

    def _emit(self) -> None:
        try:
            self._on_change(self.snapshot())
        except Exception:
            log.exception("AirPlay state handler failed")

    # ----------------------------------------------------------- commands out

    def command(self, verb: str) -> bool:
        """Fire-and-forget a transport command. False if the verb is unknown
        or shairport-sync is not on the bus."""
        member = COMMANDS.get(verb)
        if member is None:
            log.warning("unknown AirPlay command %r", verb)
            return False
        if self._bus is None or self._owner is None:
            log.warning("AirPlay command %r dropped: shairport-sync not on the bus", verb)
            return False
        self._spawn(self._command(member))
        return True

    async def _command(self, member: str) -> None:
        try:
            await self._call(SS_NAME, SS_PATH, IF_RC, member)
        except Exception as e:
            log.warning("AirPlay %s failed: %s", member, e)

    async def _call(self, destination: str, path: str, interface: str,
                    member: str, signature: str = "", body: list | None = None,
                    timeout: float = 10) -> Message:
        bus = self._bus
        if bus is None:
            raise RuntimeError(f"{member}: no bus")
        reply = await asyncio.wait_for(
            bus.call(Message(destination=destination, path=path,
                             interface=interface, member=member,
                             signature=signature, body=body or [])),
            timeout)
        if reply is None:
            raise RuntimeError(f"{member}: no reply")
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else ""
            raise RuntimeError(f"{reply.error_name}: {detail}")
        return reply
