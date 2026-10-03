"""Offline tests for the node API (docs/SETUP-API.md), across both processes:
speakerd (App + AudioNode + the control socket) and the API service (ApiNode
+ the real HTTP and HTTPS listeners on free ports with a throwaway
certificate), talking over a real Unix socket. Covers the claim and its token
rules, SSE, phones, the pairing window and agent policy, updates, speakerd
going away, /verify's merge, and the Wi-Fi input format. BlueZ, avahi,
NetworkManager and systemctl are faked. Needs python3-aiohttp and openssl."""
import asyncio, hashlib, json, os, socket, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from dbus_next import DBusError
from dbus_next.service import ServiceInterface

from dbus_next import Variant

import speakerd.__main__ as speakerd_main
from speakerd import config as config_mod
from speakerd.__main__ import App
from speakerd.agent import PairingAgent
from speakerd.api import Api
from speakerd import apiserver
from speakerd.apiserver import ApiConfig, ApiNode, StateHub
from speakerd.control import ControlServer
from speakerd.errors import ApiError, valid_name
from speakerd.netman import wifi_settings
from speakerd.system import CHECK_ORDER, System
from speakerd.wifi_add import InputError, parse

HERE = os.path.dirname(os.path.abspath(__file__))
AMP = "00:00:00:00:00:AA"
PHONE = "00:00:00:00:00:B1"
ADAPTER = "00:00:00:00:00:F0"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeMqtt:
    def __init__(self): self.published = []
    def publish(self, topic, payload, retain=False, qos=1):
        self.published.append((topic, payload, retain))


class FakeNm:
    async def start(self): pass
    async def stop(self): pass
    async def state(self):
        return {"link": "wifi", "ssid": "Home", "signal": 70, "ip": "192.0.2.10"}


class FakeZeroconf:
    def __init__(self): self.published = []
    async def start(self): pass
    async def stop(self): pass
    async def publish(self, name, port, txt): self.published.append((name, port, dict(txt)))


class Rig:
    """Both processes in one event loop."""

    def __init__(self, tmp, install_version="0.0.2"):
        self.tmp = tmp
        tls = os.path.join(tmp, "tls")
        os.makedirs(tls)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
                        "-subj", "/CN=test", "-keyout", os.path.join(tls, "key.pem"),
                        "-out", os.path.join(tls, "cert.pem")],
                       check=True, capture_output=True)
        install_json = os.path.join(tmp, "install.json")
        with open(install_json, "w") as f:
            json.dump({"state": "done", "version": install_version}, f)
        with open(os.path.join(tmp, "install-args"), "w") as f:
            f.write("USER=pi\nPHONES=dongle\n")
        self.shairport_conf = os.path.join(tmp, "shairport-sync.conf")
        with open(self.shairport_conf, "w") as f:
            f.write('general =\n{\n  name = "Old Name";\n};\n')
        self.sock = os.path.join(tmp, "speakerd.sock")
        cfg_path = os.path.join(tmp, "config.toml")
        with open(cfg_path, "w") as f:
            f.write(f"""
[mqtt]
host = "broker.invalid"
username = "u"
password = "p"
[bluetooth]
adapter = "hci0"
[airplay]
enabled = true
[system]
state_file = "{tmp}/state.json"
[control]
enabled = true
socket = "{self.sock}"
shairport_conf = "{self.shairport_conf}"
""")
        # speakerd's side
        self.app = App(config_mod.load(cfg_path), asyncio.get_event_loop())
        self.app.mqtt = FakeMqtt()
        self.node = self.app.node
        self.node.adapter_mac = ADAPTER
        self.calls = []
        self._fake_engine()
        # the API service's side
        self.cfg = ApiConfig(https_port=free_port(), http_port=free_port(), socket=self.sock,
                             state_dir=os.path.join(tmp, "api"), tls_dir=tls,
                             claim_dir=os.path.join(tmp, "claim"),
                             update_dir=os.path.join(tmp, "update"),
                             install_json=install_json,
                             install_args=os.path.join(tmp, "install-args"))
        self.api_node = ApiNode(self.cfg)
        self.api_node.nm = FakeNm()
        self.api_node.zeroconf = FakeZeroconf()
        self.control = ControlServer(self.node, self.sock)
        self.api = Api(self.api_node)

    def _fake_engine(self):
        eng, calls = self.app.engine, self.calls

        async def device_method(mac, member, timeout=30):
            calls.append((member, mac))
            if member == "Pair":
                eng._devices[eng.device_path(mac)]["Paired"] = True
            return True, None

        async def remove_device(mac):
            calls.append(("RemoveDevice", mac))
            eng._devices.pop(eng.device_path(mac), None)
            return True, None

        async def set_adapter(prop, sig, value): calls.append(("adapter", prop, value))
        async def set_device(mac, prop, sig, value): calls.append(("device", mac, prop, value))
        async def connect_device(slug): calls.append(("Connect", slug)); return True, None
        async def disconnect_device(slug): calls.append(("Disconnect", slug)); return True, None
        async def nothing(*a, **k): calls.append(a)

        eng.device_method = device_method
        eng.remove_device = remove_device
        eng.set_adapter = set_adapter
        eng.set_device = set_device
        eng.connect_device = connect_device
        eng.disconnect_device = disconnect_device
        eng.start_discovery = eng.stop_discovery = nothing
        eng.roster_changed = nothing

    async def __aenter__(self):
        await self.control.start()
        await self.api_node.start()
        await self.api.start()
        await self.wait(lambda: self.api_node.audio.get("adapter_mac") == ADAPTER)
        return self

    async def __aexit__(self, *exc):
        await self.api.stop()
        await self.api_node.stop()
        await self.control.stop()
        self.node.stop()

    # Generous on purpose: a loaded Pi once missed 5 s. A pass returns early.
    async def wait(self, cond, timeout=15):
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout
        while not cond():
            if loop.time() > end:
                raise AssertionError("timed out waiting")
            await asyncio.sleep(0.02)

    def add_device(self, mac, name, paired, connected=False):
        self.app.engine._devices[self.app.engine.device_path(mac)] = {
            "Address": mac, "Alias": name, "Paired": paired, "Trusted": paired,
            "Connected": connected, "RSSI": -50,
            "Class": 0x240414 if name == "Amp" else 0x5a020c, "UUIDs": ()}


class Client:
    def __init__(self, rig, session):
        self.s = session
        self.https = f"https://127.0.0.1:{rig.cfg.https_port}/api/v2"
        self.http = f"http://127.0.0.1:{rig.cfg.http_port}/api/v2"
        self.token = None

    async def req(self, method, path, body=None, secure=True, token="default"):
        url = (self.https if secure else self.http) + path
        headers = {}
        tok = self.token if token == "default" else token
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
        async with self.s.request(method, url, json=body, headers=headers, ssl=False) as r:
            return r.status, await r.json()

    async def claim(self):
        st, res = await self.req("POST", "/claim", {"claimed_by": "ha"}, token=None)
        assert st == 200, res
        self.token = res["token"]


async def with_rig(tmp, fn, **kw):
    async with Rig(tmp, **kw) as rig:
        async with aiohttp.ClientSession() as s:
            await fn(rig, Client(rig, s))


def raw_and_hash():
    raw = os.urandom(32)
    return raw.hex(), hashlib.sha256(raw).hexdigest()


# ----------------------------------------------------------------- tests

async def test_unclaimed_and_https_rules(tmp):
    async def body(rig, c):
        st, info = await c.req("GET", "/info", secure=False)
        assert st == 200 and info["state"] == "unclaimed" and info["claim"] == "open", info
        assert info["api"] == 2 and info["version"] == "0.0.2" and info["amp"] is None, info
        assert info["id"] == "nowairplaying_0000f0" and info["mac"] == ADAPTER, info
        assert info["phones"] == "dongle", info
        # the zeroconf record follows speakerd's state
        name, port, txt = rig.api_node.zeroconf.published[-1]
        assert port == rig.cfg.https_port and txt["state"] == "unclaimed" and txt["api"] == "2"
        assert txt["mac"] == ADAPTER and txt["id"] == info["id"], txt
        st, err = await c.req("GET", "/info", secure=False, token="ab" * 32)
        assert st == 403 and err["error"] == "https_required", err
        st, err = await c.req("POST", "/claim", {}, secure=False)
        assert st == 403 and err["error"] == "https_required", err
        for method, path, b in (("PUT", "/node/name", {"name": "X"}), ("POST", "/node/reboot", None),
                                ("POST", "/node/update", {}), ("POST", "/release", None)):
            st, err = await c.req(method, path, b)
            assert st == 403 and err["error"] == "claim_required", (path, err)
        st, state = await c.req("GET", "/state", secure=False)
        assert st == 200 and state["amp"] is None and state["source"] == "idle", state
        assert state["network"] == {"link": "wifi", "ssid": "Home", "signal": 70,
                                    "ip": "192.0.2.10"}, state["network"]
        # Wi-Fi is not an API call any more
        st, err = await c.req("POST", "/wifi", {"ssid": "x"})
        assert st == 404 and err["error"] == "not_found", err
        st, err = await c.req("POST", "/amp/connect")
        assert st == 404 and err["error"] == "not_found", err  # speakerd: no amp yet
    await with_rig(tmp, body)
    print("unclaimed_and_https_rules: PASS")


async def test_planted_claim(tmp):
    async def body(rig, c):
        bearer, h = raw_and_hash()
        os.makedirs(rig.cfg.claim_dir)
        planted = os.path.join(rig.cfg.claim_dir, "claim-token")
        with open(planted, "w") as f:
            f.write(h + "\n")
        st, info = await c.req("GET", "/info")
        assert info["claim"] == "token", info
        st, err = await c.req("POST", "/claim", {"name": "Bath"}, token="cd" * 32)
        assert st == 401 and err["error"] == "unauthorized", err
        st, err = await c.req("POST", "/claim", {"name": "Bath"}, token=None)
        assert st == 401, err
        st, res = await c.req("POST", "/claim", {"name": "Bathroom Speaker", "area": "Bathroom",
                                                 "claimed_by": "homeassistant.local"}, token=bearer)
        assert st == 200 and len(res["token"]) == 64, res
        assert not os.path.exists(planted), "a used planted token must be deleted"
        c.token = res["token"]
        await rig.wait(lambda: rig.api_node.name == "Bathroom Speaker")
        st, info = await c.req("GET", "/info")
        assert info["state"] == "claimed" and info["claim"] == "open", info
        assert info["claimed_by"] == "homeassistant.local" and info["area"] == "Bathroom", info
        with open(rig.shairport_conf) as f:
            assert 'name = "Bathroom Speaker";' in f.read()
        stored = json.load(open(rig.cfg.api_state_file))
        assert stored["token_sha256"] == hashlib.sha256(bytes.fromhex(c.token)).hexdigest()
        assert c.token not in json.dumps(stored)
        st, err = await c.req("POST", "/claim", {}, token=None)
        assert st == 409 and err["error"] == "already_claimed", err
        # speakerd cleared its MQTT discovery, and remembers that
        cleared = [t for t, p, r in rig.app.mqtt.published if p == "" and r]
        assert any(t.startswith("homeassistant/") for t in cleared), cleared
        assert json.load(open(os.path.join(rig.tmp, "state.json")))["mqtt_discovery"] is False
        await rig.wait(lambda: rig.api_node.zeroconf.published[-1][2]["state"] == "claimed")
        assert rig.api_node.zeroconf.published[-1][0] == "Bathroom Speaker"
    await with_rig(tmp, body)
    print("planted_claim: PASS")


async def test_claimed_auth(tmp):
    async def body(rig, c):
        await c.claim()
        st, err = await c.req("GET", "/state", secure=False, token=None)
        assert st == 401 and err["error"] == "unauthorized", err
        st, err = await c.req("GET", "/state", token="ef" * 32)
        assert st == 401, err
        st, err = await c.req("GET", "/state", token="not-hex")
        assert st == 401, err
        st, state = await c.req("GET", "/state")
        assert st == 200 and set(state) == {"node", "network", "amp", "phones", "source",
                                            "bluetooth_streaming", "now_playing"}, state
        rig.node.roster.set_amp(AMP, "Amp")
        rig.node.changed()
        await rig.wait(lambda: rig.api_node.audio.get("amp") is not None)
        st, res = await c.req("POST", "/amp/disconnect", secure=False, token=None)
        assert st == 200 and ("Disconnect", "amp") in rig.calls, (res, rig.calls)
        st, res = await c.req("POST", "/amp/connect", secure=False, token=None)
        assert st == 200 and ("Connect", "amp") in rig.calls, res
        st, err = await c.req("POST", "/amp/reconnect", secure=False, token=None)
        assert st == 401, err
        st, _ = await c.req("PUT", "/amp/auto-reconnect", {"on": False})
        assert st == 200 and rig.app.auto_reconnect is False
        # speakerd's validation error comes back through the socket
        st, err = await c.req("PUT", "/amp/auto-reconnect", {"on": "no"})
        assert st == 400 and err["error"] == "bad_request", err
        st, err = await c.req("PUT", "/node/name", {"name": 'Bad "name"'})
        assert st == 400 and err["error"] == "bad_request", err
        st, _ = await c.req("PUT", "/node/name", {"name": "Kitchen"})
        assert st == 200 and rig.node.name == "Kitchen"
        assert ("adapter", "Alias", "Bluetooth Kitchen") in rig.calls, rig.calls
        rig.app.mqtt.published.clear()
        st, _ = await c.req("POST", "/release")
        assert st == 200 and not rig.api_node.claims.claimed
        assert any(t.startswith("homeassistant/") and p for t, p, r in rig.app.mqtt.published)
    await with_rig(tmp, body)
    print("claimed_auth: PASS")


async def test_sse(tmp):
    async def body(rig, c):
        await c.claim()
        url = c.https + "/events"
        async with c.s.get(url, headers={"Authorization": f"Bearer {c.token}"}, ssl=False) as r:
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")

            async def event():
                lines = {}
                while True:
                    line = (await asyncio.wait_for(r.content.readline(), 5)).decode().rstrip("\n")
                    if not line:
                        if lines:
                            return lines
                        continue
                    if line.startswith(":"):
                        continue
                    k, _, v = line.partition(": ")
                    lines[k] = v
            first = await event()
            assert first["event"] == "state", first
            seq = int(first["id"])
            assert json.loads(first["data"])["bluetooth_streaming"] is False
            # two changes in one loop iteration in speakerd: one push, one event
            rig.app.streaming_changed(True)
            rig.app.now_playing_changed({"status": "playing", "title": "T", "artist": None,
                                         "album": None, "duration": None, "position": None,
                                         "device": None})
            ev = await event()
            assert ev["event"] == "change" and int(ev["id"]) == seq + 1, ev
            change = json.loads(ev["data"])
            assert change["bluetooth_streaming"] is True and change["source"] == "bluetooth", change
            assert change["now_playing"]["bluetooth"]["title"] == "T", change
            assert "node" not in change, "unchanged keys must not be sent"
            st, _ = await c.req("POST", "/release")
            assert st == 200
            rest = await asyncio.wait_for(r.content.read(), 5)
            assert b"event: state" not in rest, rest
    await with_rig(tmp, body)
    print("sse: PASS")


async def test_phones_and_pairing(tmp):
    async def body(rig, c):
        node = rig.node
        rig.add_device(PHONE, "Pat's iPhone", paired=True, connected=True)
        rig.add_device(AMP, "Amp", paired=False)
        node.devices_changed()
        await rig.wait(lambda: rig.api_node.audio["phones"]["devices"])
        st, state = await c.req("GET", "/state")
        phones = state["phones"]["devices"]
        assert [p["mac"] for p in phones] == [PHONE] and phones[0]["connected"], phones
        st, found = await c.req("GET", "/amp/found")
        assert [d["mac"] for d in found["devices"]] == [AMP] and found["devices"][0]["likely_amp"]
        assert node.pin_for(AMP) is None
        st, res = await c.req("POST", "/amp/pair", {"mac": AMP.lower()})
        assert st == 200, res
        assert ("Pair", AMP) in rig.calls and ("device", AMP, "Trusted", True) in rig.calls
        assert node.roster.amp_mac == AMP
        assert json.load(open(os.path.join(rig.tmp, "state.json")))["amp"]["mac"] == AMP
        st, err = await c.req("POST", f"/phones/{AMP}/connect")
        assert st == 404, err
        st, _ = await c.req("POST", f"/phones/{PHONE}/disconnect")
        assert st == 200 and ("Disconnect", PHONE) in rig.calls
        await rig.wait(lambda: rig.api_node.audio["phones"]["devices"][0]["last_result"])
        assert node.pin_for("00:00:00:00:00:C1") is None and not node.accept_pairing("x")
        st, _ = await c.req("POST", "/phones/pairing", {"seconds": 120})
        assert st == 200 and node.pairing_open
        assert ("adapter", "Discoverable", True) in rig.calls, rig.calls
        assert node.pin_for("00:00:00:00:00:C1") == "0000" and node.accept_pairing("x")
        new = "00:00:00:00:00:C1"
        rig.add_device(new, "New Phone", paired=True)
        node.devices_changed()
        await asyncio.sleep(0)
        assert node._last_paired == new and ("device", new, "Trusted", True) in rig.calls
        await rig.wait(lambda: rig.api_node.audio["phones"]["pairing"]["last_paired"] == new)
        st, state = await c.req("GET", "/state")
        assert state["phones"]["pairing"]["open"], state
        st, _ = await c.req("POST", "/phones/pairing", {"open": False})
        assert not node.pairing_open and ("adapter", "Discoverable", False) in rig.calls
        assert not node.accept_pairing(new)
        assert node.accept_service(new, "x"), "a paired phone's profiles are accepted"
        st, err = await c.req("POST", "/phones/pairing", {"seconds": 9999})
        assert st == 400, err
        st, _ = await c.req("DELETE", f"/phones/{new}")
        assert st == 200 and ("RemoveDevice", new) in rig.calls
        st, err = await c.req("DELETE", f"/phones/{new}")
        assert st == 404, err
        st, err = await c.req("POST", "/phones/zz/connect")
        assert st == 400, err
        st, _ = await c.req("POST", "/amp/forget")
        assert st == 200 and node.roster.amp is None
        assert json.load(open(os.path.join(rig.tmp, "state.json")))["amp"] is None
        st, err = await c.req("POST", "/amp/forget")
        assert st == 404, err
    await with_rig(tmp, body)
    print("phones_and_pairing: PASS")


async def test_speakerd_away(tmp):
    async def body(rig, c):
        await rig.control.stop()
        await rig.wait(lambda: not rig.api_node.audio_client.connected)
        st, err = await c.req("POST", "/amp/connect")
        assert st == 502 and "speakerd is not running" in err["message"], err
        st, state = await c.req("GET", "/state")
        assert st == 200 and state["amp"] is None and state["phones"]["devices"] == [], state
        st, v = await c.req("GET", "/verify")
        by_id = {x["id"]: x for x in v["checks"]}
        assert by_id["speakerd_running"]["ok"] is False and v["ok"] is False, v
        # it comes back on its own
        rig.control = ControlServer(rig.node, rig.sock)
        await rig.control.start()
        await rig.wait(lambda: rig.api_node.audio.get("adapter_mac") == ADAPTER)
        st, info = await c.req("GET", "/info")
        assert info["mac"] == ADAPTER, info
    await with_rig(tmp, body)
    print("speakerd_away: PASS")


async def test_verify_merge(tmp):
    async def body(rig, c):
        st, v = await c.req("GET", "/verify")
        assert st == 200 and [x["id"] for x in v["checks"]] == list(CHECK_ORDER), v
        by_id = {x["id"]: x for x in v["checks"]}
        assert by_id["speakerd_running"]["ok"] is True, by_id["speakerd_running"]
    await with_rig(tmp, body)
    print("verify_merge: PASS")


async def test_agent_rejects(tmp):
    class Policy:
        open = False
        def pin_for(self, mac): return "0000" if self.open else None
        def accept_pairing(self, mac): return self.open
        def accept_service(self, mac, uuid): return self.open
    p = Policy()
    agent = PairingAgent(p)
    dev = "/org/bluez/hci0/dev_00_00_00_00_00_C1"
    methods = {m.name: m.fn for m in ServiceInterface._get_methods(agent)}
    for name, args in (("RequestPinCode", (dev,)), ("RequestConfirmation", (dev, 123456)),
                       ("RequestPasskey", (dev,))):
        try:
            methods[name](agent, *args)
        except DBusError as e:
            assert e.type == "org.bluez.Error.Rejected"
        else:
            raise AssertionError(f"closed window must reject {name}")
    p.open = True
    assert methods["RequestPinCode"](agent, dev) == "0000"
    methods["RequestConfirmation"](agent, dev, 123456)  # no raise
    try:
        methods["RequestPasskey"](agent, dev)
    except DBusError:
        pass
    else:
        raise AssertionError("passkey entry is never possible here")
    print("agent_rejects: PASS")


async def test_update(tmp):
    async def body(rig, c):
        log = os.path.join(tmp, "systemctl.log")
        os.environ["FAKE_SYSTEMCTL_LOG"] = log
        await c.claim()
        st, err = await c.req("POST", "/node/update", {"version": "1.0", "sha256": "a" * 64})
        assert st == 400, err
        st, err = await c.req("POST", "/node/update", {"version": "0.0.3", "sha256": "A" * 64})
        assert st == 400, err
        # fake systemctl reports every unit "activating": busy
        st, err = await c.req("POST", "/node/update", {"version": "0.0.3", "sha256": "a" * 64})
        assert st == 409 and err["error"] == "busy", err
        os.environ["FAKE_SYSTEMCTL_FAIL"] = "1"
        st, err = await c.req("POST", "/node/update", {"version": "0.0.1", "sha256": "a" * 64})
        assert st == 409 and err["error"] == "downgrade", err
        st, err = await c.req("POST", "/node/update", {"version": "0.0.3", "sha256": "a" * 64})
        assert st == 502, err
        req = json.load(open(os.path.join(rig.cfg.update_dir, "request.json")))
        assert req == {"version": "0.0.3", "sha256": "a" * 64}, req
        assert "start nowairplaying-update.service" in open(log).read()
        sysm = System(rig.cfg.install_json, rig.cfg.update_dir)
        with open(rig.cfg.install_json, "w") as f:
            json.dump({"state": "failed", "version": "0.0.3", "phase_name": "verify",
                       "reason": "verify_failed", "message": "2 check(s) FAILED.",
                       "rolled_back": True}, f)
        u = await sysm.update_state()
        assert u["state"] == "failed" and u["rolled_back"] and u["reason"] == "verify_failed", u
        with open(rig.cfg.install_json, "w") as f:
            json.dump({"state": "done", "version": "0.0.3"}, f)
        u = await sysm.update_state()
        assert u["state"] == "done" and u["reason"] is None, u
        os.environ.pop("FAKE_SYSTEMCTL_FAIL")
        u = await sysm.update_state()
        assert u["state"] == "running", u
        assert sysm.installed_version() == "0.0.3"
    await with_rig(tmp, body)
    print("update: PASS")


async def test_review_fixes(tmp):
    """The 0.0.2 security review (F3, F5, F6, F7) and the first Pi run."""
    # F3: a control character in a name never reaches shairport-sync.conf
    for bad in ("Room\nfoo", "Tab\there", "Bell\x07", "Del\x7f"):
        try:
            valid_name(bad)
        except ApiError as e:
            assert e.code == "bad_request", e
        else:
            raise AssertionError(f"accepted {bad!r}")
    assert valid_name(" Pat's Room ") == "Pat's Room"

    # F7: close_all ends even a subscriber whose queue is full
    hub = StateHub(dict)
    q = hub.subscribe()
    while not q.full():
        q.put_nowait((0, "change", {}))
    hub.close_all()
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert items[-1] is None, "a full subscriber must still get the end"

    async def body(rig, c):
        # first .156 run: with no name in state.json, speakerd starts out with
        # the AirPlay name the install wrote, not one made from the node id
        assert rig.node.name == "Old Name", rig.node.name
        # and the API's version follows install.json live: it starts during
        # phase 5, before the "done" write
        rec = json.load(open(rig.cfg.install_json))
        with open(rig.cfg.install_json, "w") as f:
            json.dump(dict(rec, state="installing"), f)
        st, info = await c.req("GET", "/info")
        assert info["version"] == "0.0.0", info
        with open(rig.cfg.install_json, "w") as f:
            json.dump(rec, f)
        st, info = await c.req("GET", "/info")
        assert info["version"] == "0.0.2", info

        # F5: a planted file that's there but unusable locks the claim
        os.makedirs(rig.cfg.claim_dir)
        planted = os.path.join(rig.cfg.claim_dir, "claim-token")
        with open(planted, "w") as f:
            f.write("not a hash\n")
        st, info = await c.req("GET", "/info")
        assert info["claim"] == "token", info
        st, err = await c.req("POST", "/claim", {}, token=None)
        assert st == 409 and err["error"] == "claim_token_invalid", err
        _, h = raw_and_hash()
        with open(planted, "w") as f:
            f.write(h)
        os.chmod(planted, 0)
        if os.geteuid() != 0:  # root reads it anyway
            st, err = await c.req("POST", "/claim", {}, token=None)
            assert st == 409 and err["error"] == "claim_token_invalid", err
        os.remove(planted)
        st, info = await c.req("GET", "/info")
        assert info["claim"] == "open", info
        # F3 over HTTP: the claim's name is checked before anything changes
        st, err = await c.req("POST", "/claim", {"name": "Room\nfoo"}, token=None)
        assert st == 400 and not rig.api_node.claims.claimed, err

        # F6: inside the pending window, a finished update shows as soon as
        # install.json is newer than the request, and not before
        node = rig.api_node
        os.environ["FAKE_SYSTEMCTL_FAIL"] = "1"  # the unit isn't active
        os.makedirs(rig.cfg.update_dir, exist_ok=True)
        with open(os.path.join(rig.cfg.update_dir, "request.json"), "w") as f:
            json.dump({"version": "0.0.2", "sha256": "a" * 64}, f)
        with open(rig.cfg.install_json, "w") as f:
            json.dump({"state": "done", "version": "0.0.2"}, f)
        old_poll, apiserver.UPDATE_POLL_S = apiserver.UPDATE_POLL_S, 0.01
        try:
            loop = asyncio.get_running_loop()
            node._update = {"state": "running"}
            node._update_pending_until = loop.time() + 15
            node._update_requested_at = time.time() + 100  # install.json is older
            try:
                await asyncio.wait_for(node._poll_update(), 0.3)
            except asyncio.TimeoutError:
                pass
            else:
                raise AssertionError("a record from before the request ended the wait")
            assert node._update == {"state": "running"}, node._update
            node._update_requested_at = time.time() - 100  # install.json is newer
            await asyncio.wait_for(node._poll_update(), 2)
            assert node._update["state"] == "done", node._update
        finally:
            apiserver.UPDATE_POLL_S = old_poll
    await with_rig(tmp, body)
    print("review_fixes: PASS")


async def test_amp_audio_restore(tmp):
    """First .156 run: a WirePlumber restart dropped the amp's A2DP transport
    while Device1 stayed connected, and nothing brought the audio back. Plus
    the review of that fix (workspace/docs/nowairplaying-amp-audio-review.md)."""
    async def body(rig, c):
        import dataclasses
        app, eng = rig.app, rig.app.engine
        consts = ("AMP_AUDIO_GRACE_S", "AMP_AUDIO_WAIT_S", "AMP_AUDIO_SLOW_RETRY_S")
        saved = {k: getattr(speakerd_main, k) for k in consts}
        speakerd_main.AMP_AUDIO_GRACE_S = 0.05
        speakerd_main.AMP_AUDIO_WAIT_S = 0.2
        speakerd_main.AMP_AUDIO_SLOW_RETRY_S = 0.1
        app.cfg = dataclasses.replace(app.cfg, amp_reconnect_retry_delay_s=0.01)
        dev_path = eng.device_path(AMP)
        fd = dev_path + "/sep1/fd0"
        mode = {"profile": "audio", "fix": True}  # profile: audio | nothing | fail

        def transport_up():
            eng._on_interfaces_added(fd, {"org.bluez.MediaTransport1":
                                          {"State": Variant("s", "idle")}})

        def transport_down():
            eng._on_interfaces_removed(fd, ["org.bluez.MediaTransport1"])

        async def connect_amp_audio():
            rig.calls.append(("ConnectProfile", "amp"))
            if mode["profile"] == "fail":
                return False, "org.bluez.Error.Failed"
            if mode["profile"] == "audio":
                transport_up()
            return True, None

        async def fix_metadata():
            rig.calls.append(("fix_metadata", "amp"))
            if mode["fix"]:
                transport_up()
            return True, None

        def reconnect_edge():
            eng._dev_paths[AMP] = {dev_path: False}
            eng._publish_device(AMP)
            eng._dev_paths[AMP] = {dev_path: True}
            eng._publish_device(AMP)

        eng.connect_amp_audio = connect_amp_audio
        eng.fix_metadata = fix_metadata
        try:
            rig.node.roster.set_amp(AMP, "Amp")
            rig.add_device(AMP, "Amp", paired=True, connected=True)
            eng._dev_paths[AMP] = {dev_path: True}
            eng._publish_device(AMP)  # connected, and no transport
            assert app.amp_connected and not app.amp_audio
            await rig.wait(lambda: app.amp_audio)
            assert rig.calls == [("ConnectProfile", "amp")], rig.calls
            await rig.wait(lambda: (rig.api_node.audio.get("amp") or {}).get("audio") is True)
            st, state = await c.req("GET", "/state")
            assert state["amp"]["audio"] is True and state["amp"]["last_result"]["ok"], state["amp"]
            st, v = await c.req("GET", "/verify")
            assert {x["id"]: x for x in v["checks"]}["amp_audio"]["ok"] is True, v

            # ConnectProfile says yes but no transport follows: the full reconnect
            rig.calls.clear()
            mode["profile"] = "nothing"
            transport_down()
            assert not app.amp_audio
            await rig.wait(lambda: app.amp_audio)
            assert rig.calls == [("ConnectProfile", "amp"), ("fix_metadata", "amp")], rig.calls

            # review 6: a failed call doesn't sit out the wait for nothing
            speakerd_main.AMP_AUDIO_WAIT_S = 3
            rig.calls.clear()
            mode["profile"] = "fail"
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            transport_down()
            await rig.wait(lambda: app.amp_audio)
            assert loop.time() - t0 < 1.5, "waited after a failed ConnectProfile"
            assert rig.calls == [("ConnectProfile", "amp"), ("fix_metadata", "amp")], rig.calls
            speakerd_main.AMP_AUDIO_WAIT_S = 0.2

            # review 1: the grace counts from the latest connect
            speakerd_main.AMP_AUDIO_GRACE_S = 0.4
            mode["profile"] = "audio"
            rig.calls.clear()
            transport_down()
            await asyncio.sleep(0.25)
            reconnect_edge()  # restarts the grace: nothing before ~0.65 s
            await asyncio.sleep(0.25)
            assert rig.calls == [], rig.calls
            await rig.wait(lambda: app.amp_audio)
            assert rig.calls == [("ConnectProfile", "amp")], rig.calls
            speakerd_main.AMP_AUDIO_GRACE_S = 0.05

            # review 2: after giving up, a slow retry of ConnectProfile alone
            mode["profile"], mode["fix"] = "nothing", False
            rig.calls.clear()
            transport_down()
            await rig.wait(lambda: rig.calls.count(("ConnectProfile", "amp")) >= 3)
            assert rig.calls[:3] == [("ConnectProfile", "amp"), ("fix_metadata", "amp"),
                                     ("fix_metadata", "amp")], rig.calls
            assert app.last_result("amp")["ok"] is False
            assert rig.calls[3:] and set(rig.calls[3:]) == {("ConnectProfile", "amp")}, rig.calls
            mode["profile"] = "audio"
            await rig.wait(lambda: app.amp_audio)
            await rig.wait(lambda: app.last_result("amp")["ok"] is True)
            n = len(rig.calls)
            await asyncio.sleep(0.3)
            assert len(rig.calls) == n, "the slow retry must stop once the audio is back"

            # review 5: a late State for a transport already removed doesn't
            # bring it back
            transport_down()
            eng._on_props_changed(fd, "org.bluez.MediaTransport1",
                                  {"State": Variant("s", "active")})
            assert fd not in eng._transports and not app.amp_audio
            await rig.wait(lambda: app.amp_audio)  # and the restore still runs

            # review 3: device calls go to the path the amp is connected on
            other = dev_path.replace("/hci0/", "/hci1/")
            eng._dev_paths[AMP] = {dev_path: False, other: True}
            assert eng._call_path(AMP) == other
            eng._dev_paths[AMP] = {other: False}
            assert eng._call_path(AMP) == dev_path, "disconnected: our own adapter"
            eng._dev_paths[AMP] = {dev_path: True}

            # auto-reconnect switched off: the audio link is left alone
            app.set_auto_reconnect(False)
            rig.calls.clear()
            transport_down()
            await asyncio.sleep(0.3)
            assert not app.amp_audio and rig.calls == [], rig.calls
            st, v = await c.req("GET", "/verify")
            chk = {x["id"]: x for x in v["checks"]}["amp_audio"]
            assert chk["ok"] is False and "nowhere to play" in chk["detail"], chk
        finally:
            for k, v in saved.items():
                setattr(speakerd_main, k, v)
    await with_rig(tmp, body)
    print("amp_audio_restore: PASS")


async def test_v004(tmp):
    """0.0.4: re-claim with a planted token, claimed_by limits, the installed
    version, and POST /audio/restart."""
    log = os.path.join(tmp, "systemctl.log")
    os.environ["FAKE_SYSTEMCTL_LOG"] = log

    async def body(rig, c):
        import dataclasses
        app, eng = rig.app, rig.app.engine

        # claimed_by and area: at most 255 characters, no control characters
        for bad in ({"claimed_by": "ha\nevil"}, {"area": "x\x7f"}, {"claimed_by": "h" * 256},
                    {"claimed_by": "ha\u0085x"}, {"area": "a\u2028b"}):
            st, err = await c.req("POST", "/claim", bad, token=None)
            assert st == 400 and err["error"] == "bad_request", (bad, err)
        await c.claim()
        old = c.token

        # re-claim: refused without a planted token, or with the wrong bearer
        st, err = await c.req("POST", "/claim", {}, token=None)
        assert st == 409 and err["error"] == "already_claimed", err
        bearer, h = raw_and_hash()
        os.makedirs(rig.cfg.claim_dir, exist_ok=True)
        planted = os.path.join(rig.cfg.claim_dir, "claim-token")
        with open(planted, "w") as f:
            f.write(h)
        st, err = await c.req("POST", "/claim", {}, token="cd" * 32)
        assert st == 401 and err["error"] == "unauthorized", err
        # review 4: a plant that can't be deleted is never left as a standing key
        os.chmod(rig.cfg.claim_dir, 0o500)
        try:
            st, err = await c.req("POST", "/claim", {}, token=bearer)
            assert st == 502 and err["error"] == "failed", err
        finally:
            os.chmod(rig.cfg.claim_dir, 0o700)
        st, _ = await c.req("GET", "/state", token=old)
        assert st == 200, "a failed re-claim must leave the old claim in place"
        # a stream opened under the old token ends at the re-claim
        url = f"https://127.0.0.1:{rig.cfg.https_port}/api/v2/events"
        async with c.s.get(url, headers={"Authorization": f"Bearer {old}"}, ssl=False) as r:
            await r.content.readuntil(b"\n\n")  # the first state event
            st, res = await c.req("POST", "/claim", {"claimed_by": "rebuilt-ha"}, token=bearer)
            assert st == 200 and res["token"] != old, res
            await asyncio.wait_for(r.content.read(), 2)  # ends, rather than pinging on
        assert not os.path.exists(planted), "a used planted token must be deleted"
        st, err = await c.req("GET", "/state", token=old)
        assert st == 401, err
        c.token = res["token"]
        st, info = await c.req("GET", "/info")
        assert info["state"] == "claimed" and info["claimed_by"] == "rebuilt-ha", info

        # the installed version survives a failed or refused run after it
        sysm = rig.api_node.system
        for rec, want in (({"state": "failed", "version": "0.0.9", "installed": "0.0.3"}, "0.0.3"),
                          ({"state": "done", "version": "0.0.4", "installed": None}, "0.0.4"),
                          ({"state": "failed", "version": "0.0.4", "installed": "0.0.3\n"},
                           "0.0.0")):
            with open(rig.cfg.install_json, "w") as f:
                json.dump(rec, f)
            assert sysm.installed_version() == want, (rec, sysm.installed_version())

        # POST /audio/restart: open, even on a claimed node
        saved = speakerd_main.AUDIO_SETTLE_S, speakerd_main.AMP_AUDIO_WAIT_S
        saved_grace = speakerd_main.AMP_AUDIO_GRACE_S
        saved_cooldown = speakerd_main.AUDIO_RESTART_COOLDOWN_S
        speakerd_main.AUDIO_RESTART_COOLDOWN_S = 0
        speakerd_main.AUDIO_SETTLE_S, speakerd_main.AMP_AUDIO_WAIT_S = 0.3, 0.2
        app.cfg = dataclasses.replace(app.cfg, amp_reconnect_retry_delay_s=0.01)
        dev_path = eng.device_path(AMP)
        fd = dev_path + "/sep1/fd0"

        def transport(up):
            if up:
                eng._on_interfaces_added(fd, {"org.bluez.MediaTransport1":
                                              {"State": Variant("s", "idle")}})
            else:
                eng._on_interfaces_removed(fd, ["org.bluez.MediaTransport1"])

        def connected(on):
            eng._dev_paths[AMP] = {dev_path: on}
            eng._publish_device(AMP)

        fail_profile = {"n": 0}  # this many ConnectProfile calls fail first

        async def connect_amp_audio():
            rig.calls.append(("ConnectProfile", "amp"))
            if fail_profile["n"] > 0:
                fail_profile["n"] -= 1
                return False, "org.bluez.Error.Failed"
            transport(True)
            return True, None

        async def fix_metadata():
            rig.calls.append(("fix_metadata", "amp"))
            transport(True)
            return True, None

        async def connect_device(slug):
            rig.calls.append(("Connect", slug))
            connected(True)
            transport(True)
            return True, None

        eng.connect_amp_audio = connect_amp_audio
        eng.connect_device = connect_device
        eng.fix_metadata = fix_metadata

        async def restart():
            st, res = await c.req("POST", "/audio/restart", token=None)
            assert st == 202, res
            await rig.wait(lambda: not app.audio_restart["running"])
            await rig.wait(lambda: rig.api_node.build_state()["node"]["audio_restart"]
                           == app.audio_restart)
            return app.audio_restart["last_result"]

        try:
            rig.node.roster.set_amp(AMP, "Amp")
            rig.add_device(AMP, "Amp", paired=True, connected=True)
            app.set_auto_reconnect(False)  # only the restart may act here
            connected(True)
            transport(False)  # what the restart does to the audio link
            rig.calls.clear()
            res = await restart()
            assert res["ok"] and res["error"] is None, res
            assert app.amp_audio and rig.calls == [("ConnectProfile", "amp")], rig.calls
            with open(log) as f:
                assert ("--user restart pipewire.service wireplumber.service "
                        "shairport-sync.service") in f.read()
            st, info = await c.req("GET", "/info")
            assert info["amp"]["audio"] is True, info

            # one at a time
            transport(False)
            st, res = await c.req("POST", "/audio/restart", token=None)
            assert st == 202, res
            st, err = await c.req("POST", "/audio/restart", token=None)
            assert st == 409 and err["error"] == "busy", err
            await rig.wait(lambda: not app.audio_restart["running"])

            # a 60 s cooldown after one ends (shortened here)
            transport(False)
            await restart()
            speakerd_main.AUDIO_RESTART_COOLDOWN_S = 0.5  # counts from the end just now
            st, err = await c.req("POST", "/audio/restart", token=None)
            assert st == 409 and err["error"] == "busy" and "try again" in err["message"], err
            await asyncio.sleep(0.6)
            transport(False)
            assert (await restart())["ok"]
            speakerd_main.AUDIO_RESTART_COOLDOWN_S = 0

            # the amp dropped along with it: connected again
            transport(False)
            connected(False)
            rig.calls.clear()
            res = await restart()
            assert res["ok"] and rig.calls == [("Connect", "amp")], rig.calls

            # a deliberate disconnect still wins
            await app.amp_command("disconnect")
            transport(False)
            connected(False)
            rig.calls.clear()
            res = await restart()
            assert res["ok"] and rig.calls == [], rig.calls

            # review 1: with auto-reconnect on, the automatic restore holds off
            # while the restart runs, so only one loop acts on the amp
            await app.amp_command("connect")
            transport(True)
            speakerd_main.AMP_AUDIO_GRACE_S = 0.05
            app.set_auto_reconnect(True)
            fail_profile["n"] = 1
            transport(False)
            rig.calls.clear()
            res = await restart()
            assert res["ok"], res
            assert rig.calls == [("ConnectProfile", "amp"), ("fix_metadata", "amp")], rig.calls
            # and it re-arms afterwards
            rig.calls.clear()
            transport(False)
            await rig.wait(lambda: app.amp_audio)
            assert rig.calls == [("ConnectProfile", "amp")], rig.calls
            app.set_auto_reconnect(False)

            # review 2: systemctl refused (shairport-sync wouldn't start), but the
            # amp's audio link is still brought back
            os.environ["FAKE_SYSTEMCTL_FAIL"] = "1"
            transport(False)
            rig.calls.clear()
            res = await restart()
            assert not res["ok"] and res["error"].startswith("restart failed"), res
            assert app.amp_audio and rig.calls == [("ConnectProfile", "amp")], rig.calls
            os.environ.pop("FAKE_SYSTEMCTL_FAIL")

            # review 6: an unexpected error still ends the run with a result
            async def boom():
                raise RuntimeError("boom")
            eng.connect_amp_audio = boom
            transport(False)
            res = await restart()
            assert not res["ok"] and "internal error: boom" in res["error"], res
            assert not app.audio_restart["running"]
            st, state = await c.req("GET", "/state")
            assert state["node"]["audio_restart"]["running"] is False, state["node"]
        finally:
            os.environ.pop("FAKE_SYSTEMCTL_FAIL", None)
            speakerd_main.AUDIO_SETTLE_S, speakerd_main.AMP_AUDIO_WAIT_S = saved
            speakerd_main.AMP_AUDIO_GRACE_S = saved_grace
            speakerd_main.AUDIO_RESTART_COOLDOWN_S = saved_cooldown
    try:
        await with_rig(tmp, body)
    finally:
        os.environ["FAKE_SYSTEMCTL_LOG"] = os.devnull
    print("v004: PASS")


async def test_reset_request(tmp):
    rig = Rig(tmp)
    node = rig.api_node
    node.claims.claim("ha", None)
    assert node.claims.claimed
    os.makedirs(rig.cfg.state_dir, exist_ok=True)
    open(rig.cfg.reset_request, "w").close()
    await node.start()
    assert not node.claims.claimed and not os.path.exists(rig.cfg.reset_request)
    await node.stop()
    print("reset_request: PASS")


async def test_control_protocol(tmp):
    rig = Rig(tmp)
    await rig.control.start()
    try:
        reader, writer = await asyncio.open_unix_connection(rig.sock)
        writer.write(b"not json\n")
        writer.write(b'{"id": 1, "method": "no.such"}\n')
        writer.write(b'{"id": 2, "method": "amp.found"}\n')
        replies = {}
        while len(replies) < 2:
            msg = json.loads(await asyncio.wait_for(reader.readline(), 5))
            replies[msg["id"]] = msg
        assert replies[1]["error"]["status"] == 404, replies
        assert replies[2]["result"] == {"scanning": False, "devices": []}, replies
        writer.close()
    finally:
        await rig.control.stop()
    print("control_protocol: PASS")


async def test_wifi_input(tmp):
    assert parse("ssid=Home Network\npassword=pa ss=word\n") == ("Home Network", "pa ss=word", False)
    assert parse("﻿# comment\r\nssid=Cafe\r\n\r\nhidden=yes\r\n") == ("Cafe", None, True)
    for bad, why in (("password=12345678\n", "ssid"), ("ssid=x\npassword=short\n", "password"),
                     ("ssid=x\nssid=y\n", "twice"), ("ssid=x\npsk=12345678\n", "unexpected"),
                     ("ssid=" + "x" * 33 + "\n", "ssid"), ("ssid=x\nhidden=maybe\n", "hidden")):
        try:
            parse(bad)
        except InputError as e:
            assert why in str(e), (bad, e)
        else:
            raise AssertionError(f"accepted {bad!r}")
    s = wifi_settings("Net", None, True)
    assert "802-11-wireless-security" not in s and s["802-11-wireless"]["hidden"].value is True
    s = wifi_settings("Net", "12345678", False)
    assert s["802-11-wireless-security"]["psk"].value == "12345678"
    assert s["connection"]["id"].value == "NowAirPlaying Net"
    assert "autoconnect-priority" not in s["connection"], "added alongside, not preferred"
    print("wifi_input: PASS")


async def main():
    with tempfile.TemporaryDirectory() as fake_bin:
        os.symlink(os.path.join(HERE, "fake-systemctl.sh"), os.path.join(fake_bin, "systemctl"))
        os.environ["PATH"] = fake_bin + os.pathsep + os.environ["PATH"]
        os.environ.setdefault("FAKE_SYSTEMCTL_LOG", os.devnull)
        for test in (test_unclaimed_and_https_rules, test_planted_claim, test_claimed_auth,
                     test_sse, test_phones_and_pairing, test_speakerd_away, test_verify_merge,
                     test_agent_rejects, test_update, test_review_fixes,
                     test_amp_audio_restore, test_v004, test_reset_request,
                     test_control_protocol, test_wifi_input):
            with tempfile.TemporaryDirectory() as tmp:
                await test(tmp)
            os.environ.pop("FAKE_SYSTEMCTL_FAIL", None)

asyncio.run(main())
