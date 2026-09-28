# Roadmap

The order runs from the cheapest win to the most work: a working installer first,
then guided setup, then the flashable image. The image is generated from the
installer's recipe, so the recipe stays the single source of truth.

## 1. The installer

`install/install.sh` runs on a fresh Raspberry Pi OS Lite 64-bit, trixie or bookworm.

**Status:** on 2026-09-28 it ran end to end on stock trixie Lite on a Pi 4:
- a fresh install, a re-run that changed nothing, and a reboot with nobody logged in;
- pairing the Kohler, then `--amp-mac`: every check passed;
- by ear: sound, phone volume and the amp's screen all work.

The bookworm path has not been run on a real Pi yet.

It has six phases, and each one checks before it acts, so a re-run is safe and a failed run
resumes where it stopped. `--verify` runs phase 6 alone. The log goes to
`/var/log/nowairplaying-install.log`.

| Phase | What it does |
|---|---|
| 1. Preflight | Checks the Pi model, `arm64`, bookworm or trixie, and the Lite edition. On bookworm it also checks that the vendored packages match `build/vendor.lock`. Refuses clearly if anything is wrong. |
| 2. Apt | Installs PipeWire 1.4.2 and WirePlumber 0.5.8, plus `dbus-user-session`, speakerd's Python libraries and mDNS. **Trixie:** from the archive. **Bookworm:** the vendored backports packages (`build/vendor.sh`) from local files, without enabling backports, and held so an upgrade can't bring back the Pi archive's 1.2.7. |
| 3. Install | Installs our packages from `build/` and holds them: `nowairplaying-nqptp` (1.2.8) and `nowairplaying-shairport-sync` (5.2.2, AirPlay 2). **Bookworm only:** also `nowairplaying-bluez` (5.87, replaces Debian's `bluez`). Trixie keeps Pi OS's BlueZ 5.82. |
| 4. Units | Installs speakerd in `/opt/nowairplaying`, the speakerd and shairport-sync user units, and enables linger. Masks `mpris-proxy`, which Debian's bluez enables for every user. |
| 5. Configure | Writes `/etc/bluetooth/main.conf` `[AVRCP]`, unblocks any Bluetooth radio that rfkill soft-blocked (Pi OS can start that way), and writes the WirePlumber override and the speakerd and shairport-sync configs. |
| 6. Verify | Checks the **running** system, not the installed packages. |

Every apt step is simulated first. If apt wants to remove any package other than
Debian's `bluez` on bookworm, the installer stops before changing anything.

The WirePlumber override does two things. It turns off the dummy AVRCP player, and
it turns off seat monitoring: WirePlumber 0.5 starts its Bluetooth monitor only
while the user's seat is active, and on a headless node nobody logs in.

speakerd stays off until the amplifier's address is known (`--amp-mac`), because
there is nothing for it to do before the amplifier is paired.

Phase 6 must catch these, because each of them fails silently:

- The running `bluetoothd` is 5.87 on bookworm, or at least 5.82 on trixie. Debian's
  5.66 ignores the `[AVRCP]` settings, and volume has no path.
- The Bluetooth adapter is powered, which it can't be while rfkill blocks it.
- `shairport-sync -V` contains `AirPlay2`. Otherwise it's classic AirPlay 1.
- `pipewire --version` is ≥ 1.4.
- `nqptp` is active before shairport-sync starts.
- `avahi-daemon` is running and `mdns4_minimal` is in `/etc/nsswitch.conf`.
- WirePlumber's `bluez5.dummy-avrcp-player` is off, and `mpris-proxy` is not running.
  Either one adds a second player next to speakerd's.
- Exactly one player is registered on the amp's adapter, and it is speakerd's
  (`/org/speakerd/player`). The system bus refuses to list another program's
  objects, even to root, so the installer checks this indirectly. It rules out
  each other source of a player (the checks above and below) and finds speakerd's
  registration in its log since its last start.
- `org.gnome.ShairportSync` is owned on the user's session bus, and
  `org.mpris.MediaPlayer2.ShairportSync` is owned on no bus.

Some steps can't be scripted, so the installer stops and prints a checklist instead:
pairing with the amplifier, and the MQTT login if Home Assistant is used. The Kohler
uses legacy PIN pairing with the fixed PIN **0000**. An agent that offers no PIN fails
with "Authentication Failed".

## 2. Package builds

`build/` makes the three `.deb` packages. See [build/README.md](../build/README.md).

- **Clean chroot.** They are built in a throwaway Debian arm64 chroot that matches
  Pi OS Lite (Debian + the Raspberry Pi archive), so nothing from the build machine
  leaks in. `SUITE=bookworm` (the default) also adds backports for PipeWire and
  builds BlueZ 5.87. `SUITE=trixie` builds only nqptp and shairport-sync.
- **Generic ARMv8.0-A flags.** A build on a Pi 5 must never pick up `-mcpu=native`, or
  the binary dies with an illegal instruction on a Pi 4. The build stops if any
  Makefile asks for `native`.
- **Everything installs to `/usr`.** Each package takes over from the matching Debian
  package (`Provides`, `Conflicts`, `Replaces`). There is exactly one `bluetoothd`, with
  no systemd drop-in, so `command -v` and dpkg report the version that actually runs.
  BlueZ is configured with `--sysconfdir=/etc --localstatedir=/var`, so its compiled-in
  paths are the real ones.
- **Files under `/etc` are conffiles.** dpkg keeps local edits to `main.conf` on
  upgrade, instead of `make install` overwriting it.
- **No `mpris-proxy` unit.** BlueZ's `mpris-proxy.service` user unit is left out of
  the package.

shairport-sync 5.2.2 is configured with:

```
./configure --prefix=/usr --sysconfdir=/etc --with-airplay-2 --with-ssl=openssl \
    --with-avahi --with-alsa --with-pipewire --with-soxr --with-metadata \
    --with-dbus-interface
```

The PipeWire option is `--with-pipewire`. Autoconf silently ignores a misspelled
option such as `--with-pw`, and the build then quietly has no PipeWire output. The
build checks `-V` for `-AirPlay2-`, `-PipeWire-` and `-dbus-`, and fails on `-mqtt-` or
`-mpris-`.

That is the reference build minus two options:

- **No `--with-mqtt-client`.** speakerd is the node's only MQTT client.
  shairport-sync reaches speakerd over D-Bus only.
- **No `--with-mpris-interface`.** When compiled in, shairport-sync always starts an
  MPRIS player, and the config only chooses its bus. A second player next to
  speakerd's is the failure the one-player design removes.


## 3. Guided setup

The node works without Home Assistant: its own setup page pairs the amplifier. When
Home Assistant is present, its Kohler Anthem+ integration finds the node, hands it an
MQTT login, pairs it with the amplifier, and runs the verify checks. Both use one small
HTTP API on the node. The draft contract is
[SETUP-API.md](SETUP-API.md).

**Being redesigned (2026-09-28).** The first-boot setup hotspot (comitup) is
dropped. The setup now starts in Home Assistant, whose integration gives the user
what they need to flash the card. The rest of that journey is still being planned.

For Wi-Fi, the node uses Ethernet or the Wi-Fi set when the card is flashed.
Raspberry Pi Imager's Wi-Fi settings have documented failures on bookworm, and on the
trixie test card they worked.

**Proven so far,** in a simulation of what Home Assistant would do over SSH:
- HA logs in with its own key.
- The user types the Pi's password once, because the user Imager creates has no
  password-free `sudo`.
- HA starts the installer as root in its own systemd unit, so a dropped connection
  can't kill it, and follows its log.
- Pairing the Kohler worked the same way.

Whether HA drives setup over SSH or over the setup API is still open.

## 4. The image

A fat, versioned image built with `rpi-image-gen`, never cloned from a live card. It
carries the exact tested package set, including the PipeWire and WirePlumber debs
on bookworm (backports drops old versions; trixie's archive has the right ones), and
the apt holds. Releases are rebuilt deliberately
and published as GitHub releases, with a GPL source offer for each one.

## Open design questions

- **The D-Bus link is not yet tested with a real shairport-sync.** The tests run
  speakerd's link against a real D-Bus daemon and a stand-in shairport-sync with the
  same interface. The first node install must confirm, on a live AirPlay session,
  that song changes arrive and that the amp's buttons reach the iPhone.
- **The hardware floor:** untested below a Pi 4. 2.4 GHz-only boards (the Pi Zero 2 W
  and Pi 3B) also depend on the home network's band.
