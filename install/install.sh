#!/bin/sh
# NowAirPlaying installer for Raspberry Pi OS Lite 64-bit (bookworm or trixie).
#
# Six phases (docs/ROADMAP.md). Each one checks before it acts, so a re-run
# is safe and a failed run resumes where it stopped:
#   1 preflight   2 apt   3 packages   4 units   5 configure   6 verify
# Every install run records its progress in /var/lib/nowairplaying/install.json
# (docs/INSTALL-STATE.md). install/bootstrap.sh fetches a release and runs this.
#
# From 0.0.2 the whole audio stack (PipeWire, WirePlumber, shairport-sync,
# speakerd) runs as the system account "nowairplaying", not as --user.
# The node API runs as a second system account, "nowairplaying-api": it is
# the only polkit subject, since shairport-sync listens to the whole LAN and
# the audio account must hold no grants. speakerd (as nowairplaying) serves
# the API over a local control socket; nothing of ours shares one account
# with the other.
# --user is now the SSH login user: it's added to group nowairplaying-api
# (so it can plant a fresh claim token without sudo, docs/SETUP-API.md), and
# its 0.0.1-era units and config are migrated into the new account once.
#
# Runs from a copy of this repo that carries the built packages
# (build/README.md), or from --packages DIR laid out the same way:
#   bookworm: build/out/debs, plus the vendored PipeWire/WirePlumber in
#             build/out/vendor
#   trixie:   build/out/trixie/debs; PipeWire/WirePlumber come from the archive
#
# Usage:
#   sudo install/install.sh [--amp-mac AA:BB:CC:DD:EE:FF] [--name "Now AirPlaying"]
#                           [--phones onboard|dongle] --user NAME
#                           [--packages DIR] [--force]
#   sudo install/install.sh --verify        # phase 6 only
#
# --amp-mac pre-fills the amplifier's address in speakerd's config, same as
# today, but it's no longer required: speakerd always starts, with or
# without an amp. Pairing is normally done from Home Assistant or the node's
# own setup page (http://<hostname>.local:8080/), not from this installer.
# --name is optional too: given, it renames the AirPlay receiver; omitted,
# the current name (or the config example's) is left alone.
# --phones is accepted for phone Bluetooth, which is not built yet: so far it
# changes nothing.
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
LOG=${NAP_LOG:-/var/log/nowairplaying-install.log}
VERSION=$(cat "$REPO/VERSION" 2>/dev/null || echo unknown)

usage() { sed -n '22,35p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
say()  { printf '\n== %s\n' "$*"; }
info() { printf '   %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
# A preflight failure that means something already on the Pi would clash
# with NowAirPlaying, not a broken install: status.sh's finish_status gives
# it the reason preflight_conflict instead of the generic install_failed,
# by recognising this exact prefix in the log's last ERROR line.
conflict() { die "preflight_conflict: $*"; }

[ "$(id -u)" = 0 ] || die "run with sudo: sudo $0 $*"

USER_NAME=${SUDO_USER:-}
AMP_MAC=
AMP_NAME_CARRY=
AIRPLAY_NAME=
PHONES=onboard
PKGS=
FORCE=0
VERIFY_ONLY=0
ARGS="$*"
# a function, so its shifts leave the script's own "$@" for the re-run below
parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --amp-mac)    [ $# -ge 2 ] || usage 2; AMP_MAC=$2; shift ;;
            --name)       [ $# -ge 2 ] || usage 2; AIRPLAY_NAME=$2; shift ;;
            --phones)     [ $# -ge 2 ] || usage 2; PHONES=$2; shift ;;
            --user)       [ $# -ge 2 ] || usage 2; USER_NAME=$2; shift ;;
            --packages)   [ $# -ge 2 ] || usage 2; PKGS=$2; shift ;;
            --force)      FORCE=1 ;;
            --verify)     VERIFY_ONLY=1 ;;
            -h|--help)    usage ;;
            *)            echo "unknown option: $1" >&2; usage 2 ;;
        esac
        shift
    done
}
parse_args "$@"

if [ -n "$AMP_MAC" ]; then
    AMP_MAC=$(printf '%s' "$AMP_MAC" | tr 'a-f-' 'A-F:')
    printf '%s' "$AMP_MAC" | grep -Eq '^([0-9A-F]{2}:){5}[0-9A-F]{2}$' \
        || die "--amp-mac: not a Bluetooth address: $AMP_MAC"
    [ "$AMP_MAC" != 00:00:00:00:00:00 ] || die "--amp-mac: 00:00:00:00:00:00 is the placeholder"
fi
case "$AIRPLAY_NAME" in *'"'*|*'\'*|*/*|*'&'*) die "--name: no quotes, backslashes, / or &" ;; esac
# and no control characters: a newline would break shairport-sync.conf's name line
[ "$(printf '%s' "$AIRPLAY_NAME" | tr -d '[:cntrl:]')" = "$AIRPLAY_NAME" ] \
    || die "--name: no control characters"
case "$PHONES" in onboard|dongle) ;; *) die "--phones: onboard or dongle, not $PHONES" ;; esac
case "$PKGS" in *[[:space:]]*) die "--packages: the path must not contain spaces" ;; esac

# shellcheck source=status.sh
. "$HERE/status.sh"
STARTED=${NAP_STARTED:-$(date -Is)}
LOG_OFFSET=${NAP_LOG_OFFSET:-$(log_size)}

# the system account the audio stack runs as. Resolved here (not just
# created in phase 4) so --verify, which skips phases 1-5 entirely, still
# knows whose session to check.
# Its home is a subdirectory of STATE_DIR, never STATE_DIR itself: STATE_DIR
# holds what root reads back (install.json, install-args) and the API's
# claim/, update/ and tls/, so it stays root:root 0755. An account that owned
# it could rename any of those and feed root its own.
NAP_USER=nowairplaying
NAP_HOME=$STATE_DIR/home
NAP_UID=$(id -u "$NAP_USER" 2>/dev/null) || NAP_UID=

# the account that runs the node API and is polkit's only subject
# (docs/SETUP-API.md "Privileges"). Resolved here too, so preflight's port
# check and --verify know it before phase 4 creates it.
NAP_API_USER=nowairplaying-api
NAP_API_HOME=/var/lib/nowairplaying-api

# phase N NAME: record an install run's progress (--verify only reads)
phase() { [ "$VERIFY_ONLY" = 1 ] || write_status installing "$1" "$2" "" "" ""; }

# everything also goes to $LOG, with the script's own exit status kept, and
# install.json gets the run's final state
if [ -z "${NAP_LOGGING:-}" ]; then
    phase 1 preflight
    rc=$(mktemp)
    # set +e: under -e a failing run would end this group before it records $?
    { set +e; NAP_LOGGING=1 NAP_STARTED=$STARTED NAP_LOG_OFFSET=$LOG_OFFSET NAP_LOG=$LOG \
        sh "$0" "$@"; echo $? > "$rc"; } 2>&1 | tee -a "$LOG"
    status=$(cat "$rc"); rm -f "$rc"
    [ "$VERIFY_ONLY" = 1 ] || finish_status "${status:-1}"
    exit "${status:-1}"
fi
printf '\n##### %s  %s %s\n' "$(date -Is)" "$0" "$ARGS"
info "NowAirPlaying $VERSION"

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

# run a command as the nowairplaying account, inside its systemd user session
as_user() {
    runuser -u "$NAP_USER" -- env HOME="$NAP_HOME" XDG_RUNTIME_DIR="/run/user/$NAP_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$NAP_UID/bus" "$@"
}
user_ctl() { as_user systemctl --user "$@"; }
as_user_mkdir() { runuser -u "$NAP_USER" -- mkdir -p "$@"; }

# put SRC DEST MODE OWNER: install SRC at DEST if it differs. Returns 0 when
# DEST changed, 1 when it was already identical -- so call it under "if", or
# with "|| true" when the change doesn't matter. Never bare: under set -e an
# unchanged file would end the whole install with no message.
put() {
    if [ -f "$2" ] && cmp -s "$1" "$2" && [ "$(stat -c '%a %U' "$2")" = "$3 $4" ]; then
        return 1
    fi
    # die, not set -e: set -e is off inside "if put" and "put || true"
    install -m "$3" -o "$4" -g "$(id -gn "$4")" "$1" "$2" || die "could not write $2"
    info "wrote $2"
}

# Files in the nowairplaying account's own home are read and written by the
# account itself, never by root. It controls every directory in there, so a
# link it planted would otherwise turn a root write into a write anywhere
# (say, a unit file in /etc/systemd/system/*.wants), and a root read into a
# copy of any file it likes.
#
# user_read FILE: FILE's contents, read as nowairplaying
user_read() { runuser -u "$NAP_USER" -- cat -- "$1"; }

# put_user SRC DEST MODE: put, for a file in the nowairplaying account's
# home. SRC is root's, and goes to the account on stdin.
put_user() {
    if runuser -u "$NAP_USER" -- sh -c \
        '[ -f "$1" ] && [ ! -L "$1" ] && [ "$(stat -c %a "$1")" = "$2" ] && cmp -s - "$1"' \
        sh "$2" "$3" < "$1"; then
        return 1
    fi
    runuser -u "$NAP_USER" -- sh -c \
        'umask 077; t=$1.nap; cat > "$t" && chmod "$2" "$t" && mv -f "$t" "$1"' \
        sh "$2" "$3" < "$1" || die "could not write $2"
    info "wrote $2"
}

# set_amp_kv FILE KEY VALUE: set KEY = "VALUE" in a speakerd config file,
# whether KEY is already live, commented out, or missing entirely --
# config/config.example.toml may ship any of the three, since amp_mac is
# optional from 0.0.2.
set_amp_kv() {
    f=$1; k=$2; v=$3
    if grep -Eq "^[[:space:]]*$k[[:space:]]*=" "$f"; then
        sed -E "s/^([[:space:]]*)$k[[:space:]]*=.*/\1$k = \"$v\"/" "$f" > "$f.nap" && mv "$f.nap" "$f"
    elif grep -Eq "^[[:space:]]*#[[:space:]]*$k[[:space:]]*=" "$f"; then
        sed -E "s/^[[:space:]]*#[[:space:]]*$k[[:space:]]*=.*/$k = \"$v\"/" "$f" > "$f.nap" && mv "$f.nap" "$f"
    else
        printf '%s = "%s"\n' "$k" "$v" >> "$f"
    fi
}

TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
trap 'exit 130' INT TERM

epoch() { [ -n "$1" ] && date -d "$1" +%s 2>/dev/null || echo 0; }

# the amp address speakerd is configured with, or empty for the placeholder
# the amp speakerd uses: its state file wins (a pair or forget over the node
# API is stored there, and an "amp": null there means forgotten), else
# config.toml's amp_mac
configured_amp() {
    id -u "$NAP_USER" >/dev/null 2>&1 || return 0
    a=$(user_read "$NAP_HOME/.local/state/speakerd/state.json" 2>/dev/null | python3 -c 'import json,sys
d=json.load(sys.stdin)
if "amp" in d: print((d["amp"] or {}).get("mac") or "-")' 2>/dev/null || true)
    case "$a" in
        -) return 0 ;;
        ??:??:??:??:??:??) echo "$a"; return 0 ;;
    esac
    user_read "$SPEAKERD_CONF" 2>/dev/null \
        | sed -n 's/^amp_mac *= *"\([0-9A-Fa-f:]*\)".*/\1/p' | head -1 \
        | grep -v '^00:00:00:00:00:00$' || true
}

# ---------------------------------------------------------------- setup

[ -n "$USER_NAME" ] || die "no target user: run with sudo from the Pi's own user, or pass --user NAME"
[ "$USER_NAME" != root ] || die "--user must be a normal login user, not root"
USER_UID=$(id -u "$USER_NAME" 2>/dev/null) || die "no such user: $USER_NAME"
USER_HOME=$(getent passwd "$USER_NAME" | cut -d: -f6)
SPEAKERD_CONF=$NAP_HOME/.config/speakerd/config.toml
SHAIRPORT_CONF=$NAP_HOME/.config/shairport-sync.conf
WP_CONF=$NAP_HOME/.config/wireplumber/wireplumber.conf.d/90-nowairplaying.conf
UNIT_DIR=$NAP_HOME/.config/systemd/user

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
SYS_PKGS="dbus-user-session python3-dbus-next python3-paho-mqtt python3-aiohttp \
avahi-daemon libnss-mdns openssl unattended-upgrades $ARCHIVE_PW"

# ---------------------------------------------------------------- 1 preflight

# TCP 8443/8080 (the node API, served by nowairplaying-api, not speakerd) and
# UDP 319/320 (nqptp) must be free, or already ours: a re-run finds its own
# nowairplaying-api/nqptp bound there.
check_ports_free() {
    for p in 8443 8080; do
        pid=$(ss -Hltnp "sport = :$p" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1)
        [ -n "$pid" ] || continue
        owner=$(ps -o user= -p "$pid" 2>/dev/null | tr -d '[:space:]')
        [ "$owner" = "$NAP_API_USER" ] && continue
        conflict "TCP port $p is already used by pid $pid (user $owner), not $NAP_API_USER"
    done
    for p in 319 320; do
        pid=$(ss -Hlunp "sport = :$p" 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -1)
        [ -n "$pid" ] || continue
        comm=$(ps -o comm= -p "$pid" 2>/dev/null)
        [ "$comm" = nqptp ] && continue
        conflict "UDP port $p is already used by pid $pid ($comm), not nqptp"
    done
}

# another AirPlay receiver already running is a conflict; shairport-sync
# running as $NAP_USER (ours, a re-run) or as $USER_NAME (the 0.0.1 layout,
# about to be migrated) is not.
check_other_airplay() {
    for p in uxplay owntone; do
        pgrep -x "$p" >/dev/null 2>&1 && conflict "another AirPlay receiver is running: $p"
    done
    for pid in $(pgrep -x shairport-sync 2>/dev/null || true); do
        owner=$(ps -o user= -p "$pid" 2>/dev/null | tr -d '[:space:]')
        case "$owner" in
            "$NAP_USER"|"$USER_NAME") continue ;;
        esac
        conflict "shairport-sync (pid $pid) is already running as ${owner:-an unknown user}, not $NAP_USER or $USER_NAME"
    done
}

preflight() {
    phase 1 preflight
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
            conflict "this is the desktop edition ($dm is installed): NowAirPlaying needs Raspberry Pi OS Lite, since the desktop runs a second audio session"
        fi
    done
    check_ports_free
    check_other_airplay
    info "system: Raspberry Pi OS $CODENAME $arch, Lite"
    info "SSH user: $USER_NAME ($USER_HOME); audio runs as $NAP_USER"

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
    phase 2 apt
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

    # OS security updates: not something Home Assistant does from the API.
    # nqptp and shairport-sync stay apt-held above, so this can't replace them.
    cat > "$TMP/20auto-upgrades" <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
    put "$TMP/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades 644 root || true
    systemctl enable --now apt-daily.timer apt-daily-upgrade.timer >/dev/null
    info "unattended-upgrades enabled"
}

# ---------------------------------------------------------------- 3 packages

phase_packages() {
    phase 3 packages
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

# create_nap_user: the system account the audio stack runs as from 0.0.2
# (docs/SETUP-API.md "The service": "our own account"). Idempotent: a
# re-run only fixes up group membership and ownership.
create_nap_user() {
    # STATE_DIR is root's (see NAP_HOME above). status.sh has usually made it
    # already; this also takes it back from any earlier layout.
    install -d -m 0755 -o root -g root "$STATE_DIR"
    if ! id -u "$NAP_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$NAP_HOME" --create-home --shell /usr/sbin/nologin \
            --user-group "$NAP_USER"
        info "created system account $NAP_USER"
    fi
    home=$(getent passwd "$NAP_USER" | cut -d: -f6)
    [ "$home" = "$NAP_HOME" ] \
        || die "the $NAP_USER account's home is $home, not $NAP_HOME (an early 0.0.2 test build?): remove the account, keeping its files (sudo loginctl disable-linger $NAP_USER; sudo userdel $NAP_USER), and re-run"
    NAP_UID=$(id -u "$NAP_USER")
    extra=audio
    getent group bluetooth >/dev/null 2>&1 && extra="$extra,bluetooth"
    usermod -aG "$extra" "$NAP_USER"

    # nobody else needs anything in here: the API reaches speakerd through
    # the control socket in /run/nowairplaying
    chown -h "$NAP_USER:$NAP_USER" "$NAP_HOME"
    chmod 0700 "$NAP_HOME"

    # linger: the account's session (PipeWire, shairport-sync, speakerd)
    # starts at boot with nobody logged in
    if [ "$(loginctl show-user "$NAP_USER" -p Linger --value 2>/dev/null)" != yes ]; then
        loginctl enable-linger "$NAP_USER"
        info "linger enabled for $NAP_USER"
    fi
}

# create_nap_api_user: the system account that runs the node API and is
# polkit's only subject (docs/SETUP-API.md "Privileges"). It gets no extra
# groups: it reaches BlueZ and PipeWire only through speakerd's local
# control socket, never directly. Idempotent, like create_nap_user.
create_nap_api_user() {
    if ! id -u "$NAP_API_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$NAP_API_HOME" --create-home --shell /usr/sbin/nologin \
            --user-group "$NAP_API_USER"
        info "created system account $NAP_API_USER"
    fi
    chown "$NAP_API_USER:$NAP_API_USER" "$NAP_API_HOME"
    chmod 0750 "$NAP_API_HOME"

    # so the SSH user can plant a fresh claim token without sudo, from their
    # next login (docs/SETUP-API.md "Ownership")
    usermod -aG "$NAP_API_USER" "$USER_NAME"
}

# ensure_tmpfiles: /run/nowairplaying, the control socket directory speakerd
# (nowairplaying) listens on and the API (nowairplaying-api) connects to.
# /run is tmpfs, so tmpfiles.d recreates this at every boot; run it now too,
# so a re-run has it immediately instead of waiting for the next boot. Must
# run after both accounts exist and before either speakerd or the API
# service is (re)started.
ensure_tmpfiles() {
    cat > "$TMP/tmpfiles.conf" <<EOF
d /run/nowairplaying 2750 $NAP_USER $NAP_API_USER -
EOF
    put "$TMP/tmpfiles.conf" /etc/tmpfiles.d/nowairplaying.conf 644 root || true
    systemd-tmpfiles --create /etc/tmpfiles.d/nowairplaying.conf
}

# migrate_old_layout: 0.0.1 (and the 09-28 manual installs) ran the stack as
# --user. Stop and remove just the two unit files we planted there, and
# carry over a real amp_mac if nowairplaying doesn't have one yet. Bluetooth
# bonds are system-wide (/var/lib/bluetooth) and need nothing. --user's
# linger, if 0.0.1 turned it on, is left alone: we can't tell whether we did.
migrate_old_layout() {
    old_dir=$USER_HOME/.config/systemd/user
    migrated=
    for u in speakerd shairport-sync; do
        f=$old_dir/$u.service
        [ -f "$f" ] || continue
        runuser -u "$USER_NAME" -- env HOME="$USER_HOME" XDG_RUNTIME_DIR="/run/user/$USER_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
            systemctl --user disable --now "$u.service" >/dev/null 2>&1 || true
        rm -f "$f"
        migrated=1
        info "migrated: stopped the old $USER_NAME $u.service and removed its unit file"
    done

    old_conf=$USER_HOME/.config/speakerd/config.toml
    if [ -f "$old_conf" ] && [ -z "$(configured_amp)" ]; then
        old_amp=$(sed -n 's/^amp_mac *= *"\([0-9A-Fa-f:]*\)".*/\1/p' "$old_conf" | head -1 \
            | grep -v '^00:00:00:00:00:00$' || true)
        if [ -n "$old_amp" ]; then
            AMP_MAC=${AMP_MAC:-$old_amp}
            AMP_NAME_CARRY=$(sed -n 's/^amp_name *= *"\([^"]*\)".*/\1/p' "$old_conf" | head -1)
            # set_amp_kv puts it in a sed replacement: carry only a safe name
            case "$AMP_NAME_CARRY" in *'\'*|*/*|*'&'*) AMP_NAME_CARRY= ;; esac
            info "carrying amp_mac $old_amp over from the old layout"
        fi
    fi

    # 0.0.1's WirePlumber drop-in turned seat monitoring off for --user, so
    # their WirePlumber would keep a Bluetooth monitor of its own running next
    # to nowairplaying's, and the two would fight over the audio endpoints.
    # Without it, theirs only watches Bluetooth during a local seat session.
    old_wp=$USER_HOME/.config/wireplumber/wireplumber.conf.d/90-nowairplaying.conf
    if [ -f "$old_wp" ]; then
        rm -f "$old_wp"
        runuser -u "$USER_NAME" -- env HOME="$USER_HOME" XDG_RUNTIME_DIR="/run/user/$USER_UID" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$USER_UID/bus" \
            systemctl --user try-restart wireplumber.service >/dev/null 2>&1 || true
        migrated=1
        info "migrated: removed the old WirePlumber drop-in from $USER_NAME's home"
    fi

    [ -z "$migrated" ] || info "note: $USER_NAME's linger was left as it was; this installer can't tell if 0.0.1 turned it on"
}

# USER= and PHONES= for install/update.sh, which has no flags of its own
# (docs/SETUP-API.md "POST /node/update")
write_install_args() {
    printf 'USER=%s\nPHONES=%s\n' "$USER_NAME" "$PHONES" > "$TMP/install-args"
    put "$TMP/install-args" "$STATE_DIR/install-args" 644 root || true
}

# the update unit, the Wi-Fi unit, the API unit, and the scripts they run,
# so a later /node/update or a boot-time Wi-Fi join needs no SSH session
# (docs/INSTALL-STATE.md "Rollback")
install_update_lib() {
    install -d -m 0755 /usr/local/lib/nowairplaying
    for f in update.sh bootstrap.sh status.sh wifi-add.sh; do
        put "$REPO/install/$f" "/usr/local/lib/nowairplaying/$f" 755 root || true
    done
    put "$REPO/systemd/nowairplaying-update.service" /etc/systemd/system/nowairplaying-update.service 644 root || true
    put "$REPO/systemd/nowairplaying-reset.service" /etc/systemd/system/nowairplaying-reset.service 644 root || true
    put "$REPO/systemd/nowairplaying-wifi.service" /etc/systemd/system/nowairplaying-wifi.service 644 root || true
    if put "$REPO/systemd/nowairplaying-api.service" /etc/systemd/system/nowairplaying-api.service 644 root; then
        API_UNIT_CHANGED=1
    fi
    systemctl daemon-reload
    systemctl enable nowairplaying-reset.service nowairplaying-wifi.service nowairplaying-api.service >/dev/null
}

phase_units() {
    phase 4 units
    say "4/6 The nowairplaying account, speakerd and the units"

    create_nap_user
    create_nap_api_user
    ensure_tmpfiles
    migrate_old_layout
    write_install_args
    install_update_lib

    if ! diff -rq -x __pycache__ "$REPO/speakerd" /opt/nowairplaying/speakerd >/dev/null 2>&1; then
        rm -rf /opt/nowairplaying/speakerd
        install -d -m 755 /opt/nowairplaying/speakerd
        install -m 644 "$REPO"/speakerd/*.py /opt/nowairplaying/speakerd/
        info "installed speakerd in /opt/nowairplaying"
        SPEAKERD_CHANGED=1
    fi

    as_user_mkdir "$UNIT_DIR"
    for u in speakerd shairport-sync; do
        if put_user "$REPO/systemd/$u.service" "$UNIT_DIR/$u.service" 644; then
            case "$u" in
                speakerd) SPEAKERD_CHANGED=1 ;;
                shairport-sync) SHAIRPORT_CHANGED=1 ;;
            esac
        fi
    done

    i=0
    until [ -S "/run/user/$NAP_UID/bus" ]; do
        i=$((i + 1)); [ $i -le 30 ] || die "the user session bus for $NAP_USER did not come up"
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

# ---------------------------------------------------------------- 5 configure

# the directories the node API needs, all owned by nowairplaying-api, the
# only account that touches them (docs/SETUP-API.md), in root's STATE_DIR
# next to install.json. install -d re-applies mode and ownership even when
# the directory already exists. Their entries sit in a root-owned directory,
# so the API account can't swap one for a link; a link left by an earlier
# layout is removed rather than followed.
#
# Root never chowns or chmods the files inside them: the API account owns
# those directories, and chown and chmod follow a link it could plant there
# (tls/key.pem -> /etc/shadow).
ensure_dirs() {
    for d in claim:2770 update:0700 tls:0700; do
        p=$STATE_DIR/${d%%:*}
        [ ! -L "$p" ] || rm -f "$p"
        install -d -m "${d#*:}" -o "$NAP_API_USER" -g "$NAP_API_USER" "$p"
    done
}

# a claim token Home Assistant planted at --user's home, over SSH with no
# sudo, before the install (docs/SETUP-API.md "Ownership"). Never printed.
# Owned by nowairplaying-api, the only account that reads it. An existing
# one is left exactly as it is (see ensure_dirs: no root chown in there); one
# the API can't read locks /claim rather than opening it.
migrate_claim_token() {
    src=$USER_HOME/.config/nowairplaying/claim-token
    dest=$STATE_DIR/claim/claim-token
    [ -f "$src" ] || return 0
    # root copies it: never follow a link to some other file
    [ ! -L "$src" ] || { rm -f "$src"; info "ignored a claim-token that was a symlink"; return 0; }
    if [ -e "$dest" ] || [ -L "$dest" ]; then
        rm -f "$src"
        return 0
    fi
    install -m 0600 -o "$NAP_API_USER" -g "$NAP_API_USER" "$src" "$dest"
    rm -f "$src"
    info "moved a planted claim token into place"
}

# a self-signed EC P-256 certificate, made once and kept across re-runs and
# updates (docs/SETUP-API.md "Trust: the pinned certificate"). Owned by
# nowairplaying-api, which serves it. A re-run keeps a pair of real files as
# they are, without touching them (see ensure_dirs), and replaces a missing
# one or a link (install(1) replaces a link rather than writing through it).
# CERT_CHANGED is only set when a certificate is actually generated, so
# install.sh only restarts the API service on a real change.
ensure_tls() {
    c=$STATE_DIR/tls/cert.pem; k=$STATE_DIR/tls/key.pem
    if [ -f "$c" ] && [ ! -L "$c" ] && [ -f "$k" ] && [ ! -L "$k" ]; then
        return 0
    fi
    hn=$(hostname)
    ( umask 077
      openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 36500 \
          -subj "/CN=nowairplaying" -addext "subjectAltName=DNS:$hn.local" \
          -keyout "$TMP/key.pem" -out "$TMP/cert.pem" ) 2> "$TMP/openssl.err" \
        || die "could not generate the TLS certificate: $(tail -1 "$TMP/openssl.err")"
    install -m 0600 -o "$NAP_API_USER" -g "$NAP_API_USER" "$TMP/key.pem" "$k"
    install -m 0644 -o "$NAP_API_USER" -g "$NAP_API_USER" "$TMP/cert.pem" "$c"
    info "generated a self-signed TLS certificate for $hn.local (valid 100 years)"
    CERT_CHANGED=1
}

phase_configure() {
    phase 5 configure
    say "5/6 Configuration"

    ensure_dirs
    migrate_claim_token
    ensure_tls
    # /usr/share, not /etc: the API's own verify check reads this file, and
    # /etc/polkit-1/rules.d isn't readable by non-root. Remove any copy an
    # earlier run left at the old, root-only path.
    put "$REPO/polkit/50-nowairplaying.rules" /usr/share/polkit-1/rules.d/50-nowairplaying.rules 644 root || true
    rm -f /etc/polkit-1/rules.d/50-nowairplaying.rules

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
    if put_user "$TMP/wp.conf" "$WP_CONF" 644; then WP_CHANGED=1; fi

    # shairport-sync: from the example on first install; --name renames later
    as_user_mkdir "$(dirname "$SHAIRPORT_CONF")"
    src=$TMP/shairport.current
    user_read "$SHAIRPORT_CONF" > "$src" 2>/dev/null || src=$REPO/shairport/shairport-sync.conf.example
    if [ -n "$AIRPLAY_NAME" ]; then
        sed "s/^\([[:space:]]*name = \)\"[^\"]*\"/\1\"$AIRPLAY_NAME\"/" "$src" > "$TMP/shairport.conf"
    else
        cp "$src" "$TMP/shairport.conf"
    fi
    if put_user "$TMP/shairport.conf" "$SHAIRPORT_CONF" 644; then SHAIRPORT_CHANGED=1; fi

    # speakerd: from the example on first install. --amp-mac fills the
    # address; amp_mac is otherwise optional from 0.0.2 (pairing is done over
    # the node API, not by re-running this installer).
    as_user_mkdir "$(dirname "$SPEAKERD_CONF")"
    user_read "$SPEAKERD_CONF" > "$TMP/speakerd.toml" 2>/dev/null \
        || cp "$REPO/config/config.example.toml" "$TMP/speakerd.toml"
    [ -n "$AMP_MAC" ] && set_amp_kv "$TMP/speakerd.toml" amp_mac "$AMP_MAC"
    [ -n "$AMP_NAME_CARRY" ] && set_amp_kv "$TMP/speakerd.toml" amp_name "$AMP_NAME_CARRY"
    if put_user "$TMP/speakerd.toml" "$SPEAKERD_CONF" 600; then SPEAKERD_CHANGED=1; fi

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
    # speakerd always runs from 0.0.2: it starts with or without an amp and
    # applies a pairing live, with no restart (docs/SETUP-API.md "The service")
    user_ctl enable speakerd.service
    if [ -n "${SPEAKERD_CHANGED:-}" ] || ! user_ctl is-active --quiet speakerd.service; then
        user_ctl restart speakerd.service
        info "restarted speakerd"
    fi

    # the node API: restarted on its own unit file changing, speakerd's
    # Python code changing (it's the same tree), a freshly generated
    # certificate, or simply not running yet
    if [ -n "${API_UNIT_CHANGED:-}" ] || [ -n "${SPEAKERD_CHANGED:-}" ] || [ -n "${CERT_CHANGED:-}" ] \
       || ! systemctl is-active --quiet nowairplaying-api.service; then
        systemctl restart nowairplaying-api.service
        info "restarted nowairplaying-api"
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
ok()   { printf '   ok    %s\n' "$*"; }
bad()  { printf '   FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }

# wait up to $1 seconds for a command to succeed
within() {
    n=$1; shift
    until "$@" >/dev/null 2>&1; do
        n=$((n - 1)); [ "$n" -gt 0 ] || return 1
        sleep 1
    done
}

verify() {
    phase 6 verify
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

    if user_ctl is-active --quiet wireplumber.service \
       && user_read "$WP_CONF" 2>/dev/null | grep -q 'bluez5.dummy-avrcp-player = false'; then
        wp_start=$(epoch "$(user_ctl show --timestamp=unix -p ActiveEnterTimestamp --value wireplumber.service)")
        if [ "$wp_start" -ge "$(runuser -u "$NAP_USER" -- stat -c %Y "$WP_CONF")" ]; then
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
        ok "org.gnome.ShairportSync is on $NAP_USER's session bus"
    else
        bad "org.gnome.ShairportSync is not on $NAP_USER's session bus: speakerd cannot follow AirPlay"
    fi
    if busctl --system status org.mpris.MediaPlayer2.ShairportSync >/dev/null 2>&1 \
       || as_user busctl --user status org.mpris.MediaPlayer2.ShairportSync >/dev/null 2>&1; then
        bad "shairport-sync owns an MPRIS name: a second player"
    else
        ok "shairport-sync has no MPRIS player"
    fi

    if user_ctl is-active --quiet speakerd.service; then
        ok "speakerd is running"
    else
        bad "speakerd is not running: journalctl --user -u speakerd"
    fi

    if systemctl is-active --quiet nowairplaying-api.service; then
        ok "nowairplaying-api is running"
    else
        bad "nowairplaying-api is not running: journalctl -u nowairplaying-api"
    fi

    amp=$(configured_amp)
    if [ -z "$amp" ]; then
        info "no amplifier paired yet: pair it from Home Assistant, or the setup page at http://$(hostname).local:8080/"
    else
        info_amp=$(bluetoothctl info "$amp" 2>/dev/null || true)
        if printf '%s' "$info_amp" | grep -q 'Paired: yes' \
           && printf '%s' "$info_amp" | grep -q 'Trusted: yes'; then
            ok "amplifier $amp is paired and trusted"
        else
            bad "amplifier $amp is not paired and trusted"
        fi
        if user_ctl is-active --quiet speakerd.service; then
            since=$(epoch "$(user_ctl show --timestamp=unix -p ExecMainStartTimestamp --value speakerd.service)")
            if within 15 sh -c "journalctl _UID=$NAP_UID _SYSTEMD_USER_UNIT=speakerd.service \
                    --since @$since -q --no-pager | grep -q 'amp player registered'"; then
                ok "one player on the amp's adapter: speakerd's /org/speakerd/player"
            else
                bad "speakerd has not registered its player: journalctl --user -u speakerd"
            fi
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

== Next: pair the amplifier
  Pair it from Home Assistant, or the node's own setup page:
      http://$(hostname).local:8080/
EOF
    fi
    cat <<EOF

== Optional: MQTT, for people without Home Assistant

  Add an [mqtt] section to $SPEAKERD_CONF
  (see config/config.example.toml), then:
      sudo -u $NAP_USER XDG_RUNTIME_DIR=/run/user/$NAP_UID systemctl --user restart speakerd
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
printf '\nAll checks passed. AirPlay to "%s".\n' \
    "$(sed -n 's/^[[:space:]]*name = "\([^"]*\)".*/\1/p' "$SHAIRPORT_CONF" | head -1)"
