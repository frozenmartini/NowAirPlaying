#!/bin/sh
# Build the three NowAirPlaying packages in a clean, throwaway Debian arm64
# chroot that matches Raspberry Pi OS Lite: Debian + the Raspberry Pi archive.
#   SUITE=bookworm (default): adds bookworm-backports for PipeWire 1.4.
#                             Output: build/out/debs
#   SUITE=trixie:             PipeWire 1.4 and BlueZ 5.82 (with [AVRCP]) are
#                             in the archive: builds nqptp and shairport-sync.
#                             Output: build/out/trixie/debs, versions end +deb13
#
# Why a chroot: a build machine's own /usr/local (a source-built PipeWire, an
# old BlueZ) must never leak into what a fresh node links against.
#
# Needs: an arm64 host (a Pi 4 or 5), mmdebstrap, sudo, network.
# Usage:  build/fetch.sh && sudo [SUITE=trixie] build/build.sh
# Log: build.log next to the debs folder
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/versions.env"
OUT="$HERE/out"
SUITE=${SUITE:-bookworm}
case "$SUITE" in
    bookworm) DEST=$OUT; NAP_SUFFIX=; NAP_CSTD=; NAP_BLUEZ=1 ;;
    trixie)   DEST=$OUT/trixie; NAP_SUFFIX=+deb13; NAP_CSTD=-std=gnu17; NAP_BLUEZ=0 ;;
    *) echo "SUITE must be bookworm or trixie" >&2; exit 1 ;;
esac

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
[ "$(dpkg --print-architecture)" = arm64 ] || { echo "needs an arm64 host" >&2; exit 1; }
command -v mmdebstrap >/dev/null || { echo "apt install mmdebstrap" >&2; exit 1; }
for f in "bluez-$BLUEZ_VERSION.tar.xz" "shairport-sync-$SHAIRPORT_VERSION.tar.gz" \
         "nqptp-$NQPTP_VERSION.tar.gz"; do
    [ -f "$OUT/src/$f" ] || { echo "missing $f: run build/fetch.sh first" >&2; exit 1; }
done

BUILD_DEPS="autoconf,automake,libtool,pkg-config,file,xxd,python3-docutils,systemd"
# BlueZ
BUILD_DEPS="$BUILD_DEPS,libglib2.0-dev,libdbus-1-dev,libudev-dev,libical-dev,libreadline-dev"
# shairport-sync (AirPlay 2)
BUILD_DEPS="$BUILD_DEPS,libpopt-dev,libconfig-dev,libasound2-dev,libavahi-client-dev"
BUILD_DEPS="$BUILD_DEPS,libssl-dev,libsoxr-dev,libplist-dev,libplist-utils,libsodium-dev,libgcrypt20-dev"
BUILD_DEPS="$BUILD_DEPS,libavutil-dev,libavcodec-dev,libavformat-dev,libswresample-dev,uuid-dev"

# the Pi archive's trixie suite is signed with a newer key than bookworm's:
# PI_KEY=<a trixie Pi's /usr/share/keyrings/raspberrypi-archive-keyring.pgp>
PI_KEY=${PI_KEY:-/usr/share/keyrings/raspberrypi-archive-keyring.gpg}
[ -f "$PI_KEY" ] || { echo "missing $PI_KEY (raspberrypi-archive-keyring)" >&2; exit 1; }

if [ "$SUITE" = bookworm ]; then
    PW_HOOK='chroot "$1" apt-get install -y --no-install-recommends -t bookworm-backports libpipewire-0.3-dev'
    BACKPORTS="deb http://deb.debian.org/debian bookworm-backports main"
else
    PW_HOOK='chroot "$1" apt-get install -y --no-install-recommends libpipewire-0.3-dev'
    BACKPORTS=
fi

rm -rf "$DEST/debs"
mkdir -p "$DEST"
{
    echo "== NowAirPlaying package build ($SUITE), $(date -Is)"
    # shellcheck disable=SC2086
    mmdebstrap --variant=buildd --arch=arm64 --mode=root \
        --keyring=/usr/share/keyrings/debian-archive-keyring.gpg \
        --keyring="$PI_KEY" \
        --include="$BUILD_DEPS" \
        --customize-hook="$PW_HOOK" \
        --customize-hook='mkdir -p "$1/build/src"' \
        --customize-hook="copy-in $HERE/versions.env $HERE/chroot-build.sh /build" \
        --customize-hook="sync-in $OUT/src /build/src" \
        --customize-hook="chroot \"\$1\" env NAP_SUFFIX='$NAP_SUFFIX' NAP_CSTD='$NAP_CSTD' NAP_BLUEZ='$NAP_BLUEZ' sh /build/chroot-build.sh" \
        --customize-hook="sync-out /build/debs $DEST/debs" \
        "$SUITE" /dev/null \
        "deb http://deb.debian.org/debian $SUITE main" \
        "deb http://deb.debian.org/debian $SUITE-updates main" \
        "deb http://deb.debian.org/debian-security $SUITE-security main" \
        ${BACKPORTS:+"$BACKPORTS"} \
        "deb http://archive.raspberrypi.com/debian $SUITE main"
} 2>&1 | tee "$DEST/build.log"

# tee hides mmdebstrap's exit status; the packages are the proof
ls "$DEST"/debs/*.deb >/dev/null 2>&1 || { echo "BUILD FAILED, see $DEST/build.log" >&2; exit 1; }
chown -R "${SUDO_UID:-0}:${SUDO_GID:-0}" "$OUT"
echo "== packages:"
ls -l "$DEST/debs"
