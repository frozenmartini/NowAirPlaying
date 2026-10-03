#!/bin/sh
# NowAirPlaying update: install another release, triggered by the node API's
# POST /node/update (docs/SETUP-API.md) through the fixed system unit
# systemd/nowairplaying-update.service. A polkit rule
# (polkit/50-nowairplaying.rules) lets "nowairplaying-api" start that one unit
# and nothing else, so this is the only thing of ours that runs as root
# outside the install itself.
#
# Runs from /usr/local/lib/nowairplaying, where install/install.sh copies it
# alongside bootstrap.sh and status.sh (phase 4, "units").
#
# docs/INSTALL-STATE.md "Rollback":
#   1. Read and validate /var/lib/nowairplaying/update/request.json.
#   2. Refuse a version older than the installed one (reason "downgrade").
#      The same version is allowed, as a repair.
#   3. Build the release URL ourselves -- never from the request, so a
#      stolen token can only install a release we actually published.
#   4. Run the installed bootstrap with the --user and --phones recorded by
#      the first install (/var/lib/nowairplaying/install-args). No --name:
#      from 0.0.2 the room name is owned by speakerd, not the install.
#   5. If the new release ends failed with reason verify_failed, or
#      install_failed at phase 4 or later, reinstall the previous release
#      (install/bootstrap.sh never deletes it) and record the NEW version's
#      failure with rolled_back: true.
set -eu

STATE_DIR=${NAP_STATE_DIR:-/var/lib/nowairplaying}
PREFIX=${NAP_PREFIX:-/opt/nowairplaying}
LIBDIR=${NAP_LIBDIR:-/usr/local/lib/nowairplaying}
LOG=${NAP_LOG:-/var/log/nowairplaying-install.log}
REQUEST=$STATE_DIR/update/request.json
ARGS_FILE=$STATE_DIR/install-args

mkdir -p "$(dirname "$LOG")"
say() { printf '%s\n' "$*" | tee -a "$LOG"; }

VERSION=
STARTED=$(date -Is)
LOG_OFFSET=$(if [ -f "$LOG" ]; then stat -c %s "$LOG"; else echo 0; fi)

# shellcheck source=status.sh
. "$LIBDIR/status.sh"

printf '\n##### %s  update %s\n' "$STARTED" "$*" >> "$LOG"

# fail REASON MESSAGE: record the failure (as this run's own version, if we
# got that far) and stop. Never rolls back: a bad request never touched
# anything.
fail() {
    say "ERROR: $2"
    write_status failed "" "" 1 "$1" "$2"
    exit 1
}

# field FILE KEY: one field of a JSON file, or "" if it's missing, null, or
# the file doesn't parse.
field() {
    python3 -c '
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    v = d.get(sys.argv[2])
    print("" if v is None else v)
except Exception:
    print("")
' "$1" "$2"
}

# update/ belongs to nowairplaying-api: never read through a link it planted
[ ! -L "$REQUEST" ] || fail bad_arguments "$REQUEST is a symlink"
[ -f "$REQUEST" ] || fail bad_arguments "no update request at $REQUEST"

NEW_VERSION=$(field "$REQUEST" version)
NEW_SHA=$(field "$REQUEST" sha256)
VERSION=$NEW_VERSION

# The bad value itself is never echoed: install.json is world-readable.
is_version "$NEW_VERSION" \
    || { VERSION=; fail bad_arguments "request.json: version is not a release version (N.N.N)"; }
is_sha256 "$NEW_SHA" \
    || fail bad_arguments "request.json: sha256 needs 64 lowercase hex characters"

[ -f "$ARGS_FILE" ] || fail bad_arguments "no $ARGS_FILE: this node was never installed by install.sh"
USER_NAME=$(sed -n 's/^USER=//p' "$ARGS_FILE" | head -1)
PHONES=$(sed -n 's/^PHONES=//p' "$ARGS_FILE" | head -1)
[ -n "$USER_NAME" ] || fail bad_arguments "$ARGS_FILE has no USER= line"
[ -n "$PHONES" ] || PHONES=onboard

# It becomes a path that root runs a script from (the rollback below), so
# installed_version only gives a strict release version. Nothing means no
# downgrade check and no rollback, never a guess.
OLD_VERSION=$(installed_version)

if [ -n "$OLD_VERSION" ] && dpkg --compare-versions "$NEW_VERSION" lt "$OLD_VERSION"; then
    fail downgrade "requested $NEW_VERSION is older than the installed $OLD_VERSION"
fi

# The URL is ours alone: built here from the version, never taken from the
# request, so a stolen token can only install a release we actually
# published (docs/INSTALL-STATE.md). NAP_RELEASE_BASE only exists so
# tests/test_bootstrap.sh can point this at a file:// fixture instead of
# GitHub; it is never read from $REQUEST.
#
# docs/INSTALL-STATE.md's asset name is trixie-only so far (build/release.sh
# builds no bookworm asset); a bookworm node stays on whatever it already
# has rather than guess a name nobody publishes.
CODENAME=${NAP_CODENAME:-$(. /etc/os-release && echo "${VERSION_CODENAME:-}")}
case "$CODENAME" in
    trixie) ;;
    *) fail install_failed "no release asset is published for $CODENAME yet" ;;
esac
RELEASE_BASE=${NAP_RELEASE_BASE:-https://github.com/frozenmartini/NowAirPlaying/releases/download}
URL="$RELEASE_BASE/v$NEW_VERSION/nowairplaying-$NEW_VERSION-trixie-arm64.tar.gz"

say "NowAirPlaying update: $OLD_VERSION -> $NEW_VERSION"

rc=0
NAP_STATE_DIR=$STATE_DIR NAP_LOG=$LOG NAP_PREFIX=$PREFIX \
    /bin/sh "$LIBDIR/bootstrap.sh" --version "$NEW_VERSION" --url "$URL" --sha256 "$NEW_SHA" \
        --user "$USER_NAME" --phones "$PHONES" || rc=$?

state=$(field "$STATE_DIR/install.json" state)
if [ "$state" = done ]; then
    say "update to $NEW_VERSION done (exit $rc)"
    exit 0
fi

reason=$(field "$STATE_DIR/install.json" reason)
phase=$(field "$STATE_DIR/install.json" phase); phase=${phase:-0}

needs_rollback=0
case "$reason" in
    verify_failed) needs_rollback=1 ;;
    install_failed) [ "$phase" -ge 4 ] && needs_rollback=1 ;;
esac

if [ "$needs_rollback" != 1 ] || [ -z "$OLD_VERSION" ] \
   || [ ! -f "$PREFIX/$OLD_VERSION/install/install.sh" ]; then
    say "update to $NEW_VERSION failed (reason $reason), not rolled back"
    exit 1
fi

# Capture the new version's failure before the rollback reinstall overwrites
# install.json with the old version's (happy) state.
f_version=$NEW_VERSION
f_phase=$phase
f_phase_name=$(field "$STATE_DIR/install.json" phase_name)
f_started=$(field "$STATE_DIR/install.json" started)
f_exit=$(field "$STATE_DIR/install.json" exit)
f_reason=$reason
f_message=$(field "$STATE_DIR/install.json" message)
f_log=$(field "$STATE_DIR/install.json" log)
f_log_offset=$(field "$STATE_DIR/install.json" log_offset)

say "rolling back to $OLD_VERSION"
sh "$PREFIX/$OLD_VERSION/install/install.sh" --user "$USER_NAME" --phones "$PHONES" \
    >> "$LOG" 2>&1 || true

cert=$(cert_sha256 2>/dev/null) || cert=
VERSION=$f_version
STARTED=$f_started
LOG=$f_log
LOG_OFFSET=$f_log_offset
write_status failed "$f_phase" "$f_phase_name" "$f_exit" "$f_reason" "$f_message" "$cert" true

say "update to $f_version failed and was rolled back to $OLD_VERSION"
exit 1
