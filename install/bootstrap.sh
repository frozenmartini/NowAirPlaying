#!/bin/sh
# NowAirPlaying bootstrap: fetch one pinned release, check its hash, unpack it
# and run its installer.
#
# Home Assistant ships a copy of this file with each kohler_anthem_plus version,
# the copy from the release that version pins. It uploads it over SFTP and runs
# it as root in a transient unit (docs/INSTALL-STATE.md):
#
#   systemd-run --unit=nowairplaying-install --collect --quiet /bin/sh bootstrap.sh \
#       --version 0.0.1 --url https://.../nowairplaying-0.0.1-trixie-arm64.tar.gz \
#       --sha256 <64 hex> --user NAME --name "Bathroom" --phones onboard
#
# It writes /var/lib/nowairplaying/install.json before fetching (phase 0,
# "download"), and "failed" with a reason if the arguments, the download, the
# hash or the unpack fail. The installer keeps install.json up to date from
# there on.
set -eu

PREFIX=${NAP_PREFIX:-/opt/nowairplaying}
LOG=${NAP_LOG:-/var/log/nowairplaying-install.log}

# --- status.sh begin (identical to install/status.sh)
# install.json: the install state Home Assistant reads over SSH, without sudo
# (docs/INSTALL-STATE.md). Sourced by install.sh. bootstrap.sh carries an
# identical copy between its "status.sh" markers, because it runs before any
# release is on the Pi; tests/test_bootstrap.sh keeps the two the same.
#
# Callers set VERSION, STARTED, LOG and LOG_OFFSET first.

STATE_DIR=${NAP_STATE_DIR:-/var/lib/nowairplaying}

# a JSON string literal, or null when empty. Newlines and tabs become spaces,
# other control characters are dropped.
json_str() {
    if [ -z "$1" ]; then printf null; return; fi
    printf '"%s"' "$(printf '%s' "$1" | tr '\n\t' '  ' | tr -d '\000-\037' \
        | sed 's/\\/\\\\/g; s/"/\\"/g')"
}

# a JSON integer, or null when empty or not a number
json_int() {
    case "$1" in ''|*[!0-9]*) printf null ;; *) printf '%s' "$1" ;; esac
}

log_size() { if [ -f "$LOG" ]; then stat -c %s "$LOG"; else echo 0; fi; }

# write_status STATE PHASE PHASE_NAME EXIT REASON MESSAGE
# STATE is installing, failed or done. Written to a temp file and moved into
# place, so a reader never sees half a file.
write_status() {
    mkdir -p "$STATE_DIR"
    chmod 755 "$STATE_DIR"
    if [ "$1" = installing ]; then finished=null; else finished=$(json_str "$(date -Is)"); fi
    tmp=$STATE_DIR/.install.json.$$
    cat > "$tmp" <<EOF
{"state": $(json_str "$1"), "version": $(json_str "$VERSION"),
 "phase": $(json_int "$2"), "phase_name": $(json_str "$3"),
 "started": $(json_str "$STARTED"), "finished": $finished, "exit": $(json_int "$4"),
 "reason": $(json_str "$5"), "message": $(json_str "$6"),
 "log": $(json_str "$LOG"), "log_offset": $(json_int "$LOG_OFFSET")}
EOF
    chmod 644 "$tmp"
    mv -f "$tmp" "$STATE_DIR/install.json"
}

# the phase number and name last written, as "N name"
current_phase() {
    sed -n 's/.*"phase": \([0-9]*\), "phase_name": "\([a-z]*\)".*/\1 \2/p' \
        "$STATE_DIR/install.json" 2>/dev/null | head -1
}

# finish_status EXIT: the final state of an installer run, from its exit
# status and this run's part of the log
finish_status() {
    set -- "$1" $(current_phase)
    if [ "$1" = 0 ]; then
        write_status done 6 verify 0 "" ""
        return
    fi
    run=$(tail -c +$((LOG_OFFSET + 1)) "$LOG" 2>/dev/null || true)
    msg=$(printf '%s\n' "$run" | sed -n 's/^ERROR: //p' | tail -1)
    if [ -n "$msg" ]; then
        reason=install_failed
    else
        msg=$(printf '%s\n' "$run" | grep -E '^[0-9]+ check\(s\) FAILED' | tail -1)
        if [ -n "$msg" ]; then reason=verify_failed; else reason=install_failed; msg="exit $1"; fi
    fi
    write_status failed "${2:-1}" "${3:-preflight}" "$1" "$reason" "$msg"
}
# --- status.sh end

say() { printf '%s\n' "$*" | tee -a "$LOG"; }

# fail REASON EXIT MESSAGE: record the failure and stop
fail() {
    say "ERROR: $3"
    write_status failed 0 download "$2" "$1" "$3"
    exit "$2"
}

VERSION= URL= SHA256= USER_NAME= ROOM= PHONES=onboard
STARTED=$(date -Is)
LOG_OFFSET=$(log_size)

[ "$(id -u)" = 0 ] || [ -n "${NAP_TEST:-}" ] || { echo "bootstrap.sh runs as root" >&2; exit 1; }
mkdir -p "$(dirname "$LOG")"
printf '\n##### %s  bootstrap %s\n' "$STARTED" "$*" >> "$LOG"

bad_args() { fail bad_arguments 2 "$1"; }
while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || bad_args "$1 needs a value"
    case "$1" in
        --version) VERSION=$2 ;;
        --url)     URL=$2 ;;
        --sha256)  SHA256=$2 ;;
        --user)    USER_NAME=$2 ;;
        --name)    ROOM=$2 ;;
        --phones)  PHONES=$2 ;;
        *)         bad_args "unknown option: $1" ;;
    esac
    shift 2
done

printf '%s' "$VERSION" | grep -Eqx '[0-9]+\.[0-9]+\.[0-9]+' \
    || { v=$VERSION; VERSION=; bad_args "--version: not a release version: $v"; }
case "$URL" in
    https://*.tar.gz|file:///*.tar.gz) ;;
    *) bad_args "--url: needs an https:// (or file://) address of a .tar.gz: $URL" ;;
esac
printf '%s' "$SHA256" | grep -Eqx '[0-9a-f]{64}' \
    || bad_args "--sha256: needs 64 lowercase hex characters"
[ -n "$USER_NAME" ] && id -u "$USER_NAME" >/dev/null 2>&1 \
    || bad_args "--user: no such user: $USER_NAME"
case "$ROOM" in
    '') bad_args "--name: a room name is required" ;;
    -*) bad_args "--name must not start with -" ;;
    *'"'*|*'\'*|*/*|*'&'*) bad_args "--name: no quotes, backslashes, / or &" ;;
esac
case "$PHONES" in onboard|dongle) ;; *) bad_args "--phones: onboard or dongle, not $PHONES" ;; esac

write_status installing 0 download "" "" ""
say "NowAirPlaying $VERSION: fetching $URL"

D=$(mktemp -d)
trap 'rm -rf "$D"' EXIT
file=$D/${URL##*/}

curl -fsSL --proto '=https,file' --proto-redir '=https' --retry 3 \
        --connect-timeout 20 -o "$file" "$URL" 2> "$D/curl.err" \
    || fail download_failed "$?" "$(tail -1 "$D/curl.err")"

got=$(sha256sum "$file" | cut -d' ' -f1)
[ "$got" = "$SHA256" ] || fail hash_mismatch 1 "sha256 is $got, expected $SHA256"
say "sha256 matches"

DEST=$PREFIX/$VERSION
STAGE=$PREFIX/.$VERSION.new
rm -rf "$STAGE"
mkdir -p "$STAGE"
tar -xzf "$file" --no-same-owner --strip-components=1 -C "$STAGE" 2> "$D/tar.err" \
    || { rm -rf "$STAGE"; fail unpack_failed 1 "$(tail -1 "$D/tar.err")"; }
[ -f "$STAGE/install/install.sh" ] \
    || { rm -rf "$STAGE"; fail unpack_failed 1 "the release has no install/install.sh"; }
inner=$(cat "$STAGE/VERSION" 2>/dev/null || true)
[ "$inner" = "$VERSION" ] \
    || { rm -rf "$STAGE"; fail unpack_failed 1 "the release says version ${inner:-none}, expected $VERSION"; }
rm -rf "$DEST"
mv "$STAGE" "$DEST"
say "unpacked into $DEST"

rm -rf "$D"
trap - EXIT
# the installer's own wrapper logs from here, and keeps this run's start and
# log offset in install.json
NAP_STARTED=$STARTED NAP_LOG_OFFSET=$LOG_OFFSET NAP_LOG=$LOG NAP_STATE_DIR=$STATE_DIR \
    exec /bin/sh "$DEST/install/install.sh" --user "$USER_NAME" --name "$ROOM" --phones "$PHONES"
