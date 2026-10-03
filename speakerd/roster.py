"""The live device roster and the node state that survives restarts.

config.toml is written once by the installer; everything the node API changes
at run time lives here instead, in the state file (state.json):

    {"amp_auto_reconnect": true,
     "amp": {"mac": "F4:4E:FD:00:00:00", "name": "Kohler Amplifier"},  # or null
     "name": "Bathroom Speaker"}                                       # or absent

An "amp" key, even null after a forget, overrides config.toml's amp_mac; with
no key the config's amp stands. Phones are not stored: BlueZ's own bond list is
the source of truth for them (bluez.py).
"""
from __future__ import annotations

import json
import logging
import os

from .config import AMP_SLUG, Config, Device

log = logging.getLogger("speakerd.roster")


def load_state(path: str) -> dict:
    """The state file as a dict; {} when missing or unreadable."""
    try:
        with open(path, "rb") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"expected a JSON object, got {type(data).__name__}")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        log.warning("state file %s unreadable (%s) — starting from defaults", path, e)
        return {}


def update_state(path: str, **changes) -> None:
    """Merge `changes` into the state file, atomically and durably."""
    data = load_state(path)
    data.update(changes)
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())  # survive a power cut right after the change
        os.replace(tmp, path)
    except OSError as e:
        log.warning("could not persist state to %s: %s", path, e)


class Roster:
    """The amp (changeable at run time) plus config.toml's [[devices]].

    `by_mac` / `by_slug` cover exactly the devices with MQTT entities and
    per-device connect state: the static devices and the amp.
    """

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self.static: tuple[Device, ...] = cfg.ios_devices
        state = load_state(cfg.state_file)
        self.amp: Device | None = cfg.amp
        if "amp" in state:
            self.amp = self._amp_from_state(state["amp"])
        self.by_mac: dict[str, Device] = {}
        self.by_slug: dict[str, Device] = {}
        self._rebuild()

    def _amp_from_state(self, raw) -> Device | None:
        if not isinstance(raw, dict) or not isinstance(raw.get("mac"), str):
            return None
        mac = raw["mac"].upper()
        if any(d.mac == mac for d in self.static):
            log.warning("state amp %s is also a [[devices]] entry — ignoring it", mac)
            return None
        return Device(name=str(raw.get("name") or self._cfg.amp_name),
                      slug=AMP_SLUG, mac=mac)

    def _rebuild(self) -> None:
        self.by_mac.clear()
        self.by_slug.clear()
        for dev in self.all_devices:
            self.by_mac[dev.mac] = dev
            self.by_slug[dev.slug] = dev

    @property
    def all_devices(self) -> tuple[Device, ...]:
        return self.static + ((self.amp,) if self.amp else ())

    @property
    def amp_mac(self) -> str | None:
        return self.amp.mac if self.amp else None

    def set_amp(self, mac: str | None, name: str | None = None) -> None:
        """Pair (mac) or forget (None) the amp, and persist it."""
        if mac is None:
            self.amp = None
            update_state(self._cfg.state_file, amp=None)
        else:
            self.amp = Device(name=name or self._cfg.amp_name, slug=AMP_SLUG,
                              mac=mac.upper())
            update_state(self._cfg.state_file,
                         amp={"mac": self.amp.mac, "name": self.amp.name})
        self._rebuild()
