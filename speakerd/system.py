"""Power, updates and the /verify checks.

Two accounts run this code (docs/SETUP-API.md#privileges):
- the API service (`nowairplaying-api`) uses System: power and the update
  unit through `systemctl --no-ask-password`, which asks logind or systemd
  over D-Bus, where polkit allows exactly what 50-nowairplaying.rules grants
  that one account; plus the system-wide checks.
- speakerd (`nowairplaying`, no grants) runs session_checks(): the ones that
  need the audio account's own user session.
merge_checks() puts both halves in the documented order.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

from . import __version__

log = logging.getLogger("speakerd.system")

UPDATE_UNIT = "nowairplaying-update.service"
INSTALL_UNIT = "nowairplaying-install.service"
POLKIT_RULES = "/usr/share/polkit-1/rules.d/50-nowairplaying.rules"
HELD = ("nowairplaying-nqptp", "nowairplaying-shairport-sync")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")  # no trailing newline
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
UNKNOWN_VERSION = "0.0.0"

# GET /verify order (docs/SETUP-API.md#get-verify)
CHECK_ORDER = ("bluez_version", "pipewire_version", "wireplumber_version", "packages_held",
               "shairport_airplay2", "shairport_dbus", "no_mpris", "nqptp_active", "mdns",
               "polkit_rules", "speakerd_running", "amp_paired", "amp_connected",
               "amp_audio", "amp_player", "mqtt_connected")
SESSION_CHECKS = ("pipewire_version", "wireplumber_version", "shairport_airplay2",
                  "shairport_dbus", "no_mpris", "speakerd_running", "amp_paired",
                  "amp_connected", "amp_audio", "amp_player")


async def run(*argv: str, timeout: float = 20) -> tuple[int, str]:
    """(exit status, stdout+stderr). 127 if the program is missing."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except OSError as e:
        return 127, str(e)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        return 124, f"{argv[0]}: timed out after {timeout}s"
    return proc.returncode, out.decode("utf-8", errors="replace").strip()


def version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def check(check_id: str, ok: bool, detail: str) -> dict:
    return {"id": check_id, "ok": bool(ok), "detail": detail}


def merge_checks(*parts: list[dict]) -> list[dict]:
    by_id = {c["id"]: c for part in parts for c in part}
    return [by_id[i] for i in CHECK_ORDER if i in by_id]


async def power(action: str) -> tuple[bool, str | None]:
    verb = {"reboot": "reboot", "shutdown": "poweroff"}[action]
    rc, out = await run("systemctl", "--no-ask-password", verb)
    if rc == 0:
        return True, None
    return False, out or f"systemctl {verb} exited {rc}"


class System:
    """The API service's side: install state, updates, system-wide checks."""

    def __init__(self, install_json: str, update_dir: str):
        self._install_json = install_json
        self._update_dir = update_dir

    # ------------------------------------------------------------- install state

    def install_record(self) -> dict | None:
        try:
            with open(self._install_json, "rb") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def install_written_since(self, t: float) -> bool:
        """install.json was written at or after wall-clock time t: by an
        update requested at t, since every install run writes it."""
        try:
            return os.stat(self._install_json).st_mtime >= t
        except OSError:
            return False

    def installed_version(self) -> str:
        rec = self.install_record() or {}
        # "installed" (from 0.0.4) survives a failed or refused run after it;
        # an older file only has the version of a run that ended done
        v = rec.get("installed")
        if not (isinstance(v, str) and _VERSION_RE.match(v)):
            v = rec.get("version") if rec.get("state") == "done" else None
        if isinstance(v, str) and _VERSION_RE.match(v):
            return v
        # never recorded (an install from before install.json): unknown, which
        # orders below every release, so no update is ever refused as a downgrade
        return UNKNOWN_VERSION

    async def unit_active(self, unit: str) -> bool:
        """Running, in any phase. A Type=oneshot unit (the update) is
        "activating" for its whole run, which `is-active` reports as not
        active."""
        rc, out = await run("systemctl", "show", "-p", "ActiveState", "--value", unit,
                            timeout=5)
        return rc == 0 and out.strip() in ("active", "activating", "deactivating",
                                           "reloading")

    # ------------------------------------------------------------- updates

    @property
    def update_request_path(self) -> str:
        return os.path.join(self._update_dir, "request.json")

    @staticmethod
    def check_update_request(version, sha256) -> str | None:
        """An error text for a malformed request, None when it's well formed."""
        if not isinstance(version, str) or not _VERSION_RE.match(version):
            return "version must be N.N.N"
        if not isinstance(sha256, str) or not _SHA_RE.match(sha256):
            return "sha256 must be 64 lowercase hex characters"
        return None

    async def start_update(self, version: str, sha256: str) -> tuple[bool, str | None]:
        os.makedirs(self._update_dir, mode=0o700, exist_ok=True)
        tmp = self.update_request_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": version, "sha256": sha256}, f)
        os.replace(tmp, self.update_request_path)
        rc, out = await run("systemctl", "--no-ask-password", "--no-block",
                            "start", UPDATE_UNIT)
        if rc == 0:
            return True, None
        return False, out or f"systemctl start {UPDATE_UNIT} exited {rc}"

    async def update_state(self) -> dict:
        """node.update in the state object, from the request, the unit and
        install.json (which the update writes like any install)."""
        idle = {"state": "idle", "version": None, "phase_name": None,
                "reason": None, "message": None, "rolled_back": False}
        try:
            with open(self.update_request_path, "rb") as f:
                req = json.load(f)
        except (OSError, ValueError):
            return idle
        version = req.get("version") if isinstance(req, dict) else None
        rec = self.install_record() or {}
        if await self.unit_active(UPDATE_UNIT):
            state = "running"
        elif rec.get("state") == "done" and rec.get("version") == version:
            state = "done"
        else:
            # failed, or a record that never finished (killed, or a reboot)
            state = "failed"
        return {"state": state, "version": version,
                "phase_name": rec.get("phase_name"),
                "reason": rec.get("reason") if state == "failed" else None,
                "message": rec.get("message") if state == "failed" else None,
                "rolled_back": bool(rec.get("rolled_back")) if state == "failed" else False}

    # ------------------------------------------------------------- checks

    async def checks(self) -> list[dict]:
        return list(await asyncio.gather(
            self._bluez_version(), self._packages_held(),
            self._unit_check("nqptp_active", "nqptp.service"), self._mdns(),
            self._polkit_rules()))

    async def _bluez_version(self) -> dict:
        rc, out = await run("systemctl", "show", "-p", "ExecStart", "--value",
                            "bluetooth.service", timeout=5)
        m = re.search(r"path=(\S+)", out)
        exe = m.group(1) if m else "/usr/libexec/bluetooth/bluetoothd"
        rc, ver = await run(exe, "-v", timeout=5)
        ok = rc == 0 and _at_least(ver, (5, 82))
        return check("bluez_version", ok, f"bluetoothd {ver}" if rc == 0 else ver)

    async def _packages_held(self) -> dict:
        rc, out = await run("apt-mark", "showhold", timeout=10)
        held = set(out.split())
        missing = [p for p in HELD if p not in held]
        return check("packages_held", rc == 0 and not missing,
                     "held" if not missing else "not held: " + ", ".join(missing))

    async def _unit_check(self, check_id: str, unit: str) -> dict:
        ok = await self.unit_active(unit)
        return check(check_id, ok, f"{unit} is {'active' if ok else 'not active'}")

    async def _mdns(self) -> dict:
        active = await self.unit_active("avahi-daemon.service")
        try:
            with open("/etc/nsswitch.conf", encoding="utf-8") as f:
                nss = any(line.startswith("hosts:") and "mdns4_minimal" in line
                          for line in f)
        except OSError:
            nss = False
        detail = ("avahi-daemon is not running" if not active
                  else "nsswitch.conf has no mdns4_minimal" if not nss else "ok")
        return check("mdns", active and nss, detail)

    async def _polkit_rules(self) -> dict:
        try:
            with open(POLKIT_RULES, encoding="utf-8") as f:
                ok = UPDATE_UNIT in f.read()
            detail = "present" if ok else "present but not ours"
        except OSError:
            ok, detail = False, f"{POLKIT_RULES} is missing"
        return check("polkit_rules", ok, detail)


def session_unavailable(reason: str) -> list[dict]:
    """The session checks when speakerd can't be asked: all failed."""
    return [check(i, False, reason) for i in SESSION_CHECKS]


async def session_checks(live: dict) -> list[dict]:
    """speakerd's side. `live` carries what speakerd itself knows:
    shairport_present, amp_configured, amp_paired, amp_connected, amp_audio, amp_player,
    mqtt_enabled, mqtt_connected."""
    out = list(await asyncio.gather(_pipewire_version(), _wireplumber_version(),
                                    _shairport_airplay2(), _no_mpris()))
    out.append(check("shairport_dbus", live.get("shairport_present", False),
                     "owned on the session bus" if live.get("shairport_present")
                     else "org.gnome.ShairportSync is not on the session bus"))
    out.append(check("speakerd_running", True, f"speakerd {__version__}"))
    out.append(check("amp_paired", live.get("amp_paired", False),
                     "paired and trusted" if live.get("amp_paired")
                     else "not paired" if live.get("amp_configured") else "no amp configured"))
    out.append(check("amp_connected", live.get("amp_connected", False),
                     "connected" if live.get("amp_connected") else "not connected"))
    out.append(check("amp_audio", live.get("amp_audio", False),
                     "A2DP audio link up" if live.get("amp_audio")
                     else "connected, but no audio link: AirPlay has nowhere to play"
                     if live.get("amp_connected") else "not connected"))
    out.append(check("amp_player", live.get("amp_player", False),
                     "speakerd's player is registered" if live.get("amp_player")
                     else "no player registered on the amp's adapter"))
    if live.get("mqtt_enabled"):
        out.append(check("mqtt_connected", live.get("mqtt_connected", False),
                         "connected" if live.get("mqtt_connected")
                         else "not connected to the broker"))
    return out


async def _user_unit_active(unit: str) -> bool:
    rc, _ = await run("systemctl", "--user", "is-active", "--quiet", unit, timeout=5)
    return rc == 0


async def _pipewire_version() -> dict:
    active = await _user_unit_active("pipewire.service")
    rc, out = await run("pipewire", "--version", timeout=5)
    m = re.search(r"libpipewire (\d+\.\d+\.\d+)", out)
    ver = m.group(1) if m else None
    ok = active and ver is not None and _at_least(ver, (1, 4))
    return check("pipewire_version", ok, (ver or out) if active
                 else "pipewire.service is not running")


async def _wireplumber_version() -> dict:
    active = await _user_unit_active("wireplumber.service")
    rc, out = await run("wireplumber", "--version", timeout=5)
    m = re.search(r"libwireplumber (\d+\.\d+\.\d+)", out)
    ver = m.group(1) if m else None
    ok = active and ver is not None and _at_least(ver, (0, 5, 8))
    return check("wireplumber_version", ok, (ver or out) if active
                 else "wireplumber.service is not running")


async def _shairport_airplay2() -> dict:
    active = await _user_unit_active("shairport-sync.service")
    rc, out = await run("shairport-sync", "-V", timeout=5)
    ok = active and "-AirPlay2-" in out
    return check("shairport_airplay2", ok, out if active
                 else "shairport-sync.service is not running")


async def _no_mpris() -> dict:
    found = []
    for scope in ("--user", "--system"):
        rc, out = await run("busctl", scope, "--no-legend", "list", timeout=5)
        found += [line.split()[0] for line in out.splitlines()
                  if line.startswith("org.mpris.MediaPlayer2.")]
    rc, _ = await run("pgrep", "-x", "mpris-proxy", timeout=5)
    if rc == 0:
        found.append("mpris-proxy")
    return check("no_mpris", not found, "none" if not found else "found: " + ", ".join(found))


def _at_least(text: str, minimum: tuple[int, ...]) -> bool:
    m = re.search(r"(\d+(?:\.\d+)*)", text)
    if not m:
        return False
    return tuple(int(x) for x in m.group(1).split(".")) >= minimum
