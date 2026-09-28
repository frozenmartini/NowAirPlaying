#!/bin/sh
# Vendor the exact bookworm-backports PipeWire and WirePlumber packages.
#
# Why: PipeWire >= 1.4 is REQUIRED (1.2.7, which Pi OS's own archive ships,
# receives the phone's volume but never applies it), and backports keeps only
# its newest version: the tested 1.4.2 / 0.5.8 files vanish from the mirror
# once a newer backport lands. So we keep copies, pinned by sha256 in
# build/vendor.lock, and a node installs them from files: it never needs
# backports enabled, and nothing is compiled on it.
#
# Only the PipeWire/WirePlumber family is pinned to backports (NOT `-t
# bookworm-backports`, which would also drag systemd 254 in for no reason).
# Only packages that come from backports (~bpo12) are vendored; their other
# dependencies come from bookworm main, which never loses them.
#
# First run writes build/vendor.lock; later runs must match it exactly.
# Usage: sudo build/vendor.sh      Output: build/out/vendor/*.deb
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/out"
LOCK="$HERE/vendor.lock"
PKGS="pipewire wireplumber libspa-0.2-bluetooth"
PI_KEY=/usr/share/keyrings/raspberrypi-archive-keyring.gpg

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
command -v mmdebstrap >/dev/null || { echo "apt install mmdebstrap" >&2; exit 1; }

rm -rf "$OUT/vendor" "$OUT/vendor-all"
mkdir -p "$OUT"
mmdebstrap --variant=apt --arch=arm64 --mode=root \
    --keyring=/usr/share/keyrings/debian-archive-keyring.gpg --keyring="$PI_KEY" \
    --customize-hook='cat > "$1/etc/apt/preferences.d/pipewire-backports" <<PIN
Package: pipewire* libpipewire* libspa-0.2* wireplumber libwireplumber*
Pin: release n=bookworm-backports
Pin-Priority: 990
PIN' \
    --customize-hook="chroot \"\$1\" apt-get install -y --download-only --no-install-recommends $PKGS" \
    --customize-hook="sync-out /var/cache/apt/archives $OUT/vendor-all" \
    bookworm /dev/null \
    "deb http://deb.debian.org/debian bookworm main" \
    "deb http://deb.debian.org/debian bookworm-updates main" \
    "deb http://deb.debian.org/debian-security bookworm-security main" \
    "deb http://deb.debian.org/debian bookworm-backports main" \
    "deb http://archive.raspberrypi.com/debian bookworm main" \
    > "$OUT/vendor.log" 2>&1 || { echo "mmdebstrap failed, see $OUT/vendor.log" >&2; exit 1; }

mkdir -p "$OUT/vendor"
for f in "$OUT"/vendor-all/*.deb; do
    case "$(dpkg-deb -f "$f" Version)" in
        *~bpo12*) cp "$f" "$OUT/vendor/" ;;
    esac
done
rm -rf "$OUT/vendor-all"

# the floor this whole project depends on
pw=$(dpkg-deb -f "$OUT"/vendor/pipewire_*.deb Version)
wp=$(dpkg-deb -f "$OUT"/vendor/wireplumber_*.deb Version)
dpkg --compare-versions "$pw" ge 1.4 || { echo "FATAL: pipewire $pw < 1.4" >&2; exit 1; }
dpkg --compare-versions "$wp" ge 0.5.8 || { echo "FATAL: wireplumber $wp < 0.5.8" >&2; exit 1; }

(cd "$OUT/vendor" && sha256sum -- *.deb) > "$OUT/vendor.sums"
if [ -f "$LOCK" ]; then
    if ! diff -u "$LOCK" "$OUT/vendor.sums"; then
        echo "FATAL: vendored packages differ from build/vendor.lock (above)." >&2
        echo "Backports moved on. Re-test the new versions on a node before re-pinning." >&2
        exit 1
    fi
    echo "vendor: matches build/vendor.lock"
else
    cp "$OUT/vendor.sums" "$LOCK"
    echo "vendor: pinned $(wc -l < "$LOCK") packages in build/vendor.lock"
fi
chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$OUT" "$LOCK"
echo "pipewire $pw, wireplumber $wp"
ls -1 "$OUT/vendor"
