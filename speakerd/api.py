"""The node API's HTTP layer (docs/SETUP-API.md): two listeners, auth, routes.

Runs in the API service (apiserver.py, account nowairplaying-api). Audio
commands are forwarded to speakerd over the control socket.

- HTTPS on https_port with the install's self-signed certificate, which Home
  Assistant pins by fingerprint. Every call that carries a token comes here.
- HTTP on http_port for the setup page, which never handles a token: a
  request with an Authorization header over HTTP is refused.

Auth levels, per route:
  none    anyone, over HTTP or HTTPS
  owner   unclaimed: anyone; claimed: the token, over HTTPS only
  token   claimed only, with the token, over HTTPS only
  open    anyone, even on a claimed node (amp connect/disconnect, the
          audio restart)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl

from aiohttp import web

from .errors import ApiError
from .page import PAGE

log = logging.getLogger("speakerd.api")

NONE, OWNER, TOKEN, OPEN = "none", "owner", "token", "open"
PING_S = 15
MAX_BODY = 16 * 1024


def _bearer(request: web.Request) -> str | None:
    h = request.headers.get("Authorization", "")
    if h.lower().startswith("bearer "):
        return h[7:].strip()
    return None


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": code, "message": message}, status=status)


def _ok(status: int = 200) -> web.Response:
    return web.json_response({"ok": True}, status=status)


class Api:
    def __init__(self, node):
        self.node = node
        self.cfg = node.cfg
        self._runner: web.AppRunner | None = None
        self.app = self._build()

    # ------------------------------------------------------------- wiring

    def _build(self) -> web.Application:
        app = web.Application(middlewares=[self._errors], client_max_size=MAX_BODY)
        r = app.router
        p = "/api/v2"
        routes = [
            ("GET", "/info", NONE, self.info),
            ("GET", "/verify", NONE, self.verify),
            ("GET", "/state", OWNER, self.state),
            ("GET", "/events", OWNER, self.events),
            ("POST", "/claim", NONE, self.claim),
            ("POST", "/release", TOKEN, self.release),
            ("POST", "/amp/scan", OWNER, self.amp_scan),
            ("GET", "/amp/found", OWNER, self.amp_found),
            ("POST", "/amp/pair", OWNER, self.amp_pair),
            ("POST", "/amp/connect", OPEN, self.amp_connect),
            ("POST", "/amp/disconnect", OPEN, self.amp_disconnect),
            ("POST", "/amp/reconnect", OWNER, self.amp_reconnect),
            ("PUT", "/amp/auto-reconnect", OWNER, self.amp_auto_reconnect),
            ("POST", "/amp/forget", OWNER, self.amp_forget),
            ("POST", "/audio/restart", OPEN, self.audio_restart),
            ("POST", "/phones/pairing", OWNER, self.phones_pairing),
            ("POST", "/phones/{mac}/connect", OWNER, self.phone_connect),
            ("POST", "/phones/{mac}/disconnect", OWNER, self.phone_disconnect),
            ("DELETE", "/phones/{mac}", OWNER, self.phone_forget),
            ("POST", "/media/{source}/{action}", OWNER, self.media),
            ("PUT", "/node/name", TOKEN, self.node_name),
            ("POST", "/node/reboot", TOKEN, self.node_reboot),
            ("POST", "/node/shutdown", TOKEN, self.node_shutdown),
            ("POST", "/node/update", TOKEN, self.node_update),
        ]
        for method, path, level, handler in routes:
            r.add_route(method, p + path, self._guard(level, handler))
        r.add_get("/", self.page)
        return app

    def _guard(self, level: str, handler):
        async def wrapped(request: web.Request):
            self._authorize(request, level)
            return await handler(request)
        wrapped.__name__ = handler.__name__
        return wrapped

    def _authorize(self, request: web.Request, level: str) -> None:
        if "Authorization" in request.headers and not request.secure:
            log.warning("refused a token sent over plain HTTP from %s", request.remote)
            raise ApiError(403, "https_required", "a token is only accepted over HTTPS")
        if level in (NONE, OPEN):
            return
        claimed = self.node.claims.claimed
        if level == TOKEN and not claimed:
            raise ApiError(403, "claim_required", "only a node claimed by Home Assistant does this")
        if claimed:
            if not request.secure:
                raise ApiError(401, "unauthorized", "this node is claimed: use HTTPS and its token")
            if not self.node.claims.token_ok(_bearer(request)):
                raise ApiError(401, "unauthorized", "token missing or wrong")

    def _allowed(self, request: web.Request, level: str) -> bool:
        try:
            self._authorize(request, level)
        except ApiError:
            return False
        return True

    @web.middleware
    async def _errors(self, request: web.Request, handler):
        try:
            return await handler(request)
        except ApiError as e:
            return _error(e.status, e.code, e.message)
        except web.HTTPNotFound:
            return _error(404, "not_found", "no such endpoint")
        except web.HTTPMethodNotAllowed:
            return _error(404, "not_found", "no such endpoint for this method")
        except web.HTTPRequestEntityTooLarge:
            return _error(400, "bad_request", "body too large")
        except web.HTTPException:
            raise
        except Exception:
            log.exception("API %s %s failed", request.method, request.path)
            return _error(502, "failed", "internal error, see the node's journal")

    async def _body(self, request: web.Request) -> dict:
        if not request.can_read_body:
            return {}
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "bad_request", "the body is not JSON")
        if not isinstance(body, dict):
            raise ApiError(400, "bad_request", "the body must be a JSON object")
        return body

    # ------------------------------------------------------------- lifecycle

    def _ssl_context(self) -> ssl.SSLContext | None:
        cert = os.path.join(self.cfg.tls_dir, "cert.pem")
        key = os.path.join(self.cfg.tls_dir, "key.pem")
        try:
            ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ctx.load_cert_chain(cert, key)
            return ctx
        except (OSError, ssl.SSLError) as e:
            log.error("no HTTPS: cannot load %s / %s (%s). Home Assistant cannot "
                      "reach this node until the install makes them.", cert, key, e)
            return None

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None, handle_signals=False)
        await self._runner.setup()
        ctx = self._ssl_context()
        if ctx is not None:
            await web.TCPSite(self._runner, None, self.cfg.https_port, ssl_context=ctx).start()
            log.info("node API on https port %d", self.cfg.https_port)
        await web.TCPSite(self._runner, None, self.cfg.http_port).start()
        log.info("setup page on http port %d", self.cfg.http_port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------- handlers

    async def page(self, request):
        return web.Response(text=PAGE, content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def info(self, request):
        return web.json_response(self.node.info())

    async def verify(self, request):
        return web.json_response(await self.node.verify())

    async def state(self, request):
        _seq, state = self.node.hub.snapshot()
        return web.json_response(state)

    async def events(self, request: web.Request):
        hub = self.node.hub
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                           "Cache-Control": "no-cache",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        q = hub.subscribe()
        try:
            # a release or re-claim while the headers went out closed every
            # stream but missed this one, which wasn't subscribed yet
            if not self._allowed(request, OWNER):
                return resp
            seq, state = hub.snapshot()
            await resp.write(_sse(seq, "state", state))
            while True:
                try:
                    item = await asyncio.wait_for(q.get(), PING_S)
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
                    continue
                if item is None:
                    break  # closed: released, too far behind, or shutting down
                seq, kind, data = item
                await resp.write(_sse(seq, kind, data))
        except ConnectionResetError:
            pass  # the client went away
        finally:
            hub.unsubscribe(q)
        return resp

    async def claim(self, request):
        if not request.secure:
            raise ApiError(403, "https_required", "claim over HTTPS")
        return web.json_response(await self.node.claim(_bearer(request),
                                                       await self._body(request)))

    async def release(self, request):
        await self.node.release()
        return _ok()

    async def _audio(self, method: str, params: dict | None = None, status: int = 200):
        await self.node.audio_call(method, params)
        return _ok(status)

    async def amp_scan(self, request):
        body = await self._body(request)
        return await self._audio("amp.scan", {"seconds": body.get("seconds", 20)}, 202)

    async def amp_found(self, request):
        return web.json_response(await self.node.audio_call("amp.found"))

    async def amp_pair(self, request):
        body = await self._body(request)
        return await self._audio("amp.pair", {"mac": body.get("mac")})

    async def _amp(self, action: str):
        return await self._audio("amp.command", {"action": action})

    async def amp_connect(self, request):
        return await self._amp("connect")

    async def amp_disconnect(self, request):
        return await self._amp("disconnect")

    async def amp_reconnect(self, request):
        return await self._amp("reconnect")

    async def amp_auto_reconnect(self, request):
        body = await self._body(request)
        return await self._audio("amp.auto_reconnect", {"on": body.get("on")})

    async def amp_forget(self, request):
        return await self._audio("amp.forget")

    async def audio_restart(self, request):
        return await self._audio("audio.restart", status=202)

    async def phones_pairing(self, request):
        return await self._audio("phones.pairing", await self._body(request))

    async def phone_connect(self, request):
        return await self._audio("phones.command", {"mac": request.match_info["mac"],
                                                    "action": "connect"})

    async def phone_disconnect(self, request):
        return await self._audio("phones.command", {"mac": request.match_info["mac"],
                                                    "action": "disconnect"})

    async def phone_forget(self, request):
        return await self._audio("phones.forget", {"mac": request.match_info["mac"]})

    async def media(self, request):
        return await self._audio("media", {"source": request.match_info["source"],
                                           "action": request.match_info["action"]})

    async def node_name(self, request):
        body = await self._body(request)
        await self.node.rename(body.get("name"))
        return _ok()

    async def node_reboot(self, request):
        self.node.power("reboot")
        return _ok(202)

    async def node_shutdown(self, request):
        self.node.power("shutdown")
        return _ok(202)

    async def node_update(self, request):
        await self.node.update(await self._body(request))
        return _ok(202)


def _sse(seq: int, event: str, data) -> bytes:
    payload = json.dumps(data, separators=(",", ":"))
    return f"id: {seq}\nevent: {event}\ndata: {payload}\n\n".encode()
