#!/bin/sh
# Offline tests for install/bootstrap.sh and install/status.sh: download, hash,
# unpack, the failure shapes in install.json, and the installer's final state.
# Runs as a normal user (NAP_TEST=1, everything under a temp dir), fetching a
# stub release over file://. Usage: sh tests/test_bootstrap.sh
set -eu

REPO=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d)
trap '[ -n "${KEEP:-}" ] || rm -rf "$T"' EXIT
PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf 'ok    %s\n' "$*"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  %s\n' "$*"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

export NAP_TEST=1 NAP_PREFIX="$T/opt" NAP_STATE_DIR="$T/state" NAP_LOG="$T/log/install.log"
ME=$(id -un)

# field NAME: one field of install.json, as Python prints it
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
              "$NAP_STATE_DIR/install.json" "$1"; }

# a stub release: VERSION plus an install.sh that records how it was called
mkrelease() { # VER INNER_VERSION -> path of the tarball
    d=$T/src/nowairplaying-$1
    rm -rf "$T/src"; mkdir -p "$d/install"
    [ -z "$2" ] || echo "$2" > "$d/VERSION"
    cat > "$d/install/install.sh" <<'EOF'
printf '%s\n' "$@" > "$NAP_TEST_ARGS"
printf 'offset=%s started=%s\n' "$NAP_LOG_OFFSET" "$NAP_STARTED" >> "$NAP_TEST_ARGS"
EOF
    tar -C "$T/src" -czf "$T/nowairplaying-$1-trixie-arm64.tar.gz" "nowairplaying-$1"
    echo "$T/nowairplaying-$1-trixie-arm64.tar.gz"
}
sha() { sha256sum "$1" | cut -d' ' -f1; }
boot() { sh "$REPO/install/bootstrap.sh" "$@" > "$T/out" 2>&1; }
export NAP_TEST_ARGS="$T/args"

# --- the copy of status.sh inside bootstrap.sh is identical
sed -n '/^# --- status.sh begin/,/^# --- status.sh end/p' "$REPO/install/bootstrap.sh" \
    | sed '1d;$d' > "$T/copy.sh"
check "bootstrap.sh carries an identical copy of status.sh" 'cmp -s "$T/copy.sh" "$REPO/install/status.sh"'

# --- success: fetch, check, unpack, run the installer with the right arguments
tgz=$(mkrelease 9.9.9 9.9.9)
mkdir -p "$T/log"; printf 'earlier run\n' > "$NAP_LOG"
before=$(stat -c %s "$NAP_LOG")
if boot --version 9.9.9 --url "file://$tgz" --sha256 "$(sha "$tgz")" \
        --user "$ME" --name "Bath Room" --phones dongle; then
    ok "success: exit 0"
else
    bad "success: exit $?"; cat "$T/out"
fi
check "success: unpacked into PREFIX/9.9.9" '[ -f "$NAP_PREFIX/9.9.9/install/install.sh" ]'
check "success: no staging dir left" '[ ! -e "$NAP_PREFIX/.9.9.9.new" ]'
check "success: installer got --user --name --phones" \
    '[ "$(head -6 "$T/args" | tr "\n" "|")" = "--user|$ME|--name|Bath Room|--phones|dongle|" ]'
check "success: installer got this run's log offset" 'grep -q "^offset=$before " "$T/args"'
check "success: install.json says installing, phase 0 download" \
    '[ "$(field state) $(field phase) $(field phase_name)" = "installing 0 download" ]'
check "success: log_offset is where this run starts" '[ "$(field log_offset)" = "$before" ]'
check "success: install.json is 0644" '[ "$(stat -c %a "$NAP_STATE_DIR/install.json")" = 644 ]'

# --- a re-run replaces the unpacked release
echo stale > "$NAP_PREFIX/9.9.9/stale"
boot --version 9.9.9 --url "file://$tgz" --sha256 "$(sha "$tgz")" --user "$ME" --name X || true
check "re-run: replaces the old unpack" '[ ! -e "$NAP_PREFIX/9.9.9/stale" ]'

# failcase NAME REASON ARGS...: bootstrap must fail and record REASON
failcase() {
    name=$1 reason=$2; shift 2
    if boot "$@"; then bad "$name: exit 0"; return; fi
    check "$name: state failed, reason $reason" \
        '[ "$(field state) $(field reason)" = "failed $reason" ]'
    check "$name: message is set" '[ "$(field message)" != None ]'
    check "$name: finished is set" '[ "$(field finished)" != None ]'
}

failcase "hash" hash_mismatch --version 9.9.9 --url "file://$tgz" \
    --sha256 "$(printf '0%.0s' $(seq 64))" --user "$ME" --name X
failcase "download" download_failed --version 9.9.9 --url "file://$T/missing.tar.gz" \
    --sha256 "$(sha "$tgz")" --user "$ME" --name X
check "download: exit is curl's (37)" '[ "$(field exit)" = 37 ]'

printf 'not a tarball' > "$T/junk.tar.gz"
rm -rf "$NAP_PREFIX"
failcase "unpack" unpack_failed --version 9.9.9 --url "file://$T/junk.tar.gz" \
    --sha256 "$(sha "$T/junk.tar.gz")" --user "$ME" --name X
check "unpack: nothing left in PREFIX" '[ -z "$(ls -A "$NAP_PREFIX" 2>/dev/null)" ]'

tgz2=$(mkrelease 9.9.8 9.9.7)
mv "$tgz2" "$T/nowairplaying-9.9.8-trixie-arm64.tar.gz" 2>/dev/null || true
failcase "inner version" unpack_failed --version 9.9.8 \
    --url "file://$T/nowairplaying-9.9.8-trixie-arm64.tar.gz" \
    --sha256 "$(sha "$T/nowairplaying-9.9.8-trixie-arm64.tar.gz")" --user "$ME" --name X

failcase "bad version" bad_arguments --version 'latest' --url "file://$tgz" \
    --sha256 "$(sha "$tgz")" --user "$ME" --name X
check "bad version: version is null" '[ "$(field version)" = None ]'
failcase "bad url" bad_arguments --version 9.9.9 --url "http://example.com/x.tar.gz" \
    --sha256 "$(sha "$tgz")" --user "$ME" --name X
failcase "bad phones" bad_arguments --version 9.9.9 --url "file://$tgz" \
    --sha256 "$(sha "$tgz")" --user "$ME" --name X --phones both
failcase "dash name" bad_arguments --version 9.9.9 --url "file://$tgz" \
    --sha256 "$(sha "$tgz")" --user "$ME" --name -x
failcase "quote in message" bad_arguments --version 9.9.9 --url "file://$tgz" \
    --sha256 "$(sha "$tgz")" --user "$ME" --name 'a"b'
failcase "no such user" bad_arguments --version 9.9.9 --url "file://$tgz" \
    --sha256 "$(sha "$tgz")" --user nosuchuser-nap --name X

# --- status.sh: the installer's final state from its exit and its log
(
    VERSION=1.0.0 STARTED=now LOG=$T/fin.log
    printf 'old run\nERROR: from an earlier run\n' > "$LOG"
    LOG_OFFSET=$(stat -c %s "$LOG")
    . "$REPO/install/status.sh"
    write_status installing 3 packages "" "" ""
    printf '\nERROR: apt would remove foo, "quoted"\\n\n' >> "$LOG"
    finish_status 1
    printf "%s\n" "$(field state)|$(field phase)|$(field phase_name)|$(field reason)|$(field message)" > "$T/f1"
    LOG_OFFSET=$(stat -c %s "$LOG")
    write_status installing 6 verify "" "" ""
    printf '   FAIL  nqptp\n\n2 check(s) FAILED.\n' >> "$LOG"
    finish_status 1
    printf "%s\n" "$(field reason)|$(field message)" > "$T/f2"
    finish_status 0
    printf "%s\n" "$(field state)|$(field phase)|$(field exit)|$(field reason)" > "$T/f3"
)
check "finish: ERROR line gives install_failed at the phase reached" \
    '[ "$(cat "$T/f1")" = "failed|3|packages|install_failed|apt would remove foo, \"quoted\"\\n" ]'
check "finish: only this run's part of the log counts" '! grep -q earlier "$T/f1"'
check "finish: failed checks give verify_failed" \
    '[ "$(cat "$T/f2")" = "verify_failed|2 check(s) FAILED." ]'
check "finish: exit 0 gives done" '[ "$(cat "$T/f3")" = "done|6|0|None" ]'

# --- the real install.sh: its re-run under the log wrapper keeps every option.
# As fake root in a user namespace, with a user that doesn't exist, the inner
# run stops right after parsing its options, before it changes anything.
if unshare -r true 2>/dev/null; then
    rm -rf "$NAP_STATE_DIR"; : > "$NAP_LOG"
    unshare -r sh "$REPO/install/install.sh" --user nosuchuser-nap --name X --phones dongle \
        > "$T/inst.out" 2>&1 || true
    check "install.sh: the inner run got the options (no such user, not 'no target user')" \
        'grep -q "ERROR: no such user: nosuchuser-nap" "$T/inst.out"'
    check "install.sh: the log header shows the options" \
        'grep -q "^##### .* --user nosuchuser-nap --name X --phones dongle$" "$NAP_LOG"'
    check "install.sh: failure recorded as install_failed at preflight" \
        '[ "$(field state)|$(field phase_name)|$(field reason)|$(field message)|$(field version)" = "failed|preflight|install_failed|no such user: nosuchuser-nap|$(cat "$REPO/VERSION")" ]'
    rm -rf "$NAP_STATE_DIR"
    unshare -r sh "$REPO/install/install.sh" --verify --user nosuchuser-nap > /dev/null 2>&1 || true
    check "install.sh --verify: install.json untouched" '[ ! -e "$NAP_STATE_DIR/install.json" ]'
else
    echo "skip  install.sh wrapper tests: no user namespaces here"
fi

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
