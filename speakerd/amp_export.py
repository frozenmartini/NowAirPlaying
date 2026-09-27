"""AVRCP metadata export to the amp — org.bluez.Media1, one permanent player.

The Kohler amp is a pre-AVRCP-1.4 controller: it registers for notifications
once per session, and a player unregistered while a replacement is live fires
BlueZ's Addressed-Player-Changed rejection storm that the amp never recovers
from (measured on the wire, 2026-08-25). The one shape it
handles perfectly is property changes on a player that never goes away.

So: register exactly ONE player with bluetoothd on the amp's adapter and only
ever update its properties. Source switches (phone Bluetooth <-> AirPlay) and
track changes become PropertiesChanged on the same object — register and
unregister simply never happen. BlueZ's Media1.RegisterPlayer consumes the
MPRIS 2.2 Player interface directly (doc/org.bluez.Media.rst: "must implement
at least org.mpris.MediaPlayer2.Player"; bluetoothd maps xesam:* and
mpris:length onto AVRCP track attributes in profiles/audio/media.c:2501).

This replaces mpris-proxy, which bridges session-bus players 1:1 and so
re-registers on every iOS player churn. The mpris_proxy_bridge unit MUST be
disabled when this is enabled — two exporters means two players, and Mode B
returns.

Lifecycle safety comes from the API itself: bluetoothd auto-unregisters the
player when this process's bus connection dies (a vanish with no replacement
— the safe transition), and a restarting daemon registers into an empty
session — also safe. The export runs on its OWN system-bus connection,
isolated from the engine's, so the two lifecycles cannot entangle.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
from collections import deque
from datetime import datetime

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

from .config import Config

log = logging.getLogger("speakerd.amp_export")

PLAYER_PATH = "/org/speakerd/player"
MEDIA_IFACE = "org.bluez.Media1"
REGISTER_RETRY_S = 30


class RawLog:
    """Append-only forensic log of everything crossing the export, unfiltered.

    Same spirit and timestamp format as btwatch, so acceptance runs can
    correlate this file line-for-line with the AVRCP wire capture: every
    feed (including ones deduplicated away), every emitted state, every
    button command arriving from the amp, every registration event. Enabled
    by [bluetooth] amp_export_raw_log = "<path>"; a no-op when unset.
    Write failures are logged once and never take the export down.

    Writing never touches the disk on the event loop: write() timestamps the
    line and queues it, and a drain task hands batches to a worker thread. A
    flush per line to an SD card is not a stall the loop — or a D-Bus method
    handler answering the amp's buttons — can afford. Outside the started
    window (before start(), after aclose()) there is no loop to protect and
    no drain task to do it, so those writes go straight to the file.
    """

    #: backlog cap — a wedged disk must not eat this 2 GB box's RAM
    MAX_PENDING = 10_000

    def __init__(self, path: str | None):
        self._path = path
        self._fh = None
        self._warned = False
        self._pending: deque[str] = deque()
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._dropped = 0
        self._io_lock = threading.Lock()

    # ----------------------------------------------- producer (never blocks)

    def write(self, kind: str, detail: str) -> None:
        if self._path is None:
            return
        # Timestamped at the call, never at the flush: these lines are
        # correlated against btwatch's capture line-for-line.
        ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
        line = f"{ts} [{kind:8}] {detail}\n"
        if self._task is None:            # not running — nothing to protect
            self._write_batch([line])
            return
        if len(self._pending) >= self.MAX_PENDING:
            # counted, and reported on both edges: a forensic log read
            # line-for-line against btwatch must never present a silent gap
            # as "nothing happened" (_dropped resets on recovery, so this
            # fires once per episode, not once per process)
            self._dropped += 1
            if self._dropped == 1:
                log.warning("amp-export raw log backlog hit %d lines — dropping "
                            "until it drains", self.MAX_PENDING)
            return
        self._pending.append(line)
        self._wake.set()

    # ----------------------------------------------- consumer (off the loop)

    async def start(self) -> None:
        if self._path is None:
            return
        self._wake = asyncio.Event()
        self._task = asyncio.get_running_loop().create_task(self._drain_loop())

    async def _drain_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            await self.drain()

    async def drain(self) -> None:
        """Hand everything queued to a worker thread and wait for it."""
        if not self._pending:
            return
        batch = list(self._pending)
        self._pending.clear()
        await asyncio.get_running_loop().run_in_executor(
            None, self._write_batch, batch)
        if self._dropped:
            dropped, self._dropped = self._dropped, 0
            log.warning("amp-export raw log caught up — %d line(s) lost while "
                        "the backlog was full", dropped)
            self.write("DROPPED", f"{dropped} line(s) lost to a full backlog")

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.drain()
        with self._io_lock:
            self._close_fh()

    # ----------------------------------------------- the actual file I/O

    def _write_batch(self, lines: list[str]) -> None:
        """Runs on a worker thread, or inline outside the started window.

        The lock matters because cancelling the drain task does not stop a
        thread already inside run_in_executor: a shutdown write can land here
        on the loop thread while that one is still flushing.
        """
        with self._io_lock:
            if self._path is None:
                return
            try:
                if self._fh is None:
                    parent = os.path.dirname(self._path)
                    if parent:
                        os.makedirs(parent, exist_ok=True)
                    self._fh = open(self._path, "a", encoding="utf-8")
                self._fh.writelines(lines)
                self._fh.flush()
            except OSError as e:
                if not self._warned:
                    self._warned = True
                    log.warning("amp-export raw log %s unwritable (%s) — disabled",
                                self._path, e)
                self._close_fh()
                self._path = None

    def _close_fh(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


def mpris_status(status: str | None) -> str:
    """BlueZ MediaPlayer1 / AirplayState status -> MPRIS PlaybackStatus."""
    if status in ("playing", "forward-seek", "reverse-seek"):
        return "Playing"
    if status == "paused":
        return "Paused"
    return "Stopped"


def mpris_metadata(title, artist, album, duration_ms, track_seq: int) -> dict:
    """The a{sv} Metadata dict bluetoothd maps to AVRCP track attributes.

    Every field is optional to bluetoothd (media.c parses what is present),
    so absent values are omitted and the amp shows exactly what we know.
    trackid bumps on track changes so TRACK_CHANGED edges are unmistakable.
    """
    md = {"mpris:trackid": Variant("o", f"/org/speakerd/track/{track_seq}")}
    if title:
        md["xesam:title"] = Variant("s", str(title))
    if artist:
        md["xesam:artist"] = Variant("as", [str(artist)])
    if album:
        md["xesam:album"] = Variant("s", str(album))
    if duration_ms:
        md["mpris:length"] = Variant("x", int(duration_ms) * 1000)  # ms -> us
    return md


class _MprisPlayer(ServiceInterface):
    """org.mpris.MediaPlayer2.Player — the amp's one and only player."""

    def __init__(self, on_command, raw: RawLog):
        super().__init__("org.mpris.MediaPlayer2.Player")
        self._on_command = on_command
        self._raw = raw
        self._status = "Stopped"
        self._metadata: dict = {}
        self._position = 0
        #: kept in step by the owner; the only honest answer to "did it land"
        self.registered = False

    def adopt(self, prev: "_MprisPlayer") -> None:
        """Carry the screen across a reconnect, so a fresh interface registers
        with what the amp is currently showing rather than a blank player."""
        self._status = prev._status
        self._metadata = prev._metadata
        self._position = prev._position

    def set_state(self, status: str, metadata: dict) -> None:
        self._status = status
        self._metadata = metadata
        # emit_properties_changed walks this interface's exported buses and
        # does nothing at all when there are none (dbus_next service.py:376),
        # so "the call did not raise" never meant the amp saw anything — it
        # logged emitted=yes for every pre-registration update. Registration
        # is the real answer: until RegisterPlayer succeeds bluetoothd has no
        # player to notify on, and reads current state at register time.
        ok = True
        try:
            self.emit_properties_changed(
                {"PlaybackStatus": self._status, "Metadata": self._metadata})
        except Exception:
            # With one live bus per interface nothing here should raise; if it
            # ever does the screen is stale, which is worth a log, not silence.
            ok = False
            log.exception("amp-export PropertiesChanged failed")
        emitted = self.registered and ok
        self._raw.write("EMIT", f"status={status} emitted={'yes' if emitted else 'no'} "
                        f"md={{{', '.join(f'{k}={v.value!r}' for k, v in metadata.items())}}}")

    def _cmd(self, member: str, cmd: str) -> None:
        self._raw.write("CMD", f"{member} -> {cmd}")
        self._on_command(cmd)

    # ---- properties bluetoothd reads (profiles/audio/media.c) ----

    @dbus_property(access=PropertyAccess.READ)
    def PlaybackStatus(self) -> "s":
        return self._status

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self._metadata

    @dbus_property(access=PropertyAccess.READ)
    def Position(self) -> "x":
        return self._position

    @dbus_property(access=PropertyAccess.READ)
    def CanGoNext(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanGoPrevious(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPlay(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanPause(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanControl(self) -> "b":
        return True

    @dbus_property(access=PropertyAccess.READ)
    def CanSeek(self) -> "b":
        return False

    # ---- controls the amp sends (AVRCP passthrough -> bluetoothd -> here) ----

    @method()
    def Play(self):
        self._cmd("Play", "play")

    @method()
    def Pause(self):
        self._cmd("Pause", "pause")

    @method()
    def PlayPause(self):
        self._cmd("PlayPause", "playpause")

    @method()
    def Stop(self):
        self._cmd("Stop", "stop")

    @method()
    def Next(self):
        self._cmd("Next", "next")

    @method()
    def Previous(self):
        self._cmd("Previous", "previous")

    @method()
    def Seek(self, offset: "x"):
        # CanSeek is False; tolerate a pushy controller but record it
        self._raw.write("CMD", f"Seek offset={offset} (ignored)")

    @method()
    def SetPosition(self, track_id: "o", position: "x"):
        self._raw.write("CMD", f"SetPosition {track_id} pos={position} (ignored)")

    @method()
    def OpenUri(self, uri: "s"):
        self._raw.write("CMD", f"OpenUri {uri!r} (ignored)")


class AmpMetadataExport:
    """Owns the export bus connection, registration, and screen state.

    App-facing surface:
        await start() / await stop()
        update(source, status, title, artist, album, duration_ms)
    Callbacks:
        on_command(cmd) — amp button presses ('play'/'pause'/'playpause'/
                          'stop'/'next'/'previous'), dispatched on the loop
        on_state(bool)  — registered/unregistered, for the retained
                          <base>/amp/metadata_export observability topic
    """

    def __init__(self, cfg: Config, on_command, on_state):
        self._cfg = cfg
        self._on_command = on_command
        self._on_state = on_state
        self._raw = RawLog(cfg.amp_export_raw_log)
        self._player = _MprisPlayer(on_command, self._raw)
        self._bus: MessageBus | None = None
        self._registered = False
        self._run_task: asyncio.Task | None = None
        self._register_task: asyncio.Task | None = None
        self._track_seq = 0
        self._last_key: tuple | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        await self._raw.start()
        self._run_task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        for task in (self._register_task, self._run_task):
            if task is not None:
                task.cancel()
        if self._bus is not None:
            try:
                self._bus.disconnect()  # bluetoothd auto-unregisters the player
            except Exception:
                pass
        self._raw.write("BUS", "export stopped (daemon shutdown)")
        await self._raw.aclose()

    def _new_player(self) -> _MprisPlayer:
        """One fresh interface per bus connection.

        A ServiceInterface remembers every bus it has been exported on and
        only forgets one on unexport (dbus_next service.py:423-428), so
        re-exporting a single instance across reconnects leaves dead
        connections in the set that emit_properties_changed keeps signalling.
        That set is unordered, so a dead bus can be signalled before the live
        one and take the whole emit down with it — the screen would quietly
        stop updating after a bus bounce. Build a new interface each time and
        carry the screen across.
        """
        player = _MprisPlayer(self._on_command, self._raw)
        player.adopt(self._player)
        return player

    async def _run(self) -> None:
        while True:
            try:
                self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
                self._player = self._new_player()
                self._bus.export(PLAYER_PATH, self._player)
                await self._call(
                    "org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "AddMatch", "s",
                    ["type='signal',sender='org.freedesktop.DBus',"
                     "interface='org.freedesktop.DBus',"
                     "member='NameOwnerChanged',arg0='org.bluez'"])
                self._bus.add_message_handler(self._handle_signal)
                self._raw.write("BUS", f"connected, player exported at {PLAYER_PATH}")
                self._schedule_register(0)
                await self._bus.wait_for_disconnect()
                self._raw.write("BUS", "connection lost")
                log.error("amp-export bus connection lost — reconnecting in 3s")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("amp export bus setup failed — retrying in 3s")
                # A failure after connect() (e.g. AddMatch) leaves a live,
                # exported connection behind; the next loop iteration would
                # overwrite self._bus and leak one fd per retry. Close it.
                if self._bus is not None:
                    try:
                        self._bus.disconnect()
                    except Exception:
                        pass
                    self._bus = None
            self._set_registered(False)
            await asyncio.sleep(3)

    def _handle_signal(self, msg: Message):
        if msg.message_type == MessageType.SIGNAL and msg.member == "NameOwnerChanged":
            _name, old, new = msg.body
            self._raw.write("BLUEZ", f"owner change {old!r} -> {new!r}")
            if new:
                log.warning("bluetoothd (re)started — re-registering amp player")
                self._set_registered(False)
                self._schedule_register(2)  # let bluetoothd finish coming up
            else:
                self._set_registered(False)
        return None

    def _schedule_register(self, delay: float) -> None:
        if self._register_task is not None and not self._register_task.done():
            self._register_task.cancel()
        self._register_task = asyncio.get_running_loop().create_task(
            self._register_loop(delay))

    async def _register_loop(self, delay: float) -> None:
        if delay:
            await asyncio.sleep(delay)
        adapter_path = f"/org/bluez/{self._cfg.adapter}"
        while not self._registered:
            try:
                # bluetoothd learns a RegisterPlayer player's capabilities ONLY
                # from this dict (media.c:2658-2671 parses it; :2291 gates every
                # passthrough on mp->next/mp->control). An empty dict means the
                # amp's buttons are dropped before any method call — found live
                # 2026-08-25: metadata flowed (PropertiesChanged path) while
                # Next/Previous never arrived. Flags mirror the interface.
                caps = {
                    "PlaybackStatus": Variant("s", self._player.PlaybackStatus),
                    "Metadata": Variant("a{sv}", self._player.Metadata),
                    "CanPlay": Variant("b", True),
                    "CanPause": Variant("b", True),
                    "CanGoNext": Variant("b", True),
                    "CanGoPrevious": Variant("b", True),
                    "CanControl": Variant("b", True),
                }
                await self._call("org.bluez", adapter_path, MEDIA_IFACE,
                                 "RegisterPlayer", "oa{sv}", [PLAYER_PATH, caps])
                log.info("amp player registered on %s — one permanent player, "
                         "properties-only updates", adapter_path)
                self._raw.write("REGISTER", f"ok adapter={adapter_path}")
                self._set_registered(True)
                # nudge current state across; bluetoothd also reads it on register
                self._player.set_state(self._player.PlaybackStatus,
                                       self._player.Metadata)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._raw.write("REGISTER", f"fail adapter={adapter_path} err={e}")
                log.warning("RegisterPlayer on %s failed (%s) — retrying in %ss",
                            adapter_path, e, REGISTER_RETRY_S)
                await asyncio.sleep(REGISTER_RETRY_S)

    def _set_registered(self, value: bool) -> None:
        self._player.registered = value
        if value != self._registered:
            self._registered = value
            self._raw.write("STATE", "registered" if value else "unregistered")
            try:
                self._on_state(value)
            except Exception:
                log.exception("amp export state callback failed")

    # ------------------------------------------------------------- the feed

    def update(self, source: str | None, status: str | None, title, artist,
               album, duration_ms) -> None:
        """Set what the screen should show right now. Deduplicated; a change
        becomes one PropertiesChanged on the permanent player."""
        key = (source, mpris_status(status), title, artist, album, duration_ms)
        dedup = key == self._last_key
        self._raw.write("FEED", f"src={source} status={status!r} title={title!r} "
                        f"artist={artist!r} album={album!r} dur={duration_ms} "
                        f"dedup={'yes' if dedup else 'no'}")
        if dedup:
            return
        track_changed = self._last_key is None or key[2:5] != self._last_key[2:5]
        self._last_key = key
        if track_changed:
            self._track_seq += 1
        self._player.set_state(
            mpris_status(status),
            mpris_metadata(title, artist, album, duration_ms, self._track_seq))
        log.info("amp screen <- %s: %s — %s [%s]", source or "idle",
                 title or "-", artist or "-", mpris_status(status))

    # ------------------------------------------------------------- low level

    async def _call(self, destination: str, path: str, interface: str,
                    member: str, signature: str = "", body: list | None = None,
                    timeout: float = 15) -> Message:
        reply = await asyncio.wait_for(
            self._bus.call(Message(destination=destination, path=path,
                                   interface=interface, member=member,
                                   signature=signature, body=body or [])),
            timeout)
        if reply is None:
            raise RuntimeError(f"{member}: no reply")
        if reply.message_type == MessageType.ERROR:
            detail = reply.body[0] if reply.body else ""
            raise RuntimeError(f"{reply.error_name}: {detail}")
        return reply
