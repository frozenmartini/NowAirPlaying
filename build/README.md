# Package builds

Builds the packages NowAirPlaying needs from source, as `.deb` files for Raspberry Pi
OS Lite 64-bit. On bookworm that is all three below. On trixie it is nqptp and
shairport-sync only, because Pi OS's own BlueZ 5.82 already honours `[AVRCP]` (see
[Trixie](#trixie)):

| Package | Upstream | Replaces |
|---|---|---|
| `nowairplaying-bluez` | BlueZ 5.87 | Debian `bluez` 5.66 |
| `nowairplaying-nqptp` | nqptp 1.2.8 | `nqptp` |
| `nowairplaying-shairport-sync` | shairport-sync 5.2.2, AirPlay 2 | `shairport-sync` |

```
build/fetch.sh               # download and verify the pinned sources (build/versions.env)
sudo build/build.sh          # build in a clean chroot -> build/out/debs/
sudo build/vendor.sh         # copy the pinned PipeWire/WirePlumber backports -> build/out/vendor/
sudo build/test-install.sh   # install all of it over a Pi OS-like chroot and check the result
```

## Trixie

```
build/fetch.sh
sudo SUITE=trixie PI_KEY=/path/to/raspberrypi-archive-keyring.pgp build/build.sh
                             # -> build/out/trixie/debs/, versions end in +deb13
```

- **Only nqptp and shairport-sync are built.** PipeWire 1.4.2, WirePlumber 0.5.8 and
  BlueZ 5.82 come from the trixie archive, so nothing is vendored or replaced.
- **The Pi archive signs trixie with a newer key** than the one bookworm hosts have.
  Pass it with `PI_KEY`, copied from a trixie Pi's
  `/usr/share/keyrings/raspberrypi-archive-keyring.pgp`.
- **`-std=gnu17`.** Trixie's autoconf 2.72 turns on C23, where BlueZ 5.87's `return
  false` from a pointer function is an error. The trixie build pins C to `gnu17`, which
  keeps 5.87 buildable as a fallback. Bookworm's flags are unchanged.
- `vendor.sh` and `test-install.sh` are bookworm-only.

## Vendored PipeWire and WirePlumber

PipeWire must be at least 1.4. The Pi archive's 1.2.7 accepts the phone's volume but
never applies it. The tested versions, PipeWire `1.4.2-1~bpo12+1` and WirePlumber
`0.5.8-1~bpo12+1`, come from bookworm-backports, which keeps only its newest version.
`vendor.sh` keeps copies of those exact Debian-built packages, pinned by sha256 in
[vendor.lock](vendor.lock). Every later run must match the lock, and a mismatch means
backports moved on: test the new version on a node before re-pinning.

- **Only the PipeWire/WirePlumber family** is pinned to backports, 8 packages in all.
  `-t bookworm-backports` would also have pulled systemd 254 in.
- **A node installs them from files.** It never gets backports enabled.
- **Nothing is compiled on the user's Pi.** These are ready-built Debian packages.

## Install test

`test-install.sh` starts from Debian's `bluez` 5.66, `pi-bluetooth` and `avahi-daemon`,
installs the three packages, and checks the following:
- apt removed `bluez`, and `pi-bluetooth` is still installed;
- every packaged file is on disk (the usrmerge file-loss check);
- `bluetoothd`, `command -v` and dpkg all agree on 5.87;
- no drop-in and no `mpris-proxy` unit exist;
- the units are enabled and no library is missing;
- PipeWire and WirePlumber are the vendored backports, and replace the Pi archive's
  1.2.7;
- shairport-sync links against the 1.4.2 library;
- nothing else came in from Debian backports.

It needs an arm64 host (a Pi 4 or 5 running Pi OS 64-bit), `mmdebstrap`, sudo and a
network connection. The chroot is created fresh and thrown away on every run, so the
host's own packages and `/usr/local` never reach the build. The log goes to
`build/out/build.log`.

About the CPU check: the binaries are compiled for ARMv8.0-A. A disassembly finds
one newer atomic instruction (`ldaddal`) in some of them. That is GCC's
outline-atomics helper, which checks the CPU at run time and falls back to
`ldxr`/`stlxr` on a Pi 4, so it is expected and safe.

What the build refuses to produce:
- a binary tuned for the build machine's CPU (`-march/-mcpu=native`)
- a shairport-sync without AirPlay 2, the PipeWire output or the D-Bus interface
- a shairport-sync with the MQTT client or the MPRIS interface compiled in
- a package whose files alias Debian's under usrmerge (`/lib/x` vs `/usr/lib/x`)

The packages are not signed and there is no apt repository. The installer and the
image install them with `dpkg`/`apt` from local files.
