"""Configuration loading: TOML -> frozen dataclasses.

Single source of truth for MQTT credentials and the static device registry.
MQTT is optional: without an [mqtt] section (or with enabled = false) the node
runs without a broker. The amp is optional too: a fresh node starts with none
and gets one paired over the node API (roster.py keeps the live amp).
MACs are canonicalized to uppercase colon form; the D-Bus underscore
form is always derived, never stored (the old scripts' mixed formats
were a live bug).
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
_SLUG_RE = re.compile(r"^[a-z0-9_]+$")

AMP_SLUG = "amp"
PLACEHOLDER_MAC = "00:00:00:00:00:00"


class ConfigError(Exception):
    pass


def _slugify(raw: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", raw.lower())).strip("_")


def _canon_mac(raw: str, where: str) -> str:
    mac = raw.strip().upper().replace("-", ":").replace("_", ":")
    if not _MAC_RE.match(mac):
        raise ConfigError(f"{where}: invalid MAC address {raw!r}")
    return mac


@dataclass(frozen=True)
class Device:
    name: str
    slug: str
    mac: str  # canonical AA:BB:CC:DD:EE:FF

    @property
    def dbus_mac(self) -> str:
        return self.mac.replace(":", "_")


@dataclass(frozen=True)
class Config:
    # [node] — identity of this speakerd instance (one node per room)
    node_id: str
    node_name: str
    node_area: str | None
    # [mqtt] — optional; mqtt_enabled False = standalone, the rest unused
    mqtt_enabled: bool
    host: str | None
    port: int
    username: str | None
    password: str | None
    client_id: str
    base_topic: str
    discovery_prefix: str
    announce_prefix: str
    # [bluetooth]
    adapter: str
    # the amp from config.toml; None = none configured (the live amp is in
    # roster.Roster, which a pair or forget over the API changes)
    amp: Device | None
    amp_name: str
    fix_metadata_delay_s: float
    streaming_debounce_s: float
    amp_reconnect_debounce_s: float
    amp_reconnect_retry_delay_s: float
    amp_reconnect_tries: int
    # export now-playing metadata to the amp via BlueZ Media1 (one permanent
    # MPRIS player, properties-only updates). Replaces
    # mpris-proxy: its bridge unit must be disabled when this is on.
    amp_metadata_export: bool
    # optional append-only forensic log of everything crossing the export
    # (btwatch-style timestamps, unfiltered); None disables it
    amp_export_raw_log: str | None
    # [[devices]]
    ios_devices: tuple[Device, ...]
    # [airplay]
    airplay_enabled: bool
    # [system]
    power_commands: bool
    state_file: str
    # [control] — the control socket the node API service talks to
    # (control.py; the API itself runs as its own account, apiserver.py)
    control_enabled: bool
    control_socket: str
    shairport_conf: str

    by_slug: dict[str, Device] = field(default_factory=dict)
    by_mac: dict[str, Device] = field(default_factory=dict)

    def __post_init__(self):
        for dev in self.all_devices:
            self.by_slug[dev.slug] = dev
            self.by_mac[dev.mac] = dev

    @property
    def all_devices(self) -> tuple[Device, ...]:
        return self.ios_devices + ((self.amp,) if self.amp else ())

    @property
    def ios_macs(self) -> set[str]:
        return {d.mac for d in self.ios_devices}

    def topic(self, *parts: str) -> str:
        return "/".join((self.base_topic, *parts))

    @property
    def device_id(self) -> str:
        """HA device identifier — derived, never configurable: HA keys the
        existing entity registry on it, so it must not drift with a rename."""
        return f"{self.node_id}_pi"

    @property
    def announce_topic(self) -> str:
        """Retained node announcement — how HA finds nodes without hand-entry."""
        return f"{self.announce_prefix}/{self.node_id}"

    @property
    def availability_topic(self) -> str:
        return self.topic("availability")

    @property
    def airplay_topic(self) -> str:
        return self.topic("airplay")


def load(path: str) -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)

    try:
        b = raw["bluetooth"]
    except KeyError as e:
        raise ConfigError(f"missing required section {e}") from None
    m = raw.get("mqtt", {})
    # standalone = no [mqtt] section, or enabled = false. A present section
    # with its keys missing is an edit mistake and must fail loudly.
    mqtt_enabled = "mqtt" in raw and bool(m.get("enabled", True))
    if mqtt_enabled:
        missing = [k for k in ("host", "username", "password") if k not in m]
        if missing:
            raise ConfigError(f"[mqtt] is missing {', '.join(missing)} "
                              "(or set enabled = false)")

    devices = []
    seen_slugs, seen_macs = set(), set()
    for i, d in enumerate(raw.get("devices", [])):
        where = f"devices[{i}]"
        try:
            dev = Device(
                name=str(d["name"]),
                slug=str(d["slug"]),
                mac=_canon_mac(str(d["mac"]), where),
            )
        except KeyError as e:
            raise ConfigError(f"{where}: missing key {e}") from None
        if not _SLUG_RE.match(dev.slug):
            raise ConfigError(f"{where}: slug must be [a-z0-9_], got {dev.slug!r}")
        if dev.slug == AMP_SLUG:
            raise ConfigError(f"{where}: slug '{AMP_SLUG}' is reserved for the amplifier")
        if dev.slug in seen_slugs or dev.mac in seen_macs:
            raise ConfigError(f"{where}: duplicate slug or MAC")
        seen_slugs.add(dev.slug)
        seen_macs.add(dev.mac)
        devices.append(dev)

    amp_name = str(b.get("amp_name", "Kohler Amplifier"))
    amp = None
    if b.get("amp_mac"):
        mac = _canon_mac(str(b["amp_mac"]), "bluetooth.amp_mac")
        if mac != PLACEHOLDER_MAC:
            amp = Device(name=amp_name, slug=AMP_SLUG, mac=mac)
    if amp and amp.mac in seen_macs:
        raise ConfigError("amp_mac duplicates an iOS device MAC")

    tries = int(b.get("amp_reconnect_tries", 3))
    if tries < 1:
        raise ConfigError("amp_reconnect_tries must be >= 1")

    s = raw.get("system", {})
    a = raw.get("control", {})

    def path(table: dict, key: str, default: str) -> str:
        return os.path.expanduser(str(table.get(key, default)))

    n = raw.get("node", {})
    base_topic = str(m.get("base_topic", "nowairplaying")).rstrip("/")
    # the default derives from base_topic so an existing single-node install keeps
    # its node id, device id and entity unique_ids without touching its config.
    # base_topic may legally hold '/' and capitals, which a node id may not, so
    # the DERIVED id is sanitized — only an explicit node.id is an error.
    if "id" in n:
        node_id = str(n["id"])
        if not _SLUG_RE.match(node_id):
            raise ConfigError(f"node.id must be [a-z0-9_], got {node_id!r}")
    else:
        node_id = _slugify(base_topic)
        if not node_id:
            raise ConfigError(
                f"cannot derive a node id from base_topic {base_topic!r} — set node.id")
    area = n.get("area")

    return Config(
        node_id=node_id,
        node_name=str(n.get("name", node_id.replace("_", " ").title())),
        node_area=str(area) if area else None,
        mqtt_enabled=mqtt_enabled,
        host=str(m["host"]) if mqtt_enabled else None,
        port=int(m.get("port", 1883)),
        username=str(m["username"]) if mqtt_enabled else None,
        password=str(m["password"]) if mqtt_enabled else None,
        client_id=str(m.get("client_id", "speakerd")),
        base_topic=base_topic,
        discovery_prefix=str(m.get("discovery_prefix", "homeassistant")).rstrip("/"),
        announce_prefix=str(m.get("announce_prefix", "speakerd/nodes")).strip("/"),
        adapter=str(b.get("adapter", "hci0")),
        amp=amp,
        amp_name=amp_name,
        fix_metadata_delay_s=float(b.get("fix_metadata_delay_s", 3)),
        streaming_debounce_s=float(b.get("streaming_debounce_s", 2)),
        amp_reconnect_debounce_s=float(b.get("amp_reconnect_debounce_s", 5)),
        amp_reconnect_retry_delay_s=float(b.get("amp_reconnect_retry_delay_s", 10)),
        amp_reconnect_tries=tries,
        amp_metadata_export=bool(b.get("amp_metadata_export", False)),
        amp_export_raw_log=(os.path.expanduser(str(b["amp_export_raw_log"]))
                            if b.get("amp_export_raw_log") else None),
        ios_devices=tuple(devices),
        airplay_enabled=bool(raw.get("airplay", {}).get("enabled", False)),
        power_commands=bool(s.get("power_commands", False)),
        state_file=path(s, "state_file", "~/.local/state/speakerd/state.json"),
        control_enabled=bool(a.get("enabled", False)),
        control_socket=path(a, "socket", "/run/nowairplaying/speakerd.sock"),
        shairport_conf=path(a, "shairport_conf", "~/.config/shairport-sync.conf"),
    )
