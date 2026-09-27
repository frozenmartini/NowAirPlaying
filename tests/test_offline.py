"""Offline tests: _system_command wiring, auto-reconnect policy, v1.2 node identity
+ announce contract. No live daemon touched."""
import asyncio, copy, json, os, sys, tempfile, tomllib
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from speakerd.__main__ import App, _load_auto_reconnect, _save_auto_reconnect
from speakerd import config as config_mod
from speakerd.discovery import ANNOUNCE_SCHEMA, build_announce, build_discovery

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE_CONFIG = os.path.join(HERE, "fixture-config.toml")

class FakeInfo:
    def wait_for_publish(self, timeout=None): pass
class FakeMqtt:
    def __init__(self): self.published = []
    def publish(self, topic, payload, retain=False, qos=1):
        self.published.append((topic, payload, retain)); return FakeInfo()

def make_app(tmp):
    cfg = config_mod.load(FIXTURE_CONFIG)
    object.__setattr__(cfg, "state_file", os.path.join(tmp, "state.json"))
    object.__setattr__(cfg, "amp_reconnect_debounce_s", 0.05)
    object.__setattr__(cfg, "amp_reconnect_retry_delay_s", 0.05)
    loop = asyncio.get_event_loop()
    app = App(cfg, loop)
    app._orig_subs = app.mqtt._subscriptions  # keep for subscription-QoS asserts
    app.mqtt = FakeMqtt()  # replace the real link before anything publishes
    return app

async def test_system_command(tmp):
    app = make_app(tmp)
    log = os.path.join(tmp, "sudo.log")
    os.environ["FAKE_SUDO_LOG"] = log
    # success path
    await app._system_command("reboot")
    topic, payload, retain = app.mqtt.published[-1]
    r = json.loads(payload)
    assert topic.endswith("system/result") and retain, (topic, retain)
    assert r["action"] == "reboot" and r["ok"] is True and r["error"] is None, r
    assert "systemctl reboot" in open(log).read()
    # failure path
    os.environ["FAKE_SUDO_FAIL"] = "1"
    await app._system_command("shutdown")
    r = json.loads(app.mqtt.published[-1][1])
    assert r["ok"] is False and "simulated systemctl failure" in r["error"], r
    os.environ.pop("FAKE_SUDO_FAIL")
    assert "systemctl poweroff" in open(log).read()
    print("system_command: PASS")

async def test_auto_reconnect_policy(tmp):
    app = make_app(tmp)
    calls = []
    class FakeEngine:
        async def connect_device(self, slug):
            calls.append(slug)
            app._amp_connected = True  # simulate the connect succeeding
            return True, None
    app.engine = FakeEngine()
    # unexpected drop -> reconnect fires
    app.device_changed("amp", False)
    await asyncio.sleep(0.3)
    assert calls == ["amp"], calls
    r = json.loads([p for t, p, _ in app.mqtt.published if t.endswith("device/amp/result")][-1])
    assert r["action"] == "auto_reconnect" and r["ok"] is True and r["attempt"] == 1, r
    # user OFF -> no reconnect
    calls.clear()
    app._amp_user_disconnected = True
    app._amp_connected = False
    app.device_changed("amp", False)
    await asyncio.sleep(0.3)
    assert calls == [], calls
    # gate ON re-arms and reconnects now
    app._set_auto_reconnect(True)
    await asyncio.sleep(0.3)
    assert calls == ["amp"], calls
    # gate OFF -> drop does nothing
    calls.clear()
    app._set_auto_reconnect(False)
    app._amp_connected = False
    app.device_changed("amp", False)
    await asyncio.sleep(0.3)
    assert calls == [], calls
    # gate persisted
    assert _load_auto_reconnect(app.cfg.state_file) is False
    app._set_auto_reconnect(True)
    assert _load_auto_reconnect(app.cfg.state_file) is True
    print("auto_reconnect_policy: PASS")

async def test_retry_exhaustion(tmp):
    app = make_app(tmp)
    class FailEngine:
        async def connect_device(self, slug):
            return False, "org.bluez.Error.Failed: page timeout"
    app.engine = FailEngine()
    app.device_changed("amp", False)
    await asyncio.sleep(0.8)
    r = json.loads([p for t, p, _ in app.mqtt.published if t.endswith("device/amp/result")][-1])
    assert r["action"] == "auto_reconnect" and r["ok"] is False and r["attempt"] == 3, r
    assert "page timeout" in r["error"], r
    print("retry_exhaustion: PASS")

# ---- session-3 review-fix regression tests ----

async def test_system_qos0(tmp):
    """Queued-PRESS fix: system topics subscribe at QoS 0, everything else at 1."""
    app = make_app(tmp)
    subs = dict(app._orig_subs)
    assert subs.get(app.cfg.topic("system", "reboot")) == 0, subs
    assert subs.get(app.cfg.topic("system", "shutdown")) == 0, subs
    assert all(q == 1 for t, q in subs.items() if not t.startswith(app.cfg.topic("system"))), subs
    print("system_qos0: PASS")

async def test_discovery_qos_and_cleanup(tmp):
    from speakerd.discovery import build_discovery
    app = make_app(tmp)
    cfgs = {t: p for t, p in build_discovery(app.cfg)}
    reboot = [p for t, p in cfgs.items() if "system_reboot" in t]
    assert reboot and json.loads(reboot[0])["qos"] == 0, reboot
    fixmeta = [p for t, p in cfgs.items() if "fix_metadata" in t]
    assert fixmeta and json.loads(fixmeta[0])["qos"] == 1, fixmeta
    # power_commands off -> empty retained payloads clean up the old buttons
    object.__setattr__(app.cfg, "power_commands", False)
    cfgs = dict(build_discovery(app.cfg))
    empties = [t for t, p in cfgs.items() if p == ""]
    assert len(empties) == 2 and all("system_" in t for t in empties), empties
    print("discovery_qos_and_cleanup: PASS")

async def test_success_race(tmp):
    """Connect-ok + instant drop must retry, never publish a false success."""
    app = make_app(tmp)
    calls = []
    class FlappyEngine:  # connect 'succeeds' but the link is already down again
        async def connect_device(self, slug):
            calls.append(slug); return True, None
    app.engine = FlappyEngine()
    app.device_changed("amp", False)
    await asyncio.sleep(4.5)  # 3 attempts x (1 s edge-wait + retry delay)
    assert len(calls) == 3, calls  # all tries used, no early false-success exit
    r = json.loads([p for t, p, _ in app.mqtt.published if t.endswith("device/amp/result")][-1])
    assert r["ok"] is False and "dropped immediately" in r["error"], r
    print("success_race: PASS")

async def test_gate_off_under_lock(tmp):
    """Gate switched OFF while the cycle waits on the amp lock -> no attempt."""
    app = make_app(tmp)
    calls = []
    class FakeEngine:
        async def connect_device(self, slug):
            calls.append(slug); app._amp_connected = True; return True, None
    app.engine = FakeEngine()
    await app._locks["amp"].acquire()   # a 'manual op' holds the lock
    app.device_changed("amp", False)    # drop -> cycle spawns after debounce...
    task = app._amp_reconnect_task
    assert task is None                 # ...but the locked() check suppressed it
    app._locks["amp"].release()
    app.device_changed("amp", False)    # re-drop with lock free: cycle spawns
    await asyncio.sleep(0)              # let it start, still in its debounce
    await app._locks["amp"].acquire()   # contend the lock past the debounce
    await asyncio.sleep(0.2)            # cycle passed the gate check, waits on lock
    app._set_auto_reconnect(False)      # user turns the gate off meanwhile
    app._locks["amp"].release()
    await asyncio.sleep(0.2)
    assert calls == [], calls           # under-lock re-check stopped the attempt
    print("gate_off_under_lock: PASS")

async def test_user_off_survives_concurrent_connect(tmp):
    """OFF queued behind an op whose Connected=True edge fires: intent survives."""
    app = make_app(tmp)
    calls = []
    class FakeEngine:
        async def connect_device(self, slug):
            calls.append(("connect", slug)); app._amp_connected = True; return True, None
        async def disconnect_device(self, slug):
            calls.append(("disconnect", slug)); return True, None
    app.engine = FakeEngine()
    app._amp_connected = True
    await app._locks["amp"].acquire()   # concurrent amp op in flight
    off = asyncio.get_event_loop().create_task(app._device_command("amp", "disconnect"))
    await asyncio.sleep(0.05)           # OFF is now parked on the lock, flag unset
    app.device_changed("amp", True)     # the op's Connected=True edge lands
    app._locks["amp"].release()
    await off                           # OFF executes under the lock, sets intent
    assert app._amp_user_disconnected is True
    app._amp_connected = False
    app.device_changed("amp", False)    # the disconnect's drop edge
    await asyncio.sleep(0.3)
    assert ("connect", "amp") not in calls, calls  # no reconnect against the OFF
    print("user_off_survives_concurrent_connect: PASS")

async def test_reeval_after_failed_op(tmp):
    """Failed fix_metadata leaves amp down: auto-reconnect re-fires post-lock."""
    app = make_app(tmp)
    calls = []
    class FakeEngine:
        async def fix_metadata(self):
            return False, "recipe failed"
        async def connect_device(self, slug):
            calls.append(slug); app._amp_connected = True; return True, None
    app.engine = FakeEngine()
    app._amp_connected = False          # drop edge was swallowed inside the lock
    await app._fix_metadata()
    await asyncio.sleep(0.3)
    assert calls == ["amp"], calls
    print("reeval_after_failed_op: PASS")

async def test_state_file_robustness(tmp):
    p = os.path.join(tmp, "state.json")
    with open(p, "w") as f:
        f.write("true")                 # valid JSON, wrong shape: must not crash
    assert _load_auto_reconnect(p, default=False) is False
    cwd = os.getcwd()
    os.chdir(tmp)
    try:
        _save_auto_reconnect("bare.json", False)  # no dirname: must not warn-fail
        assert _load_auto_reconnect("bare.json", default=True) is False
    finally:
        os.chdir(cwd)
    print("state_file_robustness: PASS")


# ------------------------------------------------------- v1.2 node identity

def _cfg_from(tmp, overrides: dict):
    """The fixture config with sections merged in, loaded from a temp file."""
    with open(FIXTURE_CONFIG, "rb") as f:
        raw = tomllib.load(f)
    raw = copy.deepcopy(raw)
    for section, values in overrides.items():
        raw.setdefault(section, {}).update(values)

    def dump(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        return json.dumps(str(v))

    lines = []
    for section, values in raw.items():
        if isinstance(values, list):
            for item in values:
                lines.append(f"[[{section}]]")
                lines += [f"{k} = {dump(v)}" for k, v in item.items()]
        else:
            lines.append(f"[{section}]")
            lines += [f"{k} = {dump(v)}" for k, v in values.items()]
    path = os.path.join(tmp, "config.toml")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return config_mod.load(path)


async def test_discovery_unchanged(tmp):
    """A config with no [node] section must produce discovery identical to the
    recorded baseline: anything else re-keys entities in HA's registry. If a
    change is intended, regenerate the baseline in the same commit."""
    cfg = config_mod.load(FIXTURE_CONFIG)
    assert cfg.node_id == cfg.base_topic == "nowairplaying", cfg.node_id
    assert cfg.device_id == "nowairplaying_pi", cfg.device_id
    with open(os.path.join(HERE, "baseline-discovery.json")) as f:
        baseline = json.load(f)
    now = {t: p for t, p in build_discovery(cfg)}
    assert set(now) == set(baseline), set(now) ^ set(baseline)
    for topic, payload in now.items():
        a, b = json.loads(payload or "{}"), json.loads(baseline[topic] or "{}")
        # the only sanctioned delta is the version string in the device block
        for d in (a, b):
            d.get("device", {}).pop("sw_version", None)
        assert a == b, (topic, a, b)
        assert "suggested_area" not in a.get("device", {}), topic
    print("discovery_unchanged: PASS")


async def test_node_overrides(tmp):
    """[node] id/name/area re-key everything consistently, for a second Pi."""
    cfg = _cfg_from(tmp, {"node": {"id": "kitchen_speaker", "name": "Kitchen Speaker",
                                   "area": "Kitchen"}})
    assert cfg.node_id == "kitchen_speaker" and cfg.device_id == "kitchen_speaker_pi"
    assert cfg.announce_topic == "speakerd/nodes/kitchen_speaker", cfg.announce_topic
    for topic, payload in build_discovery(cfg):
        assert f"/kitchen_speaker/" in topic, topic
        if not payload:
            continue
        p = json.loads(payload)
        assert p["unique_id"].startswith("kitchen_speaker_"), p["unique_id"]
        assert p["device"]["identifiers"] == ["kitchen_speaker_pi"], p["device"]
        assert p["device"]["suggested_area"] == "Kitchen", p["device"]
        # base_topic is independent of node id: state topics must not be re-keyed
        assert p["availability_topic"] == cfg.availability_topic
    for bad in ("Kitchen Speaker", "kitchen-speaker", ""):
        try:
            _cfg_from(tmp, {"node": {"id": bad}})
        except config_mod.ConfigError:
            continue
        raise AssertionError(f"node.id {bad!r} should have been rejected")

    # a base_topic that is legal for MQTT but not for a node id must still boot:
    # only an EXPLICIT node.id is an error, the derived default gets sanitized
    derived = _cfg_from(tmp, {"mqtt": {"base_topic": "Home/Kitchen Speaker"}})
    assert derived.node_id == "home_kitchen_speaker", derived.node_id
    assert derived.base_topic == "Home/Kitchen Speaker", derived.base_topic
    assert derived.topic("source") == "Home/Kitchen Speaker/source"
    print("node_overrides: PASS")


async def test_announce_payload(tmp):
    cfg = config_mod.load(FIXTURE_CONFIG)
    topic, payload = build_announce(cfg)
    a = json.loads(payload)
    assert topic == "speakerd/nodes/nowairplaying", topic
    assert a["schema"] == ANNOUNCE_SCHEMA and a["node_id"] == "nowairplaying"
    assert a["device_id"] == cfg.device_id and a["base_topic"] == cfg.base_topic
    assert a["topics"]["availability"] == cfg.availability_topic
    assert a["command_qos"] == {"default": 1, "system": 0}, a["command_qos"]
    assert a["amp"]["slug"] == "amp"
    assert [d["slug"] for d in a["devices"]] == [d.slug for d in cfg.ios_devices]
    assert "amp" not in [d["slug"] for d in a["devices"]]
    # every topic the announce advertises must be a real one the daemon uses
    for key, t in a["topics"].items():
        assert t.startswith(cfg.base_topic + "/"), (key, t)

    # feature flags gate their topics both ways
    off = _cfg_from(tmp, {"airplay": {"enabled": False},
                          "system": {"power_commands": False}})
    b = json.loads(build_announce(off)[1])
    assert b["features"]["airplay"] is False and b["features"]["power_commands"] is False
    for key in ("airplay_now_playing", "airplay_remote", "system_reboot",
                "system_shutdown", "system_result"):
        assert key not in b["topics"], key
    print("announce_payload: PASS")


async def test_announce_republished(tmp):
    """republish_all must (re)publish the announce retained, before discovery."""
    app = make_app(tmp)
    app.republish_all()
    pubs = app.mqtt.published
    idx = [i for i, (t, _, _) in enumerate(pubs) if t.startswith("speakerd/nodes/")]
    assert len(idx) == 1, [t for t, _, _ in pubs]
    topic, payload, retain = pubs[idx[0]]
    assert retain and json.loads(payload)["node_id"] == app.cfg.node_id
    first_discovery = min(i for i, (t, _, _) in enumerate(pubs)
                          if t.startswith(app.cfg.discovery_prefix + "/"))
    assert idx[0] < first_discovery, (idx[0], first_discovery)
    # the announce is not a command topic and must not be in the state cache
    assert not any(t.startswith("speakerd/nodes/") for t in app._retained)
    print("announce_republished: PASS")


# ------------------------------------------------------- amp metadata export

async def test_amp_export_mapping(tmp):
    from speakerd.amp_export import mpris_metadata, mpris_status
    md = mpris_metadata("Title", "Artist", "Album", 123456, 7)
    assert md["xesam:artist"].signature == "as" and md["xesam:artist"].value == ["Artist"]
    assert md["mpris:length"].value == 123456000  # ms -> us
    assert md["mpris:trackid"].value == "/org/speakerd/track/7"
    # absent fields are omitted, not sent empty — bluetoothd tolerates either,
    # the amp shows exactly what we know
    assert set(mpris_metadata(None, None, None, None, 8)) == {"mpris:trackid"}
    assert mpris_status("playing") == "Playing"
    assert mpris_status("forward-seek") == "Playing"
    assert mpris_status("paused") == "Paused" and mpris_status(None) == "Stopped"
    print("amp_export_mapping: PASS")


async def test_amp_export_arbitration(tmp):
    """One permanent player: source switches are property updates on the same
    object, the playing source wins the screen, amp buttons route to the
    owner, and the announce carries the approved schema-1 additions."""
    raw_path = os.path.join(tmp, "amp-raw.log")
    cfg = _cfg_from(tmp, {"bluetooth": {"amp_metadata_export": True,
                                        "amp_export_raw_log": raw_path},
                          "airplay": {"enabled": True}})
    app = App(cfg, asyncio.get_event_loop())
    app.mqtt = FakeMqtt()
    assert app.amp_export is not None

    app.now_playing_changed({"status": "playing", "title": "BT Song",
                             "artist": "X", "album": None, "duration": 200000,
                             "position": 0, "device": "iPhone"})
    assert app.amp_export._last_key[0] == "bluetooth"
    assert app.amp_export._player.PlaybackStatus == "Playing"
    assert app.amp_export._player.Metadata["xesam:title"].value == "BT Song"

    # BT pauses, AirPlay starts playing -> AirPlay owns the screen
    app._bt_now_playing["status"] = "paused"
    app.airplay.handle("title", b"AP Song")
    app.airplay.handle("play_start", b"")
    app._publish_airplay()
    assert app.amp_export._last_key[0] == "airplay"
    assert app.amp_export._player.Metadata["xesam:title"].value == "AP Song"

    # amp button under AirPlay ownership -> airplay/remote, shairport verbs
    app._amp_export_command("next")
    assert (cfg.topic("airplay", "remote"), "nextitem", False) in app.mqtt.published

    # AirPlay ends -> the paused BT source takes the screen back
    app.airplay.handle("active_end", b"")
    app._publish_airplay()
    assert app.amp_export._last_key[0] == "bluetooth"
    assert app.amp_export._player.PlaybackStatus == "Paused"

    # registration observability flows to the retained topic
    app.amp_export._set_registered(True)
    assert (cfg.topic("amp", "metadata_export"), "ON", True) in app.mqtt.published

    # the raw log recorded every feed unfiltered, dedups marked, plus emits
    app.now_playing_changed(dict(app._bt_now_playing))  # a pure duplicate
    raw = open(raw_path).read()
    assert "dedup=yes" in raw and "dedup=no" in raw, raw
    assert "[EMIT" in raw and "title='BT Song'" in raw.replace('"', "'"), raw
    app.amp_export._player.Next()
    assert "Next -> next" in open(raw_path).read()

    a = json.loads(build_announce(cfg)[1])
    assert a["features"]["amp_metadata_export"] is True
    assert a["topics"]["amp_metadata_export"] == cfg.topic("amp", "metadata_export")
    # explicit false, not {}: the fixture has the gate ON, so a bare {} probe
    # would assert the fixture, not the code's gate-off behaviour.
    off = json.loads(build_announce(
        _cfg_from(tmp, {"bluetooth": {"amp_metadata_export": False}}))[1])
    assert off["features"]["amp_metadata_export"] is False
    assert "amp_metadata_export" not in off["topics"]
    print("amp_export_arbitration: PASS")


async def test_amp_export_reconnect_hygiene(tmp):
    """Three reconnect-hygiene rules: the
    EMIT label follows registration rather than 'the call did not raise', a
    reconnect builds a fresh ServiceInterface that carries the screen across
    instead of stacking dead buses onto one, and raw-log writes leave the
    event loop once the drain task is up."""
    from dbus_next.service import ServiceInterface

    raw_path = os.path.join(tmp, "amp-raw.log")
    cfg = _cfg_from(tmp, {"bluetooth": {"amp_metadata_export": True,
                                        "amp_export_raw_log": raw_path}})
    app = App(cfg, asyncio.get_event_loop())
    app.mqtt = FakeMqtt()
    exp = app.amp_export
    emits = lambda: [l for l in open(raw_path) if "[EMIT" in l]

    # an update before RegisterPlayer reaches nobody, and must say so: the
    # old code called this emitted=yes because emit_properties_changed
    # silently does nothing when the interface is on no bus
    exp.update("bluetooth", "playing", "Pre", "A", None, 1000)
    assert "emitted=no" in emits()[-1], emits()[-1]

    exp._set_registered(True)
    assert exp._player.registered is True, "registration did not reach the player"
    exp.update("bluetooth", "playing", "Post", "A", None, 1000)
    assert "emitted=yes" in emits()[-1], emits()[-1]

    # a reconnect hands out a NEW interface holding the same screen
    old = exp._player
    fresh = exp._new_player()
    assert fresh is not old
    assert fresh.PlaybackStatus == old.PlaybackStatus
    assert fresh.Metadata["xesam:title"].value == "Post"

    # ...and the point of it: one interface, one bus. Re-exporting a single
    # instance is what left dead connections in that set for
    # emit_properties_changed to keep signalling.
    ServiceInterface._add_bus(old, object())          # the connection that died
    ServiceInterface._add_bus(fresh, object())        # its replacement
    assert len(ServiceInterface._get_buses(fresh)) == 1, "fresh interface inherited a bus"
    assert len(ServiceInterface._get_buses(old)) == 1

    # ...and _run is what must ask for it: a connection that re-exports the
    # existing interface is exactly the bug, so assert the wiring, not just
    # the helper. Fake bus — the real one would RegisterPlayer on live BlueZ.
    import speakerd.amp_export as ax

    class _FakeBus:
        def __init__(self):
            self.exported = []
            self._gone = asyncio.get_event_loop().create_future()
        async def connect(self):
            return self
        def export(self, path, iface):
            self.exported.append(iface)
        def add_message_handler(self, handler):
            pass
        async def wait_for_disconnect(self):
            await self._gone
        def disconnect(self):
            if not self._gone.done():
                self._gone.set_result(None)

    buses = []
    async def _noop_call(*a, **k):
        return None
    orig_bus, orig_call = ax.MessageBus, exp._call
    ax.MessageBus = lambda **kw: buses.append(_FakeBus()) or buses[-1]
    exp._call, exp._schedule_register = _noop_call, lambda delay: None
    pre = exp._player
    task = asyncio.get_event_loop().create_task(exp._run())
    try:
        await asyncio.sleep(0.05)
        assert buses[0].exported == [exp._player], buses[0].exported
        assert exp._player is not pre, "_run re-exported the existing interface"
        assert exp._player.Metadata["xesam:title"].value == "Post", "screen lost"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        ax.MessageBus, exp._call = orig_bus, orig_call

    # raw-log writes go to a queue, not to the disk, while the drain runs
    await exp._raw.start()
    exp._raw.write("TEST", "queued")
    assert "queued" not in open(raw_path).read(), "raw log wrote on the event loop"
    await exp._raw.drain()
    assert "queued" in open(raw_path).read()

    # a full backlog drops lines, but never silently: the gap is reported on
    # both edges and marked in the file the capture is correlated against
    exp._raw._pending.extend(["x\n"] * exp._raw.MAX_PENDING)
    exp._raw.write("TEST", "lost")
    assert exp._raw._dropped == 1
    await exp._raw.drain()
    assert exp._raw._dropped == 0, "drop counter never reset — no second warning"
    await exp._raw.drain()
    assert "DROPPED" in open(raw_path).read(), "silent gap in the forensic log"

    # outside the started window there is no loop to protect: write directly,
    # so the shutdown line still lands and the harness stays synchronous
    await exp._raw.aclose()
    exp._raw.write("TEST", "after-close")
    assert "after-close" in open(raw_path).read()
    print("amp_export_reconnect_hygiene: PASS")


async def main():
    # expose fake-sudo.sh as `sudo` ahead of the real one, so no manual PATH setup is needed
    with tempfile.TemporaryDirectory() as fake_bin:
        os.symlink(os.path.join(HERE, "fake-sudo.sh"), os.path.join(fake_bin, "sudo"))
        os.environ["PATH"] = fake_bin + os.pathsep + os.environ["PATH"]
        for test in (test_system_command, test_auto_reconnect_policy, test_retry_exhaustion,
                     test_system_qos0, test_discovery_qos_and_cleanup, test_success_race,
                     test_gate_off_under_lock, test_user_off_survives_concurrent_connect,
                     test_reeval_after_failed_op, test_state_file_robustness,
                     test_discovery_unchanged, test_node_overrides,
                     test_announce_payload, test_announce_republished,
                     test_amp_export_mapping, test_amp_export_arbitration,
                     test_amp_export_reconnect_hygiene):
            with tempfile.TemporaryDirectory() as tmp:
                await test(tmp)

asyncio.run(main())
