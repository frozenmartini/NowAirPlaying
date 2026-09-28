#!/bin/sh
# NowAirPlaying installer for Raspberry Pi OS Lite 64-bit (bookworm or trixie).
#
# Six phases (docs/ROADMAP.md). Each one checks before it acts, so a re-run
# is safe and a failed run resumes where it stopped:
#   1 preflight   2 apt   3 packages   4 units   5 configure   6 verify
#
# Runs from a copy of this repo that carries the built packages
# (build/README.md), or from --packages DIR laid out the same way:
#   bookworm: build/out/debs, plus the vendored PipeWire/WirePlumber in
#             build/out/vendor
#   trixie:   build/out/trixie/debs; PipeWire/WirePlumber come from the archive
#
# Usage:
#   sudo install/install.sh [--amp-mac AA:BB:CC:DD:EE:FF] [--name "Now AirPlaying"]
#                           [--user NAME] [--packages DIR] [--force]
#   sudo install/install.sh --verify        # phase 6 only
#
# Without --amp-mac everything is installed but speakerd stays off, and the
# script ends with the pairing checklist. Pair, then re-run with --amp-mac.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
LOG=/var/log/nowairplaying-install.log

usage() { sed -n '14,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run with sudo: sudo $0 $*"

# everything also goes to $LOG, with the script's own exit status kept
if [ -z "${NAP_LOGGING:-}" ]; then
    rc=$(mktemp)
    # set +e: under -e a failing run would end this group before it records $?
    { set +e; NAP_LOGGING=1 sh "$0" "$@"; echo $? > "$rc"; } 2>&1 | tee -a "$LOG"
    status=$(cat "$rc"); rm -f "$rc"
    exit "${status:-1}"
fi
printf '\n##### %s  %s %s\n' "$(date -Is)" "$0" "$*"

USER_NAME=${SUDO_USER:-}
AMP_MAC=
AIRPLAY_NAME=
PKGS=
FORCE=0
VERIFY_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --amp-mac)    [ $# -ge 2 ] || usage 2; AMP_MAC=$2; shift ;;
        --name)       [ $# -ge 2 ] || usage 2; AIRPLAY_NAME=$2; shift ;;
        --user)       [ $# -ge 2 ] || usage 2; USER_NAME=$2; shift ;;
        --packages)   [ $# -ge 2 ] || usage 2; PKGS=$2; shift ;;
        --force)      FORCE=1 ;;
        --verify)     VERIFY_ONLY=1 ;;
        -h|--help)    usage ;;
        *)            echo "unknown option: $1" >&2; usage 2 ;;
    esac
    shift
done

if [ -n "$AMP_MAC" ]; then
    AMP_MAC=$(printf '%s' "$AMP_MAC" | tr 'a-f-' 'A-F:')
    printf '%s' "$AMP_MAC" | grep -Eq '^([0-9A-F]{2}:){5}[0-9A-F]{2}$' \
        || die "--amp-mac: not a Bluetooth address: $AMP_MAC"
    [ "$AMP_MAC" != 00:00:00:00:00:00 ] || die "--amp-mac: 00:00:00:00:00:00 is the placeholder"
fi
case "$AIRPLAY_NAME" in *'"'*|*'\'*|*/*|*'&'*) die "--name: no quotes, backslashes, / or &" ;; esac
case "$PKGS" in *[[:space:]]*) die "--packages: the path must not contain spaces" ;; esac

# ---------------------------------------------------------------- helpers

# Status-Status, not Status-Abbrev: a held package abbreviates to "hi", not "ii"
installed() { [ "$(dpkg-query -W -f='${db:Status-Status}' "$1" 2>/dev/null)" = installed ]; }

# is this exact .deb (package and version) the installed one?
deb_current() {
    p=$(dpkg-deb -f "$1" Package); v=$(dpkg-deb -f "$1" Version)
    [ "$(dpkg-query -W -f='${db:Status-Status} ${Version}' "$p" 2>/dev/null)" = "installed $v" ]
}

# package names from the file names (<name>_<version>_<arch>.deb), so --verify
# works without the .deb files present
deb_names() { for f in "$@"; do f=${f##*/}; echo "${f%%_*}"; done; }

APT_UPDATED=0
apt_update() {
    [ "$APT_UPDATED" = 1 ] && return 0
    apt-get update || die "apt-get update failed: is the Pi online?"
    APT_UPDATED=1
}

# apt_install "<packages apt may remove>" <apt args...>
# Simulates first and stops if apt wants to remove anything not listed.
apt_install() {
    allowed=$1; shift
    plan=$(apt-get -s install --no-install-recommends --allow-change-held-packages "$@") \
        || die "apt cannot resolve: $*"
    for r in $(printf '%s\n' "$plan" | awk '/^Remv /{print $2}'); do
        case " $allowed " in
            *" $r "*) ;;
            *) printf '%s\n' "$plan" | grep -E '^(Inst|Remv) '
               die "apt would remove $r, which this installer does not expect. Nothing was changed." ;;
        esac
    done
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        --allow-change-held-packages -o Dpkg::Options::=--force-confold "$@"
}

hold() { apt-mark hold "$@" >/dev/null; info "held: $*"; }

# run a command as the node user, inside their systemd user session
as_user() {
    runuser -u "$USER_NAME" -- env HOME="$USER_HOME" XDG_RUNTIME_DIR="/run/user/$USER_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" "$@"
}
user_ctl() { as_user systemctl --user "$@"; }

# put SRC DEST MODE OWNER: install SRC at DEST if it differs. Returns 0 when
# DEST changed, 1 when it was already identical.
put() {
    if [ -f "$2" ] && cmp -s "$1" "$2" && [ "$(stat -c '%a %U' "$2")" = "$3 $4" ]; then
        return 1
    fi
    install -m "$3" -o "$4" -g "$(id -gn "$4")" "$1" "$2"
    info "wrote $2"
}

TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

epoch() { [ -n "$1" ] && date -d "$1" +%s 2>/dev/null || echo 0; }

# the amp address speakerd is configured with, or empty for the placeholder
configured_amp() {
    [ -f "$SPEAKERD_CONF" ] || return 0
    sed -n 's/^amp_mac *= *"\([0-9A-Fa-f:]*\)".*/\1/p' "$SPEAKERD_CONF" | head -1 \
        | grep -v '^00:00:00:00:00:00$' || true
}

# ---------------------------------------------------------------- setup

[ -n "$USER_NAME" ] || die "no target user: run with sudo from the Pi's own user, or pass --user NAME"
[ "$USER_NAME" != root ] || die "the audio stack runs as a normal user, not root: pass --user NAME"
USER_UID=$(id -u "$USER_NAME" 2>/dev/null) || die "no such user: $USER_NAME"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
SPEAKERD_CONF=$USER_HOME/.config/speakerd/config.toml
SHAIRPORT_CONF=$USER_HOME/.config/shairport-sync.conf
WP_CONF=$USER_HOME/.config/wireplumber/wireplumber.conf.d/90-nowairplaying.conf
UNIT_DIR=$USER_HOME/.config/systemd/user

# bookworm: PipeWire 1.4 vendored from backports, our BlueZ 5.87 replaces
# Debian's 5.66 (which ignores [AVRCP]).
# trixie: the archive has PipeWire 1.4 and BlueZ 5.82, which honours [AVRCP].
CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
case "$CODENAME" in
    bookworm) NAP_SUFFIX=; PKGS=${PKGS:-$REPO/build/out}; ARCHIVE_PW=
              OWN_BLUEZ=1; BLUEZ_MIN= ;;
    trixie)   NAP_SUFFIX=+deb13; PKGS=${PKGS:-$REPO/build/out/trixie}
              ARCHIVE_PW="pipewire pipewire-bin wireplumber libspa-0.2-bluetooth bluez"
              OWN_BLUEZ=0; BLUEZ_MIN=5.82 ;;
    *)        die "needs Raspberry Pi OS based on Debian 12 bookworm or 13 trixie, this is ${CODENAME:-unknown}" ;;
esac

# shellcheck source=../build/versions.env
. "$REPO/build/versions.env"
DEBS="$PKGS/debs/nowairplaying-nqptp_${NQPTP_VERSION}-0nap${NAP_REVISION}${NAP_SUFFIX}_arm64.deb
$PKGS/debs/nowairplaying-shairport-sync_${SHAIRPORT_VERSION}-0nap${NAP_REVISION}${NAP_SUFFIX}_arm64.deb"
if [ "$OWN_BLUEZ" = 1 ]; then
    DEBS="$PKGS/debs/nowairplaying-bluez_${BLUEZ_VERSION}-0nap${NAP_REVISION}${NAP_SUFFIX}_arm64.deb
$DEBS"
fi
VENDOR_DEBS=
if [ "$CODENAME" = bookworm ]; then
    VENDOR_DEBS=$(awk -v d="$PKGS/vendor" 'NF == 2 {print d "/" $2}' "$REPO/build/vendor.lock")
fi
SYS_PKGS="dbus-user-session python3-dbus-next python3-paho-mqtt avahi-daemon libnss-mdns $ARCHIVE_PW"

# ---------------------------------------------------------------- 1 preflight

preflight() {
    say "1/6 Preflight"
    model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
    case "$model" in
        "Raspberry Pi 4 Model B"*|"Raspberry Pi 5"*)
            info "board: $model" ;;
        "Raspberry Pi 3 Model B Plus"*)
            info "board: $model (untested so far: reports welcome)" ;;
        "") die "this is not a Raspberry Pi (no /proc/device-tree/model)" ;;
        *)
            [ "$FORCE" = 1 ] || die "$model is not supported. The Pi Zero 2 W and Pi 3 Model B have
       2.4 GHz-only Wi-Fi that shares one antenna with Bluetooth, so audio can drop out.
       Re-run with --force to install anyway."
            info "board: $model (unsupported, --force given)" ;;
    esac

    arch=$(dpkg --print-architecture)
    [ "$arch" = arm64 ] || die "needs Raspberry Pi OS 64-bit (arm64), this is $arch"
    for dm in lightdm gdm3 sddm; do
        if installed "$dm"; then
            die "this is the desktop edition ($dm is installed). NowAirPlaying
       needs Raspberry Pi OS Lite: the desktop runs a second audio session."
        fi
    done
    info "system: Raspberry Pi OS $CODENAME $arch, Lite"
    info "user: $USER_NAME ($USER_HOME)"

    for f in $DEBS; do
        [ -f "$f" ] || die "missing $f. Build the packages first (build/README.md)."
    done
    if [ -n "$VENDOR_DEBS" ]; then
        for f in $VENDOR_DEBS; do
            [ -f "$f" ] || die "missing $f. Run build/vendor.sh first (build/README.md)."
        done
        (cd "$PKGS/vendor" && sha256sum --quiet -c "$REPO/build/vendor.lock") \
            || die "the vendored packages do not match build/vendor.lock"
        info "packages: $PKGS (vendored set matches vendor.lock)"
    else
        info "packages: $PKGS (PipeWire and WirePlumber from the $CODENAME archive)"
    fi

    avail=$(df --output=avail -k / | tail -1)
    [ "$avail" -ge 600000 ] || die "less than 600 MB free on /"
}

# ---------------------------------------------------------------- 2 apt

phase_apt() {
    say "2/6 PipeWire, WirePlumber and system packages"
    need=
    for f in $VENDOR_DEBS; do deb_current "$f" || need=1; done
    for p in $SYS_PKGS; do installed "$p" || need=1; done
    if [ -z "$need" ]; then
        info "already installed"
    else
        apt_update
        # shellcheck disable=SC2086
        apt_install "" $VENDOR_DEBS $SYS_PKGS
    fi
    # bookworm: an upgrade must never bring back the Pi archive's 1.2.7,
    # which receives the phone's volume but never applies it
    if [ -n "$VENDOR_DEBS" ]; then
        # shellcheck disable=SC2086
        hold $(deb_names $VENDOR_DEBS)
    fi
}

# ---------------------------------------------------------------- 3 packages

phase_packages() {
    if [ "$OWN_BLUEZ" = 1 ]; then
        say "3/6 BlueZ $BLUEZ_VERSION, nqptp $NQPTP_VERSION, shairport-sync $SHAIRPORT_VERSION"
    else
        say "3/6 nqptp $NQPTP_VERSION, shairport-sync $SHAIRPORT_VERSION"
    fi
    need=
    for f in $DEBS; do deb_current "$f" || need=1; done
    if [ -z "$need" ]; then
        info "already installed"
    else
        apt_update
        # nowairplaying-bluez replaces Debian's bluez: the one removal allowed
        if [ "$OWN_BLUEZ" = 1 ]; then allow=bluez; else allow=; fi
        # shellcheck disable=SC2086
        apt_install "$allow" $DEBS
    fi
    # shellcheck disable=SC2086
    hold $(deb_names $DEBS)
    systemctl enable --now nqptp.service
}

# ---------------------------------------------------------------- 4 units

phase_units() {
    say "4/6 speakerd and the user units"
    if ! diff -rq -x __pycache__ "$REPO/speakerd" /opt/nowairplaying/speakerd >/dev/null 2>&1; then
        rm -rf /opt/nowairplaying/speakerd
        install -d -m 755 /opt/nowairplaying/speakerd
        install -m 644 "$REPO"/speakerd/*.py /opt/nowairplaying/speakerd/
        info "installed speakerd in /opt/nowairplaying"
        SPEAKERD_CHANGED=1
    fi

    as_user_mkdir "$UNIT_DIR"
    for u in speakerd shairport-sync; do
        if put "$REPO/systemd/$u.service" "$UNIT_DIR/$u.service" 644 "$USER_NAME"; then
            case "$u" in
                speakerd) SPEAKERD_CHANGED=1 ;;
                shairport-sync) SHAIRPORT_CHANGED=1 ;;
            esac
        fi
    done

    # linger: the user's session (PipeWire, shairport-sync, speakerd) starts
    # at boot with nobody logged in
    if [ "$(loginctl show-user "$USER_NAME" -p Linger --value 2>/dev/null)" != yes ]; then
        loginctl enable-linger "$USER_NAME"
        info "linger enabled for $USER_NAME"
    fi
    i=0
    until [ -S "/run/user/$USER_UID/bus" ]; do
        i=$((i + 1)); [ $i -le 30 ] || die "the user session bus for $USER_NAME did not come up"
        sleep 1
    done

    systemctl --global enable pipewire.socket pipewire.service wireplumber.service
    # Debian's bluez enables mpris-proxy for every user: it would bridge a
    # second player onto the amp next to speakerd's (our 5.87 package drops it)
    if [ -f /usr/lib/systemd/user/mpris-proxy.service ] \
       && [ "$(systemctl --global is-enabled mpris-proxy.service 2>/dev/null)" != masked ]; then
        systemctl --global mask mpris-proxy.service
        user_ctl stop mpris-proxy.service 2>/dev/null || true
        info "masked mpris-proxy"
    fi
    user_ctl daemon-reload
    user_ctl enable shairport-sync.service
    info "enabled: pipewire, wireplumber, shairport-sync"
}

as_user_mkdir() { runuser -u "$USER_NAME" -- mkdir -p "$@"; }

# ---------------------------------------------------------------- 5 configure

phase_configure() {
    say "5/6 Configuration"

    # BlueZ [AVRCP]: the Kohler advertises no AVRCP Target record, so without
    # these BlueZ refuses the volume path entirely
    f=/etc/bluetooth/main.conf
    sed -E -e 's/^#?[[:space:]]*VolumeWithoutTarget[[:space:]]*=.*/VolumeWithoutTarget = true/' \
           -e 's/^#?[[:space:]]*VolumeCategory[[:space:]]*=.*/VolumeCategory = false/' "$f" > "$TMP/main.conf"
    for k in 'VolumeWithoutTarget = true' 'VolumeCategory = false'; do
        [ "$(grep -c "^$k\$" "$TMP/main.conf")" = 1 ] \
            || die "$f: could not set '$k' under [AVRCP]. Edit it by hand and re-run."
    done
    if put "$TMP/main.conf" "$f" 644 root; then BT_CHANGED=1; fi

    # Pi OS can come up with Bluetooth soft-blocked (rfkill), and bluetoothd
    # cannot power a blocked adapter. systemd-rfkill keeps the unblock across
    # reboots.
    for d in /sys/class/rfkill/rfkill*; do
        [ -e "$d/type" ] && [ "$(cat "$d/type")" = bluetooth ] || continue
        if [ "$(cat "$d/soft")" = 1 ]; then
            echo 0 > "$d/soft"
            info "unblocked Bluetooth radio $(cat "$d/name")"
            BT_CHANGED=1
        fi
    done

    cat > "$TMP/wp.conf" <<'EOF'
# NowAirPlaying, written by install/install.sh
monitor.bluez.properties = {
  # speakerd registers the only AVRCP player on the amp's adapter. WirePlumber's
  # dummy player would be a second one next to it.
  bluez5.dummy-avrcp-player = false
}
wireplumber.profiles = {
  main = {
    # Headless node: nobody logs in, so no seat ever becomes active, and with
    # seat monitoring on WirePlumber would never start its Bluetooth monitor.
    monitor.bluez.seat-monitoring = disabled
  }
}
EOF
    as_user_mkdir "$(dirname "$WP_CONF")"
    if put "$TMP/wp.conf" "$WP_CONF" 644 "$USER_NAME"; then WP_CHANGED=1; fi

    # shairport-sync: from the example on first install; --name renames later
    as_user_mkdir "$(dirname "$SHAIRPORT_CONF")"
    src=$SHAIRPORT_CONF
    [ -f "$src" ] || src=$REPO/shairport/shairport-sync.conf.example
    if [ -n "$AIRPLAY_NAME" ]; then
        sed "s/^\([[:space:]]*name = \)\"[^\"]*\"/\1\"$AIRPLAY_NAME\"/" "$src" > "$TMP/shairport.conf"
    else
        cp "$src" "$TMP/shairport.conf"
    fi
    if put "$TMP/shairport.conf" "$SHAIRPORT_CONF" 644 "$USER_NAME"; then SHAIRPORT_CHANGED=1; fi

    # speakerd: from the example on first install; --amp-mac fills the address
    as_user_mkdir "$(dirname "$SPEAKERD_CONF")"
    src=$SPEAKERD_CONF
    [ -f "$src" ] || src=$REPO/config/config.example.toml
    if [ -n "$AMP_MAC" ]; then
        sed "s/^amp_mac *= *\"[^\"]*\"/amp_mac = \"$AMP_MAC\"/" "$src" > "$TMP/speakerd.toml"
        grep -q "^amp_mac = \"$AMP_MAC\"" "$TMP/speakerd.toml" \
            || die "$SPEAKERD_CONF: no amp_mac line to set. Edit it by hand and re-run."
    else
        cp "$src" "$TMP/speakerd.toml"
    fi
    if put "$TMP/speakerd.toml" "$SPEAKERD_CONF" 600 "$USER_NAME"; then SPEAKERD_CHANGED=1; fi

    # (re)start what changed, in dependency order
    if [ -n "${BT_CHANGED:-}" ] || ! bluetoothd_is_ours; then
        systemctl restart bluetooth.service
        info "restarted bluetooth"
    fi
    if [ -n "${WP_CHANGED:-}" ] || ! user_ctl is-active --quiet wireplumber.service; then
        user_ctl restart pipewire.service wireplumber.service
        info "restarted pipewire and wireplumber"
    fi
    if [ -n "${SHAIRPORT_CHANGED:-}" ] || ! user_ctl is-active --quiet shairport-sync.service; then
        user_ctl restart shairport-sync.service
        info "restarted shairport-sync"
    fi
    if [ -n "$(configured_amp)" ]; then
        user_ctl enable speakerd.service
        if [ -n "${SPEAKERD_CHANGED:-}" ] || ! user_ctl is-active --quiet speakerd.service; then
            user_ctl restart speakerd.service
            info "restarted speakerd"
        fi
    else
        user_ctl disable --now speakerd.service >/dev/null 2>&1 || true
        info "speakerd stays off until the amplifier is paired (checklist below)"
    fi
}

# the running bluetoothd is the /usr/libexec binary: our pinned version on
# bookworm, the archive's at BLUEZ_MIN or later on trixie
bluetoothd_is_ours() {
    pid=$(systemctl show -p MainPID --value bluetooth.service)
    [ "${pid:-0}" != 0 ] || return 1
    exe=$(readlink "/proc/$pid/exe" 2>/dev/null) || return 1
    [ "$exe" = /usr/libexec/bluetooth/bluetoothd ] || return 1
    v=$("$exe" --version 2>/dev/null)
    if [ -n "$BLUEZ_MIN" ]; then
        dpkg --compare-versions "${v:-0}" ge "$BLUEZ_MIN"
    else
        [ "$v" = "$BLUEZ_VERSION" ]
    fi
}

# ---------------------------------------------------------------- 6 verify

FAILS=0
TODO=0
ok()   { printf '   ok    %s\n' "$*"; }
bad()  { printf '   FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
todo() { printf '   todo  %s\n' "$*"; TODO=$((TODO + 1)); }

# wait up to $1 seconds for a command to succeed
within() {
    n=$1; shift
    until "$@" >/dev/null 2>&1; do
        n=$((n - 1)); [ "$n" -gt 0 ] || return 1
        sleep 1
    done
}

verify() {
    say "6/6 Verify: the running system"

    pid=$(systemctl show -p MainPID --value bluetooth.service)
    if bluetoothd_is_ours; then
        ok "running bluetoothd is $("/proc/$pid/exe" --version) (pid $pid)"
    else
        bad "running bluetoothd is not ${BLUEZ_MIN:+at least }${BLUEZ_MIN:-$BLUEZ_VERSION} (pid ${pid:-none}: $(readlink "/proc/$pid/exe" 2>/dev/null || echo no process)). Debian's bluez may ignore [AVRCP], and volume has no path."
    fi
    if grep -q '^VolumeWithoutTarget = true$' /etc/bluetooth/main.conf \
       && grep -q '^VolumeCategory = false$' /etc/bluetooth/main.conf; then
        ok "main.conf [AVRCP]: VolumeWithoutTarget = true, VolumeCategory = false"
    else
        bad "main.conf [AVRCP] settings are missing"
    fi

    if within 10 sh -c 'bluetoothctl show | grep -q "Powered: yes"'; then
        ok "Bluetooth adapter is powered"
    else
        bad "Bluetooth adapter is not powered (rfkill: $(for d in /sys/class/rfkill/rfkill*; do printf '%s soft=%s hard=%s ' "$(cat "$d/name")" "$(cat "$d/soft")" "$(cat "$d/hard")"; done))"
    fi

    v=$(shairport-sync -V 2>&1 || true)
    case "$v" in
        *-mqtt-*|*-mpris-*) bad "shairport-sync has MQTT or MPRIS compiled in: $v" ;;
        *-AirPlay2-*) ok "shairport-sync is AirPlay 2 ($v)" ;;
        *) bad "shairport-sync is not an AirPlay 2 build, iPhones get classic AirPlay: $v" ;;
    esac

    pwv=$(as_user pipewire --version 2>/dev/null | sed -n 's/^Linked with libpipewire \(.*\)/\1/p')
    if user_ctl is-active --quiet pipewire.service && [ -n "$pwv" ] \
       && dpkg --compare-versions "$pwv" ge 1.4; then
        ok "PipeWire $pwv running"
    else
        bad "PipeWire is not running at 1.4 or later (linked: ${pwv:-unknown}). 1.2.7 receives the phone's volume but never applies it."
    fi

    if user_ctl is-active --quiet wireplumber.service && [ -f "$WP_CONF" ] \
       && grep -q 'bluez5.dummy-avrcp-player = false' "$WP_CONF"; then
        wp_start=$(epoch "$(user_ctl show --timestamp=unix -p ActiveEnterTimestamp --value wireplumber.service)")
        if [ "$wp_start" -ge "$(stat -c %Y "$WP_CONF")" ]; then
            ok "WirePlumber running with its dummy AVRCP player off"
        else
            bad "WirePlumber started before $WP_CONF changed: restart it"
        fi
    else
        bad "WirePlumber is not running with $WP_CONF"
    fi
    if pgrep -x mpris-proxy >/dev/null; then
        bad "mpris-proxy is running: it adds a second player next to speakerd's"
    else
        ok "no mpris-proxy"
    fi

    if systemctl is-active --quiet nqptp.service; then
        n_t=$(systemctl show -p ActiveEnterTimestampMonotonic --value nqptp.service)
        s_t=$(user_ctl show -p ActiveEnterTimestampMonotonic --value shairport-sync.service)
        if ! user_ctl is-active --quiet shairport-sync.service; then
            bad "shairport-sync is not running"
        elif [ "${s_t:-0}" -gt "${n_t:-0}" ]; then
            ok "nqptp was up before shairport-sync started"
        else
            bad "shairport-sync started before nqptp: restart shairport-sync"
        fi
    else
        bad "nqptp is not running: AirPlay 2 has no timing"
    fi

    if systemctl is-active --quiet avahi-daemon.service \
       && grep -Eq '^hosts:.*mdns4_minimal' /etc/nsswitch.conf; then
        ok "avahi-daemon running, mdns4_minimal in nsswitch.conf"
    else
        bad "mDNS is not set up: avahi-daemon must run and nsswitch.conf needs mdns4_minimal"
    fi

    if within 15 as_user busctl --user status org.gnome.ShairportSync; then
        ok "org.gnome.ShairportSync is on $USER_NAME's session bus"
    else
        bad "org.gnome.ShairportSync is not on $USER_NAME's session bus: speakerd cannot follow AirPlay"
    fi
    if busctl --system status org.mpris.MediaPlayer2.ShairportSync >/dev/null 2>&1 \
       || as_user busctl --user status org.mpris.MediaPlayer2.ShairportSync >/dev/null 2>&1; then
        bad "shairport-sync owns an MPRIS name: a second player"
    else
        ok "shairport-sync has no MPRIS player"
    fi

    amp=$(configured_amp)
    if [ -z "$amp" ]; then
        todo "pair the amplifier, then re-run with --amp-mac (checklist below)"
    else
        info_amp=$(bluetoothctl info "$amp" 2>/dev/null || true)
        if printf '%s' "$info_amp" | grep -q 'Paired: yes' \
           && printf '%s' "$info_amp" | grep -q 'Trusted: yes'; then
            ok "amplifier $amp is paired and trusted"
        else
            bad "amplifier $amp is not paired and trusted (checklist below)"
        fi
        if user_ctl is-active --quiet speakerd.service; then
            since=$(epoch "$(user_ctl show --timestamp=unix -p ExecMainStartTimestamp --value speakerd.service)")
            if within 15 sh -c "journalctl _UID=$USER_UID _SYSTEMD_USER_UNIT=speakerd.service \
                    --since @$since -q --no-pager | grep -q 'amp player registered'"; then
                ok "one player on the amp's adapter: speakerd's /org/speakerd/player"
            else
                bad "speakerd has not registered its player: journalctl --user -u speakerd"
            fi
        else
            bad "speakerd is not running: journalctl --user -u speakerd"
        fi
    fi

    missing=
    # shellcheck disable=SC2086
    for p in $(deb_names $DEBS $VENDOR_DEBS); do
        apt-mark showhold | grep -qx "$p" || missing="$missing $p"
    done
    if [ -z "$missing" ]; then ok "packages held against upgrades"; else bad "not held:$missing"; fi
}

checklist() {
    amp=$(configured_amp)
    if [ -z "$amp" ]; then
        cat <<EOF

== Next: pair the amplifier (by hand, once)

  1. Put the Kohler amplifier in pairing mode. It only shows up in a scan
     while pairing mode is on.
  2. On the Pi:
         bluetoothctl
           scan on             wait for the amplifier, note its address
           pair  XX:XX:XX:XX:XX:XX
           trust XX:XX:XX:XX:XX:XX
           scan off
           quit
  3. Then:
         sudo $0 --amp-mac XX:XX:XX:XX:XX:XX
     This turns speakerd on and checks everything again.
EOF
    fi
    cat <<EOF

== Optional: Home Assistant

  Add an [mqtt] section to $SPEAKERD_CONF
  (see config/config.example.toml), then:
      systemctl --user restart speakerd
EOF
    echo
    echo "Log: $LOG"
}

# ---------------------------------------------------------------- main

if [ "$VERIFY_ONLY" = 0 ]; then
    preflight
    phase_apt
    phase_packages
    phase_units
    phase_configure
fi
verify
checklist
if [ "$FAILS" -gt 0 ]; then
    printf '\n%d check(s) FAILED.\n' "$FAILS"
    exit 1
fi
if [ "$TODO" -gt 0 ]; then
    printf '\nInstalled. %d step(s) left to do (above).\n' "$TODO"
else
    printf '\nAll checks passed. AirPlay to "%s".\n' \
        "$(sed -n 's/^[[:space:]]*name = "\([^"]*\)".*/\1/p' "$SHAIRPORT_CONF" | head -1)"
fi
