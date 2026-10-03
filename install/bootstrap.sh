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
# install/update.sh runs it the same way for a later release, with no --name:
# from 0.0.2 the room name is owned by speakerd (set over the API), not the
# install, so --name is optional here and the current name is left untouched.
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

# a JSON boolean: true only for "true" or "1", false for everything else
# (including empty, which is every caller that doesn't care about this field)
json_bool() {
    case "$1" in true|1) printf true ;; *) printf false ;; esac
}

log_size() { if [ -f "$LOG" ]; then stat -c %s "$LOG"; else echo 0; fi; }

# is_version V: a release version, N.N.N and nothing else. Not grep -x, which
# passes a multi-line value when any one of its lines matches.
is_version() {
    case "$1" in ''|*[!0-9.]*|.*|*.|*..*) return 1 ;; esac
    [ "$(printf '%s' "$1" | tr -cd .)" = .. ]
}

# is_sha256 V: 64 lowercase hex characters and nothing else
is_sha256() {
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
    [ ${#1} -eq 64 ]
}

# the release that last finished: install.json's "installed" (from 0.0.4),
# or, in an older file, its version when the state is done. Nothing when
# neither is there, or the value isn't a release version: it becomes a path
# root runs a script from (install/update.sh's rollback).
installed_version() {
    iv_file=$STATE_DIR/install.json
    [ -f "$iv_file" ] || return 0
    iv=$(sed -n 's/.*"installed": "\([^"]*\)".*/\1/p' "$iv_file" | head -1)
    if [ -z "$iv" ] && grep -q '"state": "done"' "$iv_file"; then
        iv=$(sed -n 's/.*"version": "\([^"]*\)".*/\1/p' "$iv_file" | head -1)
    fi
    if is_version "$iv"; then printf '%s' "$iv"; fi
}

# the SHA-256 of the API certificate in DER form, lowercase hex, or nothing
# if it isn't there yet. docs/SETUP-API.md "Trust: the pinned certificate".
cert_sha256() {
    f=$STATE_DIR/tls/cert.pem
    [ -f "$f" ] || return 1
    openssl x509 -in "$f" -outform DER 2>/dev/null | sha256sum | cut -c1-64
}

# write_status STATE PHASE PHASE_NAME EXIT REASON MESSAGE [CERT_SHA256] [ROLLED_BACK]
# STATE is installing, failed or done. Written to a temp file and moved into
# place, so a reader never sees half a file. CERT_SHA256, when empty or
# omitted, is read live from the certificate at STATE_DIR/tls/cert.pem (null
# before one exists) -- so Home Assistant, which may re-pin from install.json
# at any time, always sees the certificate the API actually serves, not just
# on a "done" write. A caller passes CERT_SHA256 explicitly only to override
# that live value, which install/update.sh's rollback write does. ROLLED_BACK
# is optional: empty means false, which is every call that predates it.
# "installed" becomes VERSION on done and carries over on every other write.
write_status() {
    mkdir -p "$STATE_DIR"
    chmod 755 "$STATE_DIR"
    if [ "$1" = installing ]; then finished=null; else finished=$(json_str "$(date -Is)"); fi
    cert=${7:-$(cert_sha256 2>/dev/null || true)}
    if [ "$1" = done ]; then installed=$VERSION; else installed=$(installed_version); fi
    tmp=$STATE_DIR/.install.json.$$
    cat > "$tmp" <<EOF
{"state": $(json_str "$1"), "version": $(json_str "$VERSION"),
 "phase": $(json_int "$2"), "phase_name": $(json_str "$3"),
 "started": $(json_str "$STARTED"), "finished": $finished, "exit": $(json_int "$4"),
 "reason": $(json_str "$5"), "message": $(json_str "$6"),
 "log": $(json_str "$LOG"), "log_offset": $(json_int "$LOG_OFFSET"),
 "cert_sha256": $(json_str "$cert"), "rolled_back": $(json_bool "${8:-}"),
 "installed": $(json_str "$installed")}
EOF
    chmod 644 "$tmp"
    mv -f "$tmp" "$STATE_DIR/install.json"
}

# the phase number and name last written, as "N name"
current_phase() {
    sed -n 's/.*"phase": \([0-9]*\), "phase_name": "\([a-z]*\)".*/\1 \2/p' \
        "$STATE_DIR/install.json" 2>/dev/null | head -1
}

# finish_status EXIT [ROLLED_BACK]: the final state of an installer run, from
# its exit status and this run's part of the log. ROLLED_BACK, when given, is
# carried into the final write untouched (install.sh itself never rolls
# back; only install/update.sh passes it, on the write it makes after
# reinstalling the previous release).
finish_status() {
    exit_status=$1; rolled_back=${2:-}
    set -- "$exit_status" $(current_phase)
    if [ "$1" = 0 ]; then
        write_status done 6 verify 0 "" "" "" "$rolled_back"
        return
    fi
    run=$(tail -c +$((LOG_OFFSET + 1)) "$LOG" 2>/dev/null || true)
    msg=$(printf '%s\n' "$run" | sed -n 's/^ERROR: //p' | tail -1)
    if [ -n "$msg" ]; then
        # install.sh's preflight tags its own conflict messages this way
        # (install/install.sh's conflict()), so they get the right reason
        # instead of the generic install_failed.
        case "$msg" in
            "preflight_conflict: "*) reason=preflight_conflict; msg=${msg#preflight_conflict: } ;;
            *) reason=install_failed ;;
        esac
    else
        msg=$(printf '%s\n' "$run" | grep -E '^[0-9]+ check\(s\) FAILED' | tail -1)
        if [ -n "$msg" ]; then reason=verify_failed; else reason=install_failed; msg="exit $1"; fi
    fi
    write_status failed "${2:-1}" "${3:-preflight}" "$1" "$reason" "$msg" "" "$rolled_back"
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

is_version "$VERSION" \
    || { v=$VERSION; VERSION=; bad_args "--version: not a release version: $v"; }
case "$URL" in
    https://*.tar.gz|file:///*.tar.gz) ;;
    *) bad_args "--url: needs an https:// (or file://) address of a .tar.gz: $URL" ;;
esac
is_sha256 "$SHA256" \
    || bad_args "--sha256: needs 64 lowercase hex characters"
[ -n "$USER_NAME" ] && id -u "$USER_NAME" >/dev/null 2>&1 \
    || bad_args "--user: no such user: $USER_NAME"
# --name is optional from 0.0.2: install/update.sh never passes one, and
# install.sh leaves the current name untouched when it gets none.
case "$ROOM" in
    '') ;;
    -*) bad_args "--name must not start with -" ;;
    *'"'*|*'\'*|*/*|*'&'*) bad_args "--name: no quotes, backslashes, / or &" ;;
esac
case "$PHONES" in onboard|dongle) ;; *) bad_args "--phones: onboard or dongle, not $PHONES" ;; esac

# never a release older than the installed one: the same rule as
# install/update.sh, so an older Home Assistant integration can't take a node
# back. The same version is allowed, as a repair.
OLD_VERSION=$(installed_version)
if [ -n "$OLD_VERSION" ] && dpkg --compare-versions "$VERSION" lt "$OLD_VERSION"; then
    fail downgrade 1 "--version $VERSION is older than the installed $OLD_VERSION"
fi

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

# Keep only this version's unpack and the one before it: an update that
# fails rolls back by re-running /opt/nowairplaying/<previous>/install/install.sh
# (install/update.sh), so that one must survive; anything older is clutter.
prune_old_versions() {
    all=$(cd "$PREFIX" && ls -1 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V) || true
    keep=$(printf '%s\n' "$all" | tail -2)
    for v in $all; do
        printf '%s\n' "$keep" | grep -Fxq "$v" && continue
        rm -rf "$PREFIX/$v"
        say "pruned old unpack $PREFIX/$v"
    done
}
prune_old_versions

rm -rf "$D"
trap - EXIT
# the installer's own wrapper logs from here, and keeps this run's start and
# log offset in install.json
set -- --user "$USER_NAME"
[ -n "$ROOM" ] && set -- "$@" --name "$ROOM"
set -- "$@" --phones "$PHONES"
NAP_STARTED=$STARTED NAP_LOG_OFFSET=$LOG_OFFSET NAP_LOG=$LOG NAP_STATE_DIR=$STATE_DIR \
    exec /bin/sh "$DEST/install/install.sh" "$@"
