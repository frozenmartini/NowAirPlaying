"""The node API service: `python3 -m speakerd.apiserver`.

Runs as the system account `nowairplaying-api`, the only account with polkit
grants (the update unit, reboot, power-off). The audio stack runs as
`nowairplaying` with none, so a bug in anything that listens to the LAN for
audio (shairport-sync) can't reach those (docs/SETUP-API.md#privileges).

This process owns the claim and its token, HTTPS and the setup page (api.py),
the zeroconf record, updates, power and the system-wide checks. Everything
Bluetooth and AirPlay is speakerd's, reached over the control socket
(control.py); speakerd pushes its audio state, and this process adds its own
keys to make the state object of docs/SETUP-API.md.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
import unicodedata
from dataclasses import dataclass

from .control import AudioClient
from .errors import ApiError, now_iso, valid_name
from .netman import NmClient
from .system import (INSTALL_UNIT, UPDATE_UNIT, System, merge_checks, power,
                     session_unavailable, version_key)
from .zeroconf import Zeroconf

log = logging.getLogger("speakerd.apiserver")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
# planted_hash() when a claim-token file is there but unusable: it locks /claim
INVALID_PLANT = "invalid"
NETWORK_POLL_S = 30
UPDATE_POLL_S = 2
UPDATE_PENDING_S = 15  # how long a just-started update may take to show as active
POWER_DELAY_S = 2
SSE_QUEUE = 256
NO_AUDIO_RESTART = {"running": False, "last_result": None}
NO_PHONES = {"pairing": {"open": False, "until": None, "last_paired": None}, "devices": []}
IDLE_PLAYING = {"airplay": {"status": "idle", "title": None, "artist": None, "album": None,
                            "client": None},
                "bluetooth": {"status": "idle", "title": None, "artist": None,
                              "album": None, "duration": None, "position": None,
                              "device": None}}


@dataclass
class ApiConfig:
    https_port: int = 8443
    http_port: int = 8080
    socket: str = "/run/nowairplaying/speakerd.sock"
    state_dir: str = "/var/lib/nowairplaying-api"        # api.json, reset-request
    tls_dir: str = "/var/lib/nowairplaying/tls"
    claim_dir: str = "/var/lib/nowairplaying/claim"
    update_dir: str = "/var/lib/nowairplaying/update"
    install_json: str = "/var/lib/nowairplaying/install.json"
    install_args: str = "/var/lib/nowairplaying/install-args"

    @property
    def api_state_file(self) -> str:
        return os.path.join(self.state_dir, "api.json")

    @property
    def reset_request(self) -> str:
        return os.path.join(self.state_dir, "reset-request")


# ----------------------------------------------------------------- the claim

def _has_control(text: str) -> bool:
    """C0 and C1 controls, DEL, and the Unicode line and paragraph
    separators: anything that can break a line where these get shown."""
    return any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in text)


def hash_ok(token: str | None, expected_hex: str) -> bool:
    """Bearer tokens are the hex of 32 raw bytes; stored hashes are the
    SHA-256 of those raw bytes."""
    if not isinstance(token, str) or not _HEX64.match(token):
        return False
    digest = hashlib.sha256(bytes.fromhex(token)).hexdigest()
    return hmac.compare_digest(digest, expected_hex)


class ClaimStore:
    """api.json: the claim (the token's SHA-256, never the token) and the
    planted claim-token the install put in claim_dir."""

    def __init__(self, path: str, claim_dir: str):
        self._path = path
        self._planted = os.path.join(claim_dir, "claim-token")
        self._data: dict = {}
        self.load()

    def load(self) -> None:
        try:
            with open(self._path, "rb") as f:
                data = json.load(f)
            self._data = data if isinstance(data, dict) else {}
        except FileNotFoundError:
            self._data = {}
        except (OSError, ValueError) as e:
            log.error("claim file %s unreadable (%s) — treating the node as unclaimed",
                      self._path, e)
            self._data = {}

    @property
    def claimed(self) -> bool:
        return bool(self._data.get("token_sha256"))

    @property
    def claimed_by(self) -> str | None:
        return self._data.get("claimed_by")

    @property
    def area(self) -> str | None:
        return self._data.get("area")

    def planted_hash(self) -> str | None:
        """The planted hash, read fresh on every call (never cached). None
        only when no file is there at all. A file that can't be read (say, a
        mode 0600 one the login user planted straight into claim/) or isn't a
        hash gives INVALID_PLANT, which no token matches: it fails closed."""
        try:
            with open(self._planted, encoding="ascii") as f:
                h = f.read().strip().lower()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as e:
            log.error("planted claim token %s is unreadable: %s", self._planted, e)
            return INVALID_PLANT
        if not _HEX64.match(h):
            log.error("planted claim token %s is not 64 hex characters", self._planted)
            return INVALID_PLANT
        return h

    def token_ok(self, token: str | None) -> bool:
        return self.claimed and hash_ok(token, self._data["token_sha256"])

    def claim(self, claimed_by: str | None, area: str | None) -> str:
        # the plant goes first: one left behind would be a standing key that
        # re-claims the node at any time, so no claim without removing it
        try:
            os.remove(self._planted)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.error("could not delete the used claim-token: %s", e)
            raise ApiError(502, "failed", "could not delete the used claim token; "
                           "nothing was claimed")
        raw = secrets.token_bytes(32)
        self._write({"token_sha256": hashlib.sha256(raw).hexdigest(),
                     "claimed_by": claimed_by, "area": area, "claimed_at": now_iso()})
        return raw.hex()

    def release(self) -> None:
        self._write({})

    def _write(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", mode=0o700, exist_ok=True)
        tmp = f"{self._path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        self._data = data


# ----------------------------------------------------------------- state push

class StateHub:
    """The state object and its SSE subscribers. changed() coalesces
    everything that happens in one loop iteration into one `change` event."""

    def __init__(self, build):
        self._build = build
        self._last: dict = {}
        self.seq = 0
        self._subs: set[asyncio.Queue] = set()
        self._scheduled = False

    def changed(self) -> None:
        if self._scheduled:
            return
        self._scheduled = True
        try:
            asyncio.get_running_loop().call_soon(self.flush)
        except RuntimeError:  # no loop yet; the first read builds
            self._scheduled = False

    def flush(self) -> None:
        self._scheduled = False
        new = self._build()
        diff = {k: v for k, v in new.items() if self._last.get(k) != v}
        if not diff and self._last:
            return
        self._last = new
        self.seq += 1
        for q in list(self._subs):
            try:
                q.put_nowait((self.seq, "change", diff))
            except asyncio.QueueFull:
                # a reader this far behind gets closed; it reconnects and
                # starts again from a full state
                self._subs.discard(q)
                while not q.empty():
                    q.get_nowait()
                q.put_nowait(None)

    def snapshot(self) -> tuple[int, dict]:
        self.flush()
        return self.seq, self._last

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SSE_QUEUE)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def close_all(self) -> None:
        subs, self._subs = self._subs, set()
        for q in subs:
            if q.full():
                q.get_nowait()  # make room for the end, as publishing does
            q.put_nowait(None)


# ----------------------------------------------------------------- the service

class ApiNode:
    def __init__(self, cfg: ApiConfig):
        self.cfg = cfg
        self.claims = ClaimStore(cfg.api_state_file, cfg.claim_dir)
        self.system = System(cfg.install_json, cfg.update_dir)
        self.zeroconf = Zeroconf()
        self.nm = NmClient()
        self.hub = StateHub(self.build_state)
        self.audio_client = AudioClient(cfg.socket, self._audio_state, self._audio_connected)
        self.audio: dict = {}
        self._version_seen = self.version
        self._published: tuple | None = None
        self._network = {"link": None, "ssid": None, "signal": None, "ip": None}
        self._update = {"state": "idle", "version": None, "phase_name": None,
                        "reason": None, "message": None, "rolled_back": False}
        self._update_pending_until = 0.0
        self._update_requested_at = 0.0  # wall clock, against install.json's mtime
        self._update_lock = asyncio.Lock()  # the busy check awaits: serialize it
        self._update_poll: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if os.path.exists(self.cfg.reset_request):
            log.warning("reset file found on the boot partition — clearing the claim")
            self.claims.release()
            try:
                os.remove(self.cfg.reset_request)
            except OSError as e:
                log.error("could not remove %s: %s", self.cfg.reset_request, e)
        for name, coro in (("zeroconf", self.zeroconf.start()),
                           ("NetworkManager", self.nm.start())):
            try:
                await coro
            except Exception as e:
                log.warning("%s unavailable: %s", name, e)
        self.audio_client.start()
        self._spawn(self._poll_network())
        self._update = await self.system.update_state()
        if self._update["state"] == "running":
            self._ensure_update_poll()
        self.hub.changed()

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self.hub.close_all()
        await self.audio_client.stop()
        await self.zeroconf.stop()
        await self.nm.stop()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # ------------------------------------------------------------- speakerd

    def _audio_state(self, state: dict | None) -> None:
        self.audio = state or {}
        self.hub.changed()
        self._spawn(self._publish_zeroconf())

    async def _audio_connected(self) -> None:
        # speakerd keeps its MQTT discovery off while claimed (it remembers
        # the last setting across restarts; this makes sure it's current)
        await self.audio_client.call("mqtt.discovery", {"enabled": not self.claims.claimed})

    async def audio_call(self, method: str, params: dict | None = None):
        return await self.audio_client.call(method, params)

    # ------------------------------------------------------------- the state

    @property
    def adapter_mac(self) -> str | None:
        return self.audio.get("adapter_mac")

    @property
    def name(self) -> str:
        return self.audio.get("name") or "NowAirPlaying"

    @property
    def node_id(self) -> str:
        mac = (self.adapter_mac or "00:00:00:00:00:00").replace(":", "").lower()
        return f"nowairplaying_{mac[-6:]}"

    def build_state(self) -> dict:
        a = self.audio
        return {
            "node": {"name": self.name, "version": self.version, "update": dict(self._update),
                     "audio_restart": a.get("audio_restart") or NO_AUDIO_RESTART},
            "network": dict(self._network),
            "amp": a.get("amp"),
            "phones": a.get("phones") or NO_PHONES,
            "source": a.get("source") or "idle",
            "bluetooth_streaming": bool(a.get("bluetooth_streaming")),
            "now_playing": a.get("now_playing") or IDLE_PLAYING,
        }

    @property
    def version(self) -> str:
        """The installed release, read live from install.json."""
        return self.system.installed_version()

    def info(self) -> dict:
        amp = self.audio.get("amp")
        return {
            "api": 2,
            "version": self.version,
            "id": self.node_id,
            "name": self.name,
            "mac": self.adapter_mac,
            "state": "claimed" if self.claims.claimed else "unclaimed",
            "claim": "token" if self.claims.planted_hash() else "open",
            "claimed_by": self.claims.claimed_by,
            "area": self.claims.area,
            "phones": self.phones_setting,
            "amp": None if amp is None else {k: amp[k] for k in
                                             ("mac", "name", "paired", "connected", "audio")
                                             if k in amp},
        }

    @property
    def phones_setting(self) -> str:
        """--phones as the install recorded it (install-args, KEY=value)."""
        try:
            with open(self.cfg.install_args, encoding="utf-8") as f:
                for line in f:
                    key, _, value = line.strip().partition("=")
                    if key == "PHONES" and value in ("onboard", "dongle"):
                        return value
        except OSError:
            pass
        return "onboard"

    async def verify(self) -> dict:
        system = await self.system.checks()
        try:
            session = await self.audio_call("verify")
        except ApiError as e:
            session = session_unavailable(e.message)
        checks = merge_checks(system, session)
        return {"ok": all(c["ok"] for c in checks), "checks": checks}

    async def _publish_zeroconf(self) -> None:
        if self.adapter_mac is None:
            return
        key = (self.name, self.adapter_mac, self.claims.claimed, self.version)
        if key == self._published:
            return
        self._published = key
        await self.zeroconf.publish(self.name, self.cfg.https_port, {
            "api": "2", "ver": self.version, "mac": self.adapter_mac, "id": self.node_id,
            "state": "claimed" if self.claims.claimed else "unclaimed"})

    # ------------------------------------------------------------- claim

    async def claim(self, bearer: str | None, body: dict) -> dict:
        """The first claim, or a re-claim: on a claimed node, a matching
        planted token replaces the claim. Planting one needs an SSH login in
        group nowairplaying-api, the same proof of ownership Reconnect relies
        on. It frees a node whose claim answer was lost, and lets a rebuilt
        Home Assistant take its node back (docs/SETUP-API.md)."""
        planted = self.claims.planted_hash()
        reclaim = self.claims.claimed
        if reclaim and planted is None:
            raise ApiError(409, "already_claimed", "the node is already claimed")
        if planted == INVALID_PLANT:
            raise ApiError(409, "claim_token_invalid",
                           "the planted claim token can't be read: it must be 64 hex characters, "
                           "readable by nowairplaying-api (mode 0640 in claim/)")
        if planted is not None and not hash_ok(bearer, planted):
            raise ApiError(401, "unauthorized", "this node expects its planted claim token")
        name, area, claimed_by = body.get("name"), body.get("area"), body.get("claimed_by")
        if name is not None:
            name = valid_name(name)
        for k, v in (("area", area), ("claimed_by", claimed_by)):
            if v is not None and (not isinstance(v, str) or len(v) > 255
                                  or _has_control(v)):
                raise ApiError(400, "bad_request",
                               f"{k}: at most 255 characters, no control characters")
        previous = self.claims.claimed_by
        token = self.claims.claim(claimed_by, area)
        if reclaim:
            log.warning("re-claimed with a planted token by %s, replacing %s",
                        claimed_by or "an unnamed client", previous or "an unnamed client")
            self.hub.close_all()  # streams opened under the old token end here
        else:
            log.warning("claimed by %s", claimed_by or "an unnamed client")
        calls = [("mqtt.discovery", {"enabled": False})]
        if name and name != self.name:
            calls.insert(0, ("node.rename", {"name": name}))
        for method, params in calls:
            try:
                await self.audio_call(method, params)
            except ApiError as e:
                # the claim stands; speakerd catches up when it reconnects
                log.warning("after the claim, %s: %s", method, e.message)
        await self._publish_zeroconf()
        self.hub.changed()
        return {"token": token}

    async def release(self) -> None:
        self.claims.release()
        log.warning("claim released")
        try:
            await self.audio_call("mqtt.discovery", {"enabled": True})
        except ApiError as e:
            log.warning("after the release, mqtt.discovery: %s", e.message)
        await self._publish_zeroconf()
        self.hub.close_all()  # streams opened under the claim end here
        self.hub.changed()

    async def rename(self, name) -> None:
        await self.audio_call("node.rename", {"name": valid_name(name)})

    # ------------------------------------------------------------- power

    def power(self, action: str) -> None:
        async def later():
            await asyncio.sleep(POWER_DELAY_S)  # let the 202 reach the client
            ok, err = await power(action)
            if not ok:
                log.error("%s failed: %s", action, err)
        log.warning("%s requested over the API", action)
        self._spawn(later())

    # ------------------------------------------------------------- update

    async def update(self, body: dict) -> None:
        version, sha = body.get("version"), body.get("sha256")
        err = self.system.check_update_request(version, sha)
        if err:
            raise ApiError(400, "bad_request", err)
        async with self._update_lock:
            await self._start_update(version, sha)

    async def _start_update(self, version: str, sha: str) -> None:
        loop = asyncio.get_running_loop()
        if (loop.time() < self._update_pending_until
                or await self.system.unit_active(UPDATE_UNIT)
                or await self.system.unit_active(INSTALL_UNIT)):
            raise ApiError(409, "busy", "an install or update is already running")
        installed = self.system.installed_version()
        if version_key(version) < version_key(installed):
            raise ApiError(409, "downgrade", f"{version} is older than the installed {installed}")
        requested_at = time.time()
        ok, msg = await self.system.start_update(version, sha)
        if not ok:
            raise ApiError(502, "failed", msg)
        log.warning("update to %s started", version)
        self._update_pending_until = loop.time() + UPDATE_PENDING_S
        self._update_requested_at = requested_at
        self._update = {"state": "running", "version": version, "phase_name": "download",
                        "reason": None, "message": None, "rolled_back": False}
        self._ensure_update_poll()
        self.hub.changed()

    def _ensure_update_poll(self) -> None:
        if self._update_poll is None or self._update_poll.done():
            self._update_poll = self._spawn(self._poll_update())

    async def _poll_update(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(UPDATE_POLL_S)
            state = await self.system.update_state()
            if (state["state"] != "running" and loop.time() < self._update_pending_until
                    and not self.system.install_written_since(self._update_requested_at)):
                continue  # not started yet: install.json is still the one from before
            if state != self._update:
                self._update = state
                self.hub.changed()
            if state["state"] != "running":
                return

    # ------------------------------------------------------------- network

    async def _poll_network(self) -> None:
        while True:
            try:
                net = await self.nm.state()
            except Exception as e:
                log.debug("network state: %s", e)
            else:
                if net != self._network:
                    self._network = net
                    self.hub.changed()
            # on a first install this service starts before install.json says
            # "done", so the version it reports catches up here
            if self.version != self._version_seen:
                self._version_seen = self.version
                self.hub.changed()
                await self._publish_zeroconf()
            await asyncio.sleep(NETWORK_POLL_S)


# ----------------------------------------------------------------- main

async def _amain(cfg: ApiConfig) -> int:
    from .api import Api  # aiohttp
    node = ApiNode(cfg)
    api = Api(node)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await node.start()
    try:
        await api.start()
    except OSError as e:
        log.error("node API could not listen: %s", e)
        await node.stop()
        return 1
    await stop.wait()
    log.info("shutting down")
    await api.stop()
    await node.stop()
    return 0


def main() -> None:
    d = ApiConfig()
    p = argparse.ArgumentParser(prog="speakerd.apiserver")
    p.add_argument("--https-port", type=int, default=d.https_port)
    p.add_argument("--http-port", type=int, default=d.http_port)
    for name in ("socket", "state_dir", "tls_dir", "claim_dir", "update_dir",
                 "install_json", "install_args"):
        p.add_argument("--" + name.replace("_", "-"), default=getattr(d, name))
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(levelname)s %(name)s: %(message)s")
    cfg = ApiConfig(**{k: v for k, v in vars(args).items() if k != "log_level"})
    sys.exit(asyncio.run(_amain(cfg)))


if __name__ == "__main__":
    main()
