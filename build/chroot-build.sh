#!/bin/sh
# Runs INSIDE the throwaway arm64 chroot (bookworm or trixie) that build.sh
# creates. Compiles BlueZ (bookworm only), nqptp and shairport-sync and
# packages each one as a .deb in /build/debs. Never run this on a real
# system: `make install` here writes outside DESTDIR in places
# (shairport-sync's D-Bus policy), which is harmless only because the chroot
# is thrown away.
set -eu
. /build/versions.env

# Generic ARMv8.0-A: runs on a Pi 4 (Cortex-A72) and a Pi 5 (Cortex-A76).
# A build on a Pi 5 that picks up -mcpu=native targets the A76 and dies with
# an illegal instruction on a Pi 4, possibly long after startup.
export CFLAGS="-O2 -march=armv8-a -mtune=cortex-a72"
# trixie's autoconf 2.72 turns on C23, where `false` is no longer a null
# pointer (BlueZ 5.87 returns it from a pointer function): build.sh passes
# NAP_CSTD=-std=gnu17 there. CFLAGS follow CC, so the later -std wins.
# CXXFLAGS is taken first: a C -std is not valid for C++.
export CXXFLAGS="$CFLAGS"
[ -n "${NAP_CSTD:-}" ] && CFLAGS="$CFLAGS $NAP_CSTD"
export LDFLAGS=""
JOBS=$(nproc)

WORK=/build/work
OUT=/build/debs
rm -rf "$WORK" "$OUT"
mkdir -p "$WORK" "$OUT"

no_native() {  # refuse any generated build file that asks for native tuning
    if grep -rIl --include=Makefile -E -- '-m(arch|cpu|tune)=native' . ; then
        echo "FATAL: a Makefile above asks for -march/-mcpu=native" >&2
        exit 1
    fi
}

unpack() {  # unpack <tarball> -> cd into it
    mkdir -p "$WORK/$1"
    tar -xf "/build/src/$2" -C "$WORK/$1" --strip-components=1
    cd "$WORK/$1"
}

strip_all() {  # strip ELF files under a staging root
    find "$1" -type f -exec sh -c \
        'for f; do if file -b "$f" | grep -q "^ELF"; then strip --strip-unneeded "$f"; fi; done' _ {} +
}

shlib_depends() {  # shlib_depends <stage> -> "libc6 (>= 2.34), ..."
    elfs=$(find "$1" -type f -exec sh -c \
        'for f; do if file -b "$f" | grep -q "^ELF"; then echo "$f"; fi; done' _ {} +)
    tmp=$(mktemp -d)
    mkdir -p "$tmp/debian"
    echo "Source: nap" > "$tmp/debian/control"
    # shellcheck disable=SC2086
    deps=$(cd "$tmp" && dpkg-shlibdeps -O $elfs) || {
        echo "FATAL: dpkg-shlibdeps failed" >&2; exit 1; }
    rm -rf "$tmp"
    deps=$(printf '%s\n' "$deps" | sed -n 's/^shlibs:Depends=//p')
    [ -n "$deps" ] || { echo "FATAL: no shared-library dependencies found" >&2; exit 1; }
    printf '%s\n' "$deps"
}

alias_guard() {  # alias_guard <stage> <debian package it replaces>
    # Pi OS is usr-merged: /bin, /sbin and /lib are links into /usr. dpkg only
    # hands over files whose path is SPELLED the same; if the old package
    # owns /lib/x and ours owns /usr/lib/x, removing the old package deletes
    # our file through the alias. Refuse any such pair.
    tmp=$(mktemp -d)
    (cd "$tmp" && apt-get download "$2" >/dev/null 2>&1) || {
        echo "alias_guard: $2 not in the archive, nothing to compare"; rm -rf "$tmp"; return; }
    dpkg -c "$tmp"/*.deb | awk '$1 !~ /^d/ {print $6}' | sed "s#^\./#/#" | sort -u > "$tmp/old"
    (cd "$1" && find . ! -type d ! -path './DEBIAN/*') | sed "s#^\./#/#" | sort -u > "$tmp/new"
    bad=$(awk '
        function c(p) { sub(/^\/bin\//, "/usr/bin/", p); sub(/^\/sbin\//, "/usr/sbin/", p);
                        sub(/^\/lib\//, "/usr/lib/", p); return p }
        NR == FNR { old[c($0)] = $0; next }
        (c($0) in old) && old[c($0)] != $0 { print old[c($0)] " (theirs) vs " $0 " (ours)" }
    ' "$tmp/old" "$tmp/new")
    rm -rf "$tmp"
    if [ -n "$bad" ]; then
        echo "FATAL: same file under two spellings vs $2 (usrmerge file loss):" >&2
        echo "$bad" >&2
        exit 1
    fi
    echo "alias_guard: no aliased paths vs $2"
}

package() {  # package <stage> <name> <version> <depends> <description> [control extras]
    stage=$1 name=$2 version=$3 depends=$4 desc=$5 extra=${6:-}
    mkdir -p "$stage/DEBIAN"
    size=$(du -sk --exclude=DEBIAN "$stage" | cut -f1)
    {
        echo "Package: $name"
        echo "Version: $version"
        echo "Architecture: arm64"
        echo "Maintainer: $MAINTAINER"
        echo "Installed-Size: $size"
        echo "Depends: $depends"
        [ -n "$extra" ] && printf '%s\n' "$extra"
        echo "Section: sound"
        echo "Priority: optional"
        echo "Homepage: https://github.com/frozenmartini/NowAirPlaying"
        echo "Description: $desc"
    } > "$stage/DEBIAN/control"
    # every file under /etc is a conffile: dpkg keeps local edits on upgrade
    if [ -d "$stage/etc" ]; then
        (cd "$stage" && find etc -type f | sed 's|^|/|') > "$stage/DEBIAN/conffiles"
    fi
    dpkg-deb --build --root-owner-group "$stage" "$OUT/${name}_${version}_arm64.deb"
}

# ------------------------------------------------------------------ BlueZ
# Installed to /usr as a full replacement for Debian's bluez 5.66: one
# bluetoothd on the system, no systemd drop-in, and `command -v` / dpkg tell
# the truth. 5.66 ignores the [AVRCP] settings the Kohler needs.
# trixie keeps Pi OS's own BlueZ 5.82, which has [AVRCP]: build.sh passes
# NAP_BLUEZ=0 there
if [ "${NAP_BLUEZ:-1}" = 1 ]; then
unpack bluez "bluez-$BLUEZ_VERSION.tar.xz"
# Paths follow Debian's bluez package spelling (/lib/systemd, /lib/udev), see
# alias_guard. OBEX (file transfer) is not needed and would clash with
# Debian's separate bluez-obexd package.
./configure --prefix=/usr --sysconfdir=/etc --localstatedir=/var \
    --libexecdir=/usr/libexec \
    --with-systemdsystemunitdir=/lib/systemd/system \
    --with-systemduserunitdir=/usr/lib/systemd/user \
    --with-udevdir=/lib/udev \
    --enable-deprecated --disable-cups --disable-library --disable-obex
no_native
make -j"$JOBS"
S="$WORK/stage-bluez"
make install DESTDIR="$S"
# mpris-proxy's user unit would bridge a second player onto the amp next to
# speakerd's: the one failure the whole metadata design exists to prevent
rm -f "$S/usr/lib/systemd/user/mpris-proxy.service"
# Debian's spellings for the files both packages ship: hciconfig lives in
# /bin there, and the D-Bus policy is a conffile in /etc (a stale copy would
# otherwise be left beside ours after Debian's bluez is removed)
if [ -f "$S/usr/bin/hciconfig" ]; then
    mkdir -p "$S/bin"
    mv "$S/usr/bin/hciconfig" "$S/bin/hciconfig"
fi
if [ -f "$S/usr/share/dbus-1/system.d/bluetooth.conf" ]; then
    mkdir -p "$S/etc/dbus-1/system.d"
    mv "$S/usr/share/dbus-1/system.d/bluetooth.conf" "$S/etc/dbus-1/system.d/"
    rmdir "$S/usr/share/dbus-1/system.d" 2>/dev/null || true
fi
mkdir -p "$S/usr/sbin"
ln -sf ../libexec/bluetooth/bluetoothd "$S/usr/sbin/bluetoothd"
strip_all "$S"
alias_guard "$S" bluez
mkdir -p "$S/DEBIAN"
cat > "$S/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi
# enabled either way (also inside an image build); never restarted here —
# a restart drops the amp, so the installer decides when
systemctl enable bluetooth.service >/dev/null 2>&1 || true
EOF
chmod 0755 "$S/DEBIAN/postinst"
DEPS=$(shlib_depends "$S")
package "$S" nowairplaying-bluez "$BLUEZ_VERSION-0nap$NAP_REVISION${NAP_SUFFIX:-}" \
    "$DEPS, dbus, udev, kmod" \
    "Bluetooth stack (BlueZ $BLUEZ_VERSION) for NowAirPlaying
 Upstream BlueZ $BLUEZ_VERSION, replacing Debian's bluez 5.66, which ignores
 the [AVRCP] settings the Kohler amplifier needs." \
    "Provides: bluez (= $BLUEZ_VERSION)
Conflicts: bluez
Replaces: bluez"
fi

# ------------------------------------------------------------------ nqptp
unpack nqptp "nqptp-$NQPTP_VERSION.tar.gz"
autoreconf -fi
./configure --prefix=/usr --with-systemd-startup
no_native
make -j"$JOBS"
S="$WORK/stage-nqptp"
make install DESTDIR="$S" systemdsystemunitdir=/lib/systemd/system
strip_all "$S"
alias_guard "$S" nqptp
mkdir -p "$S/DEBIAN"
cat > "$S/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
fi
systemctl enable nqptp.service >/dev/null 2>&1 || true
EOF
chmod 0755 "$S/DEBIAN/postinst"
DEPS=$(shlib_depends "$S")
package "$S" nowairplaying-nqptp "$NQPTP_VERSION-0nap$NAP_REVISION${NAP_SUFFIX:-}" \
    "$DEPS" \
    "PTP timing companion for AirPlay 2 (nqptp $NQPTP_VERSION)
 Runs as a system service; shairport-sync needs it for AirPlay 2." \
    "Provides: nqptp (= $NQPTP_VERSION)
Conflicts: nqptp
Replaces: nqptp"

# --------------------------------------------------------- shairport-sync
# Deliberately WITHOUT --with-mqtt-client (speakerd is the node's only MQTT
# client) and WITHOUT --with-mpris-interface (compiled in, it always starts an
# MPRIS player: a second player next to speakerd's). The PipeWire option is
# --with-pipewire; autoconf silently ignores a misspelt --with-pw.
unpack shairport-sync "shairport-sync-$SHAIRPORT_VERSION.tar.gz"
autoreconf -fi
./configure --prefix=/usr --sysconfdir=/etc \
    --with-airplay-2 --with-ssl=openssl --with-avahi --with-alsa \
    --with-pipewire --with-soxr --with-metadata --with-dbus-interface
no_native
make -j"$JOBS"
S="$WORK/stage-shairport"
make install DESTDIR="$S"
# config comes from NowAirPlaying's template (user unit, ~/.config); the
# system-bus D-Bus policies are for a root/system-user daemon we don't run
rm -rf "$S/etc/dbus-1" "$S/etc/shairport-sync.conf"
strip_all "$S"
alias_guard "$S" shairport-sync
version_string=$("$S/usr/bin/shairport-sync" -V 2>&1 || true)
echo "shairport-sync -V: $version_string"
case "$version_string" in
    *-AirPlay2-*) ;; *) echo "FATAL: not an AirPlay 2 build" >&2; exit 1 ;;
esac
case "$version_string" in
    *-PipeWire-*) ;; *) echo "FATAL: no PipeWire backend" >&2; exit 1 ;;
esac
case "$version_string" in
    *-dbus-*) ;; *) echo "FATAL: no D-Bus interface" >&2; exit 1 ;;
esac
case "$version_string" in
    *-mqtt-*|*-mpris-*) echo "FATAL: MQTT or MPRIS compiled in" >&2; exit 1 ;;
esac
DEPS=$(shlib_depends "$S")
package "$S" nowairplaying-shairport-sync "$SHAIRPORT_VERSION-0nap$NAP_REVISION${NAP_SUFFIX:-}" \
    "$DEPS, avahi-daemon, nowairplaying-nqptp" \
    "AirPlay 2 receiver (shairport-sync $SHAIRPORT_VERSION) for NowAirPlaying
 Built with AirPlay 2, PipeWire and the native D-Bus interface only; no MQTT
 client and no MPRIS player." \
    "Provides: shairport-sync (= $SHAIRPORT_VERSION)
Conflicts: shairport-sync
Replaces: shairport-sync"

ls -l "$OUT"
