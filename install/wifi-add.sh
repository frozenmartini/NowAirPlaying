#!/bin/sh
# NowAirPlaying Wi-Fi: joins (or forgets) a network, with the password never
# on a command line or in a log. install/install.sh installs this at
# /usr/local/lib/nowairplaying/wifi-add.sh 0755 root (phase 4, "units"),
# alongside update.sh and the other root-run helpers.
#
# All the real work -- talking to NetworkManager, the connect-and-roll-back
# dance (docs/SETUP-API.md "POST /wifi") -- lives in speakerd.wifi_add. This
# is a thin root wrapper around it, with two callers:
#
#   - Home Assistant, over SSH, as root, with the sudo password as stdin's
#     own first line and the network after it:
#       sudo -S /usr/local/lib/nowairplaying/wifi-add.sh
#       <sudo password>
#       ssid=Home
#       password=...
#       hidden=yes
#   - systemd/nowairplaying-wifi.service, at boot, reading a dropped file
#     instead of stdin:
#       /usr/local/lib/nowairplaying/wifi-add.sh --file /boot/firmware/nowairplaying-wifi.txt --delete
#
# Neither path ever puts the password in argv or the process list; it's
# always read from a line, never a flag.
set -eu

exec env PYTHONPATH=/opt/nowairplaying /usr/bin/python3 -m speakerd.wifi_add "$@"
