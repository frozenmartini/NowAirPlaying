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

## Supported hardware

- **Amplifier:** the Kohler amplifier in the Anthem+ system, which is the only
  tested target.
- **Pi:** Raspberry Pi 4 or 5, running Raspberry Pi OS Lite 64-bit (Debian 12
  bookworm). Smaller boards are untested.

Other Bluetooth screens or speakers that act as an AVRCP 1.4+ controller *may*
work, but they are untested and unsupported. Reports are welcome.

## How it works

```
iPhone ──AirPlay 2──▶ Pi (shairport-sync + nqptp) ──▶ PipeWire ──A2DP──▶ Kohler amp
                         │                                               ▲
                         └── metadata ──▶ speakerd ──AVRCP (BlueZ Media1)─┘
```

- **shairport-sync** (built with AirPlay 2) and **nqptp** receive the stream.
- **PipeWire 1.4+** sends the audio to the amplifier over Bluetooth A2DP. Older
  PipeWire accepts volume from the source but never applies it.
- **BlueZ 5.87** carries the metadata. BlueZ 5.66, which Debian ships, ignores the
  `[AVRCP]` settings the Kohler needs.
- **speakerd**, the Python daemon in this repo, registers **one permanent**
  AVRCP player with BlueZ and only ever updates its properties. The Kohler
  controller predates AVRCP 1.4 and goes silent if a player is swapped out
  mid-session, so it never is.
- **Home Assistant is optional.** speakerd can publish the node's state and
  controls over MQTT, with auto-discovery.

Bluetooth streaming from a phone (for example Android, which has no AirPlay) is an
optional add-on. AirPlay needs no pairing between the phone and the Pi.

## Trademarks

AirPlay is a trademark of Apple Inc. Kohler and Anthem+ are trademarks of Kohler
Co. This project is independent and is not affiliated with, endorsed by, or
sponsored by Apple or Kohler.

## License

MIT, see [LICENSE](LICENSE). The published image will also contain third-party
software under its own licenses, including GPL components such as BlueZ and the
Linux kernel. Each image release will say where to get their source.
