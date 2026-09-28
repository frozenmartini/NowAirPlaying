"""ShairportLink against a real D-Bus daemon (a private one, not the user's).

A fake shairport-sync exports the two interfaces speakerd uses, with the same
names, path and property types as shairport-sync 5.2.2's
org.gnome.ShairportSync.xml. The test walks the lifecycle a node sees:
speakerd up first, shairport arrives, a track plays, an amp button press,
shairport restarts mid-song, shairport goes away.

Needs dbus-daemon (package dbus) and dbus-next. Run:
    python3 tests/test_shairport_dbus.py
"""
import asyncio, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dbus_next import Variant
from dbus_next.aio import MessageBus
from dbus_next.constants import PropertyAccess
from dbus_next.service import ServiceInterface, dbus_property, method

from speakerd.shairport import IF_MAIN, IF_RC, SS_NAME, SS_PATH, ShairportLink


class FakeMain(ServiceInterface):
    def __init__(self):
        super().__init__(IF_MAIN)
        self.active = False

    @dbus_property(access=PropertyAccess.READ)
    def Active(self) -> "b":
        return self.active


class FakeRemote(ServiceInterface):
    def __init__(self):
        super().__init__(IF_RC)
        self.state = "Not Available"
        self.client = ""
        self.md = {}
        self.calls = []

    @dbus_property(access=PropertyAccess.READ)
    def PlayerState(self) -> "s":
        return self.state

    @dbus_property(access=PropertyAccess.READ)
    def ClientName(self) -> "s":
        return self.client

    @dbus_property(access=PropertyAccess.READ)
    def Metadata(self) -> "a{sv}":
        return self.md

    @method()
    def Next(self):
        self.calls.append("Next")

    @method()
    def PlayPause(self):
        self.calls.append("PlayPause")


class FakeShairport:
    async def up(self, address, playing=False):
        self.main, self.rc = FakeMain(), FakeRemote()
        if playing:
            self.main.active, self.rc.state = True, "Playing"
            self.rc.md = {"xesam:title": Variant("s", "Resumed"),
                          "xesam:artist": Variant("as", ["After Restart"])}
        self.bus = await MessageBus(bus_address=address).connect()
        self.bus.export(SS_PATH, self.main)
        self.bus.export(SS_PATH, self.rc)
        await self.bus.request_name(SS_NAME)
        return self

    def play(self, title, artist):
        self.main.active = True
        self.main.emit_properties_changed({"Active": True})
        self.rc.state, self.rc.client = "Playing", "Test iPhone"
        self.rc.md = {"xesam:title": Variant("s", title),
                      "xesam:artist": Variant("as", [artist]),
                      "mpris:length": Variant("x", 200_000_000)}
        self.rc.emit_properties_changed({"PlayerState": "Playing",
                                         "ClientName": "Test iPhone",
                                         "Metadata": self.rc.md})

    def down(self):
        self.bus.disconnect()


async def wait_for(pred, what, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def main(address):
    snaps = []
    link = ShairportLink(snaps.append, bus_address=address, retry_s=0.2)
    last = lambda: snaps[-1] if snaps else {}
    await link.start()

    # speakerd first, no shairport yet
    await wait_for(lambda: snaps and last()["present"] is False, "initial absent")
    assert link.command("next") is False  # nobody to send it to
    print("absent_at_start: PASS")

    # shairport arrives idle
    ss = await FakeShairport().up(address)
    await wait_for(lambda: last().get("present"), "shairport present")
    assert last()["active"] is False and last()["player_state"] == "Not Available"
    print("arrival_resync: PASS")

    # a track plays: pushed via PropertiesChanged, no polling
    ss.play("Song A", "Artist A")
    await wait_for(lambda: last()["metadata"].get("xesam:title") == "Song A", "Song A")
    s = last()
    assert s["active"] and s["player_state"] == "Playing", s
    assert s["metadata"]["xesam:artist"] == ["Artist A"], s
    assert s["client_name"] == "Test iPhone", s
    print("properties_changed: PASS")

    # an amp button press reaches shairport's RemoteControl
    assert link.command("next") is True and link.command("playpause") is True
    await wait_for(lambda: ss.rc.calls == ["Next", "PlayPause"], "remote calls")
    assert link.command("bogus") is False
    print("remote_command: PASS")

    # shairport restarts mid-song: absent, then a fresh resync of the new owner
    ss.down()
    await wait_for(lambda: last()["present"] is False, "shairport gone")
    ss2 = await FakeShairport().up(address, playing=True)
    await wait_for(lambda: last()["metadata"].get("xesam:title") == "Resumed", "resync")
    assert last()["present"] and last()["player_state"] == "Playing"
    print("restart_resync: PASS")

    # shairport goes away for good
    ss2.down()
    await wait_for(lambda: last()["present"] is False, "shairport gone again")
    print("final_absent: PASS")

    await link.stop()


if __name__ == "__main__":
    daemon = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE, text=True)
    try:
        address = daemon.stdout.readline().strip()
        asyncio.run(main(address))
    finally:
        daemon.terminate()
        daemon.wait()
