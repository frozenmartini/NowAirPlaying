# NowAirPlaying

AirPlay 2 for the Kohler amplifier on an Anthem+ system, with the song title and
artist on the Anthem+ touchscreen.

A Raspberry Pi receives AirPlay 2 from an iPhone, iPad or Mac and streams it to the
Kohler amplifier over Bluetooth. While it plays, the Pi sends the now-playing
information to the amplifier, so the Anthem+ touchscreen shows what's playing and
its play/pause and skip buttons control the source.

> **Status: under construction. Not installable yet.** This repo is being extracted
> from a working two-node home setup. There is no installer and no image release
> yet. See [docs/ROADMAP.md](docs/ROADMAP.md).

## Hardware

**Amplifier:** the Kohler amplifier in the Anthem+ system, which is the only tested
target. Other Bluetooth screens or speakers that act as an AVRCP 1.4+ controller *may*
work, but they are untested and unsupported. Reports are welcome.

**Raspberry Pi:**

| | Board | Notes |
|---|---|---|
| **Recommended** | Raspberry Pi 4 Model B, 2 GB or more | Tested. Dual-band Wi-Fi, plenty of headroom. |
| Supported | Raspberry Pi 5 | Works, lots of headroom. |
| Untested | Raspberry Pi 3 Model B+ (1 GB) | Should work: same Wi-Fi/Bluetooth chip as the Pi 4, with 5 GHz Wi-Fi. Reports welcome. |
| Not recommended | Pi Zero 2 W, Pi 3 Model B | Wi-Fi is 2.4 GHz only and shares one antenna with Bluetooth, so streaming both at once can drop out. The Zero 2 W also has only 512 MB. |

Also needed:

- The official Raspberry Pi power supply for your board.
- A microSD card of 16 GB or more.
- Wi-Fi or Ethernet. Ethernet helps where Wi-Fi is weak.
- A spot within Bluetooth range of the amplifier. The Pi's own radio is enough; a USB
  Bluetooth adapter is only needed for extra range, for example through tiled walls.

**Operating system:** Raspberry Pi OS **Lite** 64-bit, with no desktop: Debian 13
trixie (tested end to end on a Pi 4) or Debian 12 bookworm. The Pi runs without a
screen or keyboard, and gets its Wi-Fi from Raspberry Pi Imager's settings or uses
Ethernet. A setup that needs no terminal, led by Home Assistant, is being designed
(see [ROADMAP](docs/ROADMAP.md)). The desktop edition is not supported, because it
runs a second audio session for the desktop user and uses a lot more memory.

**What it needs from the Pi,** measured on a Pi 5 during the heaviest case (lossless
AirPlay audio, resampled, re-encoded for Bluetooth):

- **CPU:** about 3% of one core on a Pi 5, including overhead. Estimated about 5–8% on
  a Pi 4 and 12–18% on a Pi 3B+.
- **Memory:** about 190 MB for the whole audio stack, about 350 MB with the operating
  system.

## How it works

```
iPhone ──AirPlay 2──▶ shairport-sync + nqptp ──▶ PipeWire ──A2DP──────────▶ Kohler amp
                            │  ▲                                              ▲  │
             song info (D-Bus) │ buttons (D-Bus)                               │  │ buttons
                            ▼  │                                              │  ▼
                          speakerd ──────── one AVRCP player (BlueZ Media1) ──┘
                            │
                            └─ MQTT to Home Assistant (optional)
```

- **shairport-sync** (built with AirPlay 2) and **nqptp** receive the stream.
- **PipeWire 1.4+** sends the audio to the amplifier over Bluetooth A2DP. Older
  PipeWire accepts volume from the source but never applies it.
- **BlueZ** carries the metadata, and needs the `[AVRCP]` settings the Kohler relies
  on. On trixie, Pi OS's own BlueZ 5.82 has them. On bookworm, Debian's 5.66 ignores
  them, so the installer replaces it with 5.87.
- **speakerd**, the Python daemon in this repo, follows shairport-sync over
  shairport-sync's own D-Bus interface: song info in, the amp's button presses
  out. It registers **one permanent** AVRCP player with BlueZ and only ever
  updates its properties. The Kohler controller predates AVRCP 1.4 and goes
  silent if a player is swapped out mid-session, so it never is.
- **Home Assistant is optional.** With MQTT configured, speakerd publishes the
  node's state and controls with auto-discovery. speakerd is the only MQTT
  client on the node, and the screen keeps working when the broker is down.

AirPlay needs no pairing between the phone and the Pi. Bluetooth streaming from a
phone (for example Android, which has no AirPlay) is planned, through the Pi's own
radio or an optional USB Bluetooth adapter. It matters because the Pi holds the amp's
only Bluetooth link, so phones can no longer connect to the amp directly.

## Trademarks

AirPlay is a trademark of Apple Inc. Kohler and Anthem+ are trademarks of Kohler
Co. This project is independent and is not affiliated with, endorsed by, or
sponsored by Apple or Kohler.

## License

MIT, see [LICENSE](LICENSE). The published image will also contain third-party
software under its own licenses, including GPL components such as BlueZ and the
Linux kernel. Each image release will say where to get their source.
