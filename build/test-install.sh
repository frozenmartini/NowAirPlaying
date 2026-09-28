#!/bin/sh
# Install the built packages plus the vendored PipeWire/WirePlumber into a
# throwaway chroot that starts like Pi OS Lite (Debian's bluez 5.66 +
# pi-bluetooth + avahi installed; NO backports source, as on a real node) and
# check that apt swaps bluez out cleanly, no file goes missing and the audio
# stack is the tested one. Nothing is started.
#
# Usage: sudo build/test-install.sh     (after build/build.sh and build/vendor.sh)
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/out"
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
ls "$OUT"/debs/*.deb >/dev/null 2>&1 || { echo "no packages: run build/build.sh" >&2; exit 1; }
ls "$OUT"/vendor/*.deb >/dev/null 2>&1 || { echo "no vendored packages: run build/vendor.sh" >&2; exit 1; }
PI_KEY=/usr/share/keyrings/raspberrypi-archive-keyring.gpg

cat > "$OUT/check-install.sh" <<'EOF'
#!/bin/sh
set -eu
fail() { echo "FAIL: $*" >&2; exit 1; }
echo "== before: $(dpkg-query -W -f='${Package} ${Version}\n' bluez pi-bluetooth | tr '\n' ' ')"
apt-get install -y --no-install-recommends /debs/*.deb /vendor/*.deb
echo "== after:"
dpkg-query -W -f='${Package} ${Version} ${db:Status-Abbrev}\n' \
    nowairplaying-bluez nowairplaying-nqptp nowairplaying-shairport-sync pi-bluetooth
dpkg-query -W -f='${db:Status-Abbrev}' bluez 2>/dev/null | grep -q '^ii' \
    && fail "Debian's bluez is still installed"
dpkg-query -W -f='${db:Status-Abbrev}' pi-bluetooth | grep -q '^ii' \
    || fail "pi-bluetooth was removed"
# every file our packages own must exist on disk (usrmerge file loss check)
for p in nowairplaying-bluez nowairplaying-nqptp nowairplaying-shairport-sync; do
    dpkg -L "$p" | while read -r f; do
        [ -e "$f" ] || [ -L "$f" ] || fail "$p owns $f but it is missing"
    done
done
[ "$(/usr/libexec/bluetooth/bluetoothd --version)" = 5.87 ] || fail "bluetoothd is not 5.87"
[ "$(readlink -f "$(command -v bluetoothd)")" = /usr/libexec/bluetooth/bluetoothd ] \
    || fail "command -v bluetoothd does not lead to 5.87"
dpkg -S /usr/libexec/bluetooth/bluetoothd | grep -q '^nowairplaying-bluez:' \
    || fail "dpkg does not attribute bluetoothd to nowairplaying-bluez"
[ -z "$(ls /etc/systemd/system/bluetooth.service.d 2>/dev/null)" ] || fail "a bluetooth drop-in exists"
[ ! -e /usr/lib/systemd/user/mpris-proxy.service ] || fail "mpris-proxy user unit present"
systemctl is-enabled bluetooth.service nqptp.service
v=$(shairport-sync -V 2>&1)
echo "shairport-sync -V: $v"
case "$v" in *-AirPlay2-*-PipeWire-*-dbus-*) ;; *) fail "unexpected shairport build: $v" ;; esac
case "$v" in *-mqtt-*|*-mpris-*) fail "MQTT or MPRIS compiled in" ;; esac
ldd /usr/bin/shairport-sync /usr/libexec/bluetooth/bluetoothd /usr/bin/nqptp \
    | grep 'not found' && fail "unresolved shared libraries"
# the audio stack: vendored 1.4.2 / 0.5.8, never Pi OS's 1.2.7, and nothing
# else dragged in from backports
for p in pipewire pipewire-bin libpipewire-0.3-0 libspa-0.2-bluetooth wireplumber; do
    v=$(dpkg-query -W -f='${Version}' "$p")
    case "$v" in *~bpo12*) ;; *) fail "$p is $v, not the vendored backport" ;; esac
done
dpkg --compare-versions "$(dpkg-query -W -f='${Version}' pipewire)" ge 1.4 || fail "pipewire < 1.4"
dpkg --compare-versions "$(dpkg-query -W -f='${Version}' wireplumber)" ge 0.5.8 || fail "wireplumber < 0.5.8"
pipewire --version
lib=$(ldd /usr/bin/shairport-sync | awk '/libpipewire-0.3/ {print $3}')
dpkg -S "$(readlink -f "$lib")" | grep -q '^libpipewire-0.3-0' || fail "shairport links an unexpected libpipewire: $lib"
# (the Pi archive's own rebuilds carry ~bpo12+rpt versions and are normal Pi OS)
dpkg-query -W -f='${Package} ${Version}\n' | grep '~bpo12' | grep -v '+rpt' | grep -vE '^(pipewire|pipewire-bin|libpipewire-0.3-0|libpipewire-0.3-modules|libspa-0.2-modules|libspa-0.2-bluetooth|wireplumber|libwireplumber-0.5-0) ' \
    && fail "unexpected backports packages installed (above)"
systemctl --global is-enabled pipewire.socket wireplumber.service || true
echo "== PASS"
EOF

mmdebstrap --variant=apt --arch=arm64 --mode=root \
    --keyring=/usr/share/keyrings/debian-archive-keyring.gpg --keyring="$PI_KEY" \
    --include="systemd,systemd-sysv,dbus,udev,bluez,pi-bluetooth,avahi-daemon,libpipewire-0.3-0" \
    --customize-hook='mkdir -p "$1/debs"' \
    --customize-hook="sync-in $OUT/debs /debs" \
    --customize-hook='mkdir -p "$1/vendor"' \
    --customize-hook="sync-in $OUT/vendor /vendor" \
    --customize-hook="copy-in $OUT/check-install.sh /" \
    --customize-hook='chroot "$1" sh /check-install.sh' \
    bookworm /dev/null \
    "deb http://deb.debian.org/debian bookworm main" \
    "deb http://deb.debian.org/debian bookworm-updates main" \
    "deb http://deb.debian.org/debian-security bookworm-security main" \
    "deb http://archive.raspberrypi.com/debian bookworm main" \
    2>&1 | tee "$OUT/test-install.log"
grep -q '^== PASS' "$OUT/test-install.log" || { echo "INSTALL TEST FAILED, see $OUT/test-install.log" >&2; exit 1; }
chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$OUT"
