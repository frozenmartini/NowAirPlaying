"""Home Assistant MQTT Discovery payloads.

Everything lives under ONE HA device (the node). Configs are
published retained on every MQTT (re)connect and on HA's birth message.
"""
from __future__ import annotations

import json

from . import __version__
from .config import Config

ANNOUNCE_SCHEMA = 1


def _device_block(cfg: Config) -> dict:
    block = {
        "identifiers": [cfg.device_id],
        "name": cfg.node_name,
        "manufacturer": "Raspberry Pi",
        "model": "Pi 4B BT/AirPlay bridge",
        "sw_version": f"speakerd {__version__}",
    }
    if cfg.node_area:
        # HA honours this only when it first creates the device; a later change
        # never moves a device the user has already placed
        block["suggested_area"] = cfg.node_area
    return block


def build_discovery(cfg: Config) -> list[tuple[str, str]]:
    """Returns [(config_topic, json_payload), ...]."""
    entities: list[tuple[str, str, dict]] = []  # (component, object_id, payload)

    for dev in cfg.ios_devices:
        entities.append(("switch", f"{dev.slug}_connect", {
            "name": dev.name,
            "icon": "mdi:cellphone-wireless",
            "state_topic": cfg.topic("device", dev.slug, "connected"),
            "command_topic": cfg.topic("device", dev.slug, "set"),
            "json_attributes_topic": cfg.topic("device", dev.slug, "result"),
            "optimistic": False,
        }))

    entities.append(("binary_sensor", "amp_connected", {
        "name": "Amplifier Connected",
        "device_class": "connectivity",
        "state_topic": cfg.topic("device", "amp", "connected"),
        "json_attributes_topic": cfg.topic("device", "amp", "result"),
    }))

    entities.append(("button", "fix_metadata", {
        "name": "Fix Metadata (Reconnect Amp)",
        "icon": "mdi:restart",
        "command_topic": cfg.topic("amp", "fix_metadata"),
        "payload_press": "PRESS",
    }))

    entities.append(("switch", "amp_auto_reconnect", {
        "name": f"Auto Reconnect {cfg.amp.name}",
        "icon": "mdi:autorenew",
        "state_topic": cfg.topic("amp", "auto_reconnect"),
        "command_topic": cfg.topic("amp", "auto_reconnect", "set"),
        "optimistic": False,
    }))

    entities.append(("binary_sensor", "bt_streaming", {
        "name": "Bluetooth Audio Streaming",
        "device_class": "running",
        "state_topic": cfg.topic("bt", "streaming"),
    }))

    entities.append(("sensor", "bt_now_playing", {
        "name": "Now Playing (Bluetooth)",
        "icon": "mdi:music",
        "state_topic": cfg.topic("bt", "now_playing"),
        "value_template": "{{ value_json.status }}",
        "json_attributes_topic": cfg.topic("bt", "now_playing"),
    }))

    for object_id, name, icon, payload in (
        ("bt_playpause", "BT Play/Pause", "mdi:play-pause", "playpause"),
        ("bt_next", "BT Next Track", "mdi:skip-next", "next"),
        ("bt_previous", "BT Previous Track", "mdi:skip-previous", "previous"),
    ):
        entities.append(("button", object_id, {
            "name": name,
            "icon": icon,
            "command_topic": cfg.topic("bt", "transport", "set"),
            "payload_press": payload,
        }))

    entities.append(("sensor", "source", {
        "name": "Active Source",
        "icon": "mdi:import",
        "state_topic": cfg.topic("source"),
    }))

    if cfg.airplay_enabled:
        entities.append(("sensor", "airplay_now_playing", {
            "name": "Now Playing (AirPlay)",
            "icon": "mdi:cast-audio",
            "state_topic": cfg.topic("airplay", "now_playing"),
            "value_template": "{{ value_json.status }}",
            "json_attributes_topic": cfg.topic("airplay", "now_playing"),
        }))
        # shairport-sync consumes these directly on <base>/airplay/remote
        for object_id, name, icon, payload in (
            ("airplay_playpause", "AirPlay Play/Pause", "mdi:play-pause", "playpause"),
            ("airplay_next", "AirPlay Next Track", "mdi:skip-next", "nextitem"),
            ("airplay_previous", "AirPlay Previous Track", "mdi:skip-previous", "previtem"),
        ):
            entities.append(("button", object_id, {
                "name": name,
                "icon": icon,
                "command_topic": cfg.topic("airplay", "remote"),
                "payload_press": payload,
            }))

    if cfg.power_commands:
        # QoS 0 on both ends (see mqtt_link): the broker must never queue a
        # power press for the offline daemon — a queued PRESS would reboot or
        # power off the Pi right after it comes back up
        entities.append(("button", "system_reboot", {
            "name": "Reboot Pi",
            "device_class": "restart",
            "command_topic": cfg.topic("system", "reboot"),
            "payload_press": "PRESS",
            "qos": 0,
        }))
        entities.append(("button", "system_shutdown", {
            "name": "Shutdown Pi",
            "icon": "mdi:power",
            "command_topic": cfg.topic("system", "shutdown"),
            "payload_press": "PRESS",
            "qos": 0,
        }))

    out = []
    for component, object_id, payload in entities:
        payload["unique_id"] = f"{cfg.node_id}_{object_id}"
        payload["availability_topic"] = cfg.availability_topic
        payload["device"] = _device_block(cfg)
        if "command_topic" in payload:
            # QoS 1 commands + our persistent session: the broker queues
            # commands sent while the daemon is briefly reconnecting
            payload.setdefault("qos", 1)
        topic = f"{cfg.discovery_prefix}/{component}/{cfg.node_id}/{object_id}/config"
        out.append((topic, json.dumps(payload, separators=(",", ":"))))
    if not cfg.power_commands:
        # a previous run with power_commands on left retained button configs
        # behind — dead but available-looking power buttons in HA. An empty
        # retained payload removes the entity and clears the broker topic.
        for object_id in ("system_reboot", "system_shutdown"):
            out.append(
                (f"{cfg.discovery_prefix}/button/{cfg.node_id}/{object_id}/config", ""))
    return out


def build_announce(cfg: Config) -> tuple[str, str]:
    """The node announcement: (topic, json_payload), published retained.

    HA Discovery already creates the entities; this exists so a consumer can
    find the *node* without being told its id — subscribe to
    `<announce_prefix>/+`, get every speakerd on the broker with its topics,
    roster and feature set. Retained and self-describing: liveness is not in
    here, it is on `availability_topic`.
    """
    topics = {
        "availability": cfg.availability_topic,
        "source": cfg.topic("source"),
        "bt_streaming": cfg.topic("bt", "streaming"),
        "bt_now_playing": cfg.topic("bt", "now_playing"),
        "bt_transport_set": cfg.topic("bt", "transport", "set"),
        "amp_fix_metadata": cfg.topic("amp", "fix_metadata"),
        "amp_auto_reconnect": cfg.topic("amp", "auto_reconnect"),
        "amp_auto_reconnect_set": cfg.topic("amp", "auto_reconnect", "set"),
    }
    if cfg.airplay_enabled:
        topics["airplay_now_playing"] = cfg.topic("airplay", "now_playing")
        topics["airplay_remote"] = cfg.topic("airplay", "remote")
    if cfg.amp_metadata_export:
        # added under schema 1 without a version bump — retained
        # ON/OFF: is the permanent Media1 player currently registered
        topics["amp_metadata_export"] = cfg.topic("amp", "metadata_export")
    if cfg.power_commands:
        topics["system_reboot"] = cfg.topic("system", "reboot")
        topics["system_shutdown"] = cfg.topic("system", "shutdown")
        topics["system_result"] = cfg.topic("system", "result")

    def roster_entry(dev) -> dict:
        return {
            "slug": dev.slug,
            "name": dev.name,
            "connected_topic": cfg.topic("device", dev.slug, "connected"),
            "command_topic": cfg.topic("device", dev.slug, "set"),
            "result_topic": cfg.topic("device", dev.slug, "result"),
        }

    payload = {
        "schema": ANNOUNCE_SCHEMA,
        "node_id": cfg.node_id,
        "name": cfg.node_name,
        "area": cfg.node_area,
        "version": __version__,
        "base_topic": cfg.base_topic,
        "discovery_prefix": cfg.discovery_prefix,
        "device_id": cfg.device_id,
        "features": {
            "bluetooth": True,
            "airplay": cfg.airplay_enabled,
            "power_commands": cfg.power_commands,
            "amp_auto_reconnect": True,
            "amp_metadata_export": cfg.amp_metadata_export,
        },
        # QoS the consumer must publish with: system/* is 0 by design — a
        # broker-queued power press must never reach the daemon on reconnect
        "command_qos": {"default": 1, "system": 0},
        "topics": topics,
        # the amp is commandable on device/amp/set like any device, but it is
        # discovered as a binary_sensor, not a switch
        "amp": roster_entry(cfg.amp),
        "devices": [roster_entry(d) for d in cfg.ios_devices],
    }
    return cfg.announce_topic, json.dumps(payload, separators=(",", ":"))
