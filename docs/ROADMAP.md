# Roadmap

The order runs from the cheapest win to the most work: a working installer first,
then guided setup, then the flashable image. The image is generated from the
installer's recipe, so the recipe stays the single source of truth.

## 1. The installer (next)

`install/install.sh` runs on a fresh Raspberry Pi OS Lite 64-bit (bookworm). It has
six phases, and each one checks before it acts, so a re-run is safe and a failed run
resumes where it stopped.

| Phase | What it does |
|---|---|
| 1. Preflight | Checks the Pi model, `arm64` and bookworm. Refuses clearly if any is wrong. |
| 2. Apt | Enables `bookworm-backports`, installs PipeWire ≥ 1.4 and WirePlumber ≥ 0.5.8 from it, and places apt holds so an upgrade can't pull them back to 1.2.7. |
| 3. Install | Installs the three packages built from source: BlueZ 5.87, shairport-sync 5.2.2 (AirPlay 2) and nqptp 1.2.8. |
| 4. Units | Installs the systemd units and the `bluetooth.service` drop-in that runs BlueZ 5.87 instead of 5.66, and enables linger. |
| 5. Configure | Writes `/etc/bluetooth/main.conf` `[AVRCP]`, the WirePlumber override, and the speakerd and shairport-sync configs. |
| 6. Verify | Checks the **running** system, not the installed packages. |

Phase 6 must catch these, because each of them fails silently:

- `bluetoothd` runs from `/usr/local` (5.87). Otherwise volume has no path.
- `shairport-sync -V` contains `AirPlay2`. Otherwise it's classic AirPlay 1.
- `pipewire --version` is ≥ 1.4.
- `nqptp` is active before shairport-sync starts.
- `avahi-daemon` is running and `mdns4_minimal` is in `/etc/nsswitch.conf`.
- WirePlumber's `bluez5.dummy-avrcp-player` is off, and `mpris-proxy` is not running.
  Either one adds a second player next to speakerd's.

Some steps can't be scripted, so the installer stops and prints a checklist instead:
pairing with the amplifier, and the MQTT login if Home Assistant is used.

## 2. Package builds

`build/` builds `.deb` packages for BlueZ, shairport-sync and nqptp with **generic
`arm64` flags**. A build on a Pi 5 must never pick up `-mcpu=native`, or the binary
dies with an illegal instruction on a Pi 4. Known traps:

- BlueZ's `make install` overwrites `main.conf`.
- Without `--sysconfdir=/etc`, BlueZ's compiled-in config path is
  `/usr/local/etc/bluetooth`, while the daemon actually reads `/etc/bluetooth`.
- shairport-sync needs `--with-airplay-2`. Without it the build is classic-only and
  says nothing.

## 3. Guided setup

The node works without Home Assistant. When Home Assistant is present, its Kohler
Anthem+ integration will find the node, hand it an MQTT login, pair it with the
amplifier, and run the verify checks. That needs a small setup API on the node.
Its contract is not drafted yet.

For Wi-Fi, the node supports Ethernet or a first-boot setup hotspot, and
`custom.toml` for advanced users. It doesn't rely on Raspberry Pi Imager's Wi-Fi
settings, which fail on bookworm for several documented reasons.

## 4. The image

A fat, versioned image built with `rpi-image-gen`, never cloned from a live card. It
carries the exact tested package set, including the PipeWire and WirePlumber debs
(backports drops old versions) and the apt holds. Releases are rebuilt deliberately
and published as GitHub releases, with a GPL source offer for each one.

## Open design questions

- **speakerd needs an MQTT broker today, even without Home Assistant.** AirPlay
  metadata reaches speakerd only through MQTT (shairport-sync publishes, speakerd
  subscribes), and the amplifier's buttons reach shairport-sync the same way. A
  standalone node therefore shows no AirPlay metadata on the screen unless it has a
  broker. The fix is to read shairport-sync locally, over its D-Bus interface or its
  metadata pipe, and keep MQTT only for Home Assistant.
- **speakerd requires at least one `[[devices]]` phone, and an `[mqtt]` section.**
  An AirPlay-only node has neither. Both should become optional.
- **The hardware floor:** untested below a Pi 4. 2.4 GHz-only boards (the Pi Zero 2 W
  and Pi 3B) also depend on the home network's band.
