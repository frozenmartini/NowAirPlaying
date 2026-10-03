"""The control socket between the API service and speakerd.

speakerd (the audio account `nowairplaying`, no polkit grants) listens on a
Unix socket in /run/nowairplaying, a directory that is setgid group
`nowairplaying-api`, so only that account and speakerd itself can connect. The
API service (apiserver.py) is the one client.

Newline-delimited JSON, both ways:
    client  {"id": 7, "method": "amp.pair", "params": {"mac": "…"}}
    server  {"id": 7, "result": true}
            {"id": 7, "error": {"status": 502, "code": "pair_failed", "message": "…"}}
    server  {"event": "audio", "data": {…}}   speakerd's whole audio state, pushed
                                              after any change once the client has
                                              called "subscribe"
Requests run concurrently; replies carry their id. What the methods are is
node.AudioNode.rpc's business.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os

from .errors import ApiError

log = logging.getLogger("speakerd.control")

LINE_LIMIT = 1 << 20
DEFAULT_TIMEOUT_S = 40
LONG_TIMEOUT_S = 150  # pairing: up to 60 s Pair plus a 30 s Connect
LONG_METHODS = {"amp.pair", "amp.command"}


def _line(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode() + b"\n"


# ----------------------------------------------------------------- speakerd side

class ControlServer:
    def __init__(self, node, path: str):
        self._node = node
        self._path = path
        self._server: asyncio.AbstractServer | None = None
        self._subscribers: set[asyncio.StreamWriter] = set()
        self._scheduled = False
        self._last: dict | None = None
        self._tasks: set[asyncio.Task] = set()
        node.on_change = self.changed

    async def start(self) -> None:
        try:
            os.unlink(self._path)  # a stale socket from the last run
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._client, path=self._path,
                                                       limit=LINE_LIMIT)
        # the directory's setgid group (nowairplaying-api) is the socket's group
        os.chmod(self._path, 0o660)
        log.info("control socket on %s", self._path)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            for w in list(self._subscribers):
                w.close()
            await self._server.wait_closed()
            self._server = None

    def changed(self) -> None:
        """Coalesce everything in one loop iteration into one push."""
        if self._scheduled or not self._subscribers:
            return
        self._scheduled = True
        asyncio.get_running_loop().call_soon(self._push)

    def _push(self) -> None:
        self._scheduled = False
        state = self._node.audio_state()
        if state == self._last:
            return
        self._last = state
        data = _line({"event": "audio", "data": state})
        for w in list(self._subscribers):
            if w.is_closing():
                self._subscribers.discard(w)
                continue
            w.write(data)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        log.info("control client connected")
        try:
            while True:
                try:
                    line = await reader.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    log.warning("control: line too long — dropping the client")
                    break
                if not line:
                    break
                try:
                    req = json.loads(line)
                    rid, method = req["id"], str(req["method"])
                    params = req.get("params") or {}
                    if not isinstance(params, dict):
                        raise ValueError("params must be an object")
                except (ValueError, KeyError, TypeError) as e:
                    log.warning("control: bad request (%s)", e)
                    continue
                task = asyncio.get_running_loop().create_task(
                    self._serve(writer, rid, method, params))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except ConnectionError:
            pass
        finally:
            self._subscribers.discard(writer)
            writer.close()
            log.info("control client gone")

    async def _serve(self, writer, rid, method: str, params: dict) -> None:
        try:
            if method == "subscribe":
                self._subscribers.add(writer)
                result = self._node.audio_state()
                self._last = result
            else:
                result = await self._node.rpc(method, params)
            reply = {"id": rid, "result": result}
        except ApiError as e:
            reply = {"id": rid, "error": e.as_dict()}
        except Exception:
            log.exception("control: %s failed", method)
            reply = {"id": rid, "error": ApiError(502, "failed",
                                                  "internal error in speakerd").as_dict()}
        if not writer.is_closing():
            writer.write(_line(reply))
            try:
                await writer.drain()
            except ConnectionError:
                pass


# ----------------------------------------------------------------- API side

class AudioClient:
    """The API service's connection to speakerd. Reconnects forever; while
    it's down every call fails with 502 and `connected` is False."""

    def __init__(self, path: str, on_state, on_connect=None):
        self._path = path
        self._on_state = on_state      # called with speakerd's audio state, or None
        self._on_connect = on_connect  # async, after each (re)subscribe
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        self.connected = False

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._writer is not None:
            self._writer.close()

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(self._path,
                                                                    limit=LINE_LIMIT)
            except OSError as e:
                log.debug("speakerd not reachable at %s (%s)", self._path, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
                continue
            delay = 1.0
            self._writer = writer
            self.connected = True
            log.info("connected to speakerd")
            reading = asyncio.get_running_loop().create_task(self._read(reader))
            try:
                self._on_state(await self.call("subscribe"))
                if self._on_connect is not None:
                    await self._on_connect()
            except ApiError as e:
                log.warning("speakerd subscribe failed: %s", e.message)
            await reading
            self.connected = False
            self._writer = None
            writer.close()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ApiError(502, "failed", "speakerd went away"))
            self._pending.clear()
            log.warning("lost speakerd — reconnecting")
            self._on_state(None)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while True:
            try:
                line = await reader.readline()
            except (ConnectionError, ValueError, asyncio.LimitOverrunError):
                return
            if not line:
                return
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "audio":
                self._on_state(msg.get("data"))
                continue
            fut = self._pending.pop(msg.get("id"), None)
            if fut is None or fut.done():
                continue
            if "error" in msg:
                fut.set_exception(ApiError.from_dict(msg["error"] or {}))
            else:
                fut.set_result(msg.get("result"))

    async def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        if not self.connected or self._writer is None:
            raise ApiError(502, "failed", "speakerd is not running")
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write(_line({"id": rid, "method": method, "params": params or {}}))
        if timeout is None:
            timeout = LONG_TIMEOUT_S if method in LONG_METHODS else DEFAULT_TIMEOUT_S
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise ApiError(502, "failed", f"speakerd did not answer {method} in {timeout:.0f}s")
        finally:
            self._pending.pop(rid, None)
