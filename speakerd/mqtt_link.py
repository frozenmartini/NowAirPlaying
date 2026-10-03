"""paho-mqtt 1.6 / 2.x (v1 callback API) <-> asyncio bridge.

paho runs its network loop in its own thread; every callback here fires on
that thread and must only hand data to asyncio via loop.call_soon_threadsafe.
client.publish()/subscribe() are thread-safe, so the asyncio side calls them
directly. connect_async + loop_start means a dead broker never kills the
daemon — paho retries internally forever.
"""
from __future__ import annotations

import asyncio
import logging

import paho.mqtt.client as mqtt

from .config import Config

log = logging.getLogger("speakerd.mqtt")

EV_CONNECTED = "mqtt_connected"
EV_MESSAGE = "mqtt_message"


class MqttLink:
    def __init__(self, cfg: Config, loop: asyncio.AbstractEventLoop,
                 queue: asyncio.Queue, subscriptions: list[tuple[str, int]]):
        self._cfg = cfg
        self._loop = loop
        self._queue = queue
        self._subscriptions = subscriptions

        # clean_session=False + stable client_id: the broker keeps our
        # subscriptions and queues QoS-1 commands sent while we are
        # reconnecting. Topics that must NOT be queued for the offline
        # session (power and transport buttons) subscribe at QoS 0.
        # paho 2.x (trixie) refuses a Client without a callback API version;
        # ask for v1 so the callbacks below keep their 1.6 signatures
        api = (mqtt.CallbackAPIVersion.VERSION1,) if hasattr(mqtt, "CallbackAPIVersion") else ()
        c = mqtt.Client(*api, client_id=cfg.client_id, clean_session=False)
        c.username_pw_set(cfg.username, cfg.password)
        c.will_set(cfg.availability_topic, "offline", qos=1, retain=True)
        c.reconnect_delay_set(min_delay=1, max_delay=30)
        # bound the client-side QoS-1 queue during broker outages: on_connect
        # republishes the full retained state anyway, a backlog adds nothing
        c.max_queued_messages_set(100)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self._client = c
        self.connected = False  # written by paho's thread, read for /verify

    def start(self) -> None:
        self._client.connect_async(self._cfg.host, self._cfg.port, keepalive=30)
        self._client.loop_start()

    def stop(self) -> None:
        """Graceful shutdown from the main thread (never from a paho callback)."""
        try:
            info = self._client.publish(
                self._cfg.availability_topic, "offline", qos=1, retain=True)
            info.wait_for_publish(timeout=3)
        except Exception:
            log.exception("failed to publish offline availability on shutdown")
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:
            pass

    def publish(self, topic: str, payload: str, retain: bool = False,
                qos: int = 1) -> mqtt.MQTTMessageInfo:
        return self._client.publish(topic, payload, qos=qos, retain=retain)

    # ---- paho callbacks: paho network thread ----

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("MQTT connect failed rc=%s (%s)", rc, mqtt.connack_string(rc))
            return
        log.info("MQTT connected to %s:%s", self._cfg.host, self._cfg.port)
        self.connected = True
        for topic, qos in self._subscriptions:
            client.subscribe(topic, qos=qos)
        self._loop.call_soon_threadsafe(self._queue.put_nowait, (EV_CONNECTED, None, None))

    def _on_disconnect(self, client, userdata, rc):
        self.connected = False
        if rc != 0:
            log.warning("MQTT connection lost rc=%s, paho will reconnect", rc)

    def _on_message(self, client, userdata, msg):
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait,
            (EV_MESSAGE, msg.topic, (msg.payload, bool(msg.retain))))


class _NoPublish:
    def wait_for_publish(self, timeout=None) -> None:
        pass


class NullMqttLink:
    """Stand-in when [mqtt] is off: the node runs standalone, every publish
    goes nowhere and no command ever arrives. Keeps App free of MQTT checks."""

    def __init__(self, *args, **kwargs):
        self._subscriptions: list[tuple[str, int]] = []
        self.connected = False

    def start(self) -> None:
        log.info("MQTT disabled in config — running standalone")

    def stop(self) -> None:
        pass

    def publish(self, topic: str, payload: str, retain: bool = False,
                qos: int = 1) -> _NoPublish:
        return _NoPublish()
