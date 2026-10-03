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

# --- status.sh: cert_sha256 (0.0.2) is null until a certificate exists, and
# from there on is read live on EVERY write -- installing, done or failed --
# not only on a "done" write (Home Assistant may re-pin from install.json at
# any time, so it must always see the certificate the API actually serves).
# An explicit 7th argument still overrides the live value, which
# install/update.sh's rollback write uses. rolled_back defaults false and is
# carried through when a caller passes it.
(
    VERSION=2.0.0 STARTED=now LOG=$T/cert.log
    : > "$LOG"
    LOG_OFFSET=0
    . "$REPO/install/status.sh"

    # no certificate yet: null on both a "done" and an "installing" write
    finish_status 0
    printf '%s\n' "$(field cert_sha256)|$(field rolled_back)" > "$T/g1"
    write_status installing 3 packages "" "" ""
    printf '%s\n' "$(field cert_sha256)|$(field rolled_back)" > "$T/g1b"

    mkdir -p "$STATE_DIR/tls"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 1 \
        -subj "/CN=test" -keyout "$STATE_DIR/tls/key.pem" -out "$STATE_DIR/tls/cert.pem" \
        >/dev/null 2>&1
    want=$(openssl x509 -in "$STATE_DIR/tls/cert.pem" -outform DER | sha256sum | cut -c1-64)

    # once a certificate exists, a "done" write carries it
    finish_status 0
    printf '%s\n' "$(field cert_sha256)|$want" > "$T/g2"

    # ... and so does a mid-run "installing" write
    write_status installing 3 packages "" "" ""
    printf '%s\n' "$(field cert_sha256)|$want" > "$T/g3"

    # ... and a "failed" write
    finish_status 1
    printf '%s\n' "$(field cert_sha256)|$want" > "$T/g3b"

    # an explicit 7th argument overrides the live value
    write_status installing 3 packages "" "" "" deadbeef
    printf '%s\n' "$(field cert_sha256)" > "$T/g3c"

    finish_status 1 true
    printf '%s\n' "$(field rolled_back)" > "$T/g4"
)
check "write_status done: cert_sha256 is null before a certificate exists, rolled_back false" \
    '[ "$(cat "$T/g1")" = "None|False" ]'
check "write_status installing: cert_sha256 is also null before a certificate exists" \
    '[ "$(cat "$T/g1b")" = "None|False" ]'
check "write_status done: cert_sha256 matches the certificate once one exists" \
    '[ -n "$(cut -d"|" -f2 "$T/g2")" ] && [ "$(cut -d"|" -f1 "$T/g2")" = "$(cut -d"|" -f2 "$T/g2")" ]'
check "write_status installing: cert_sha256 is written mid-run too, once a certificate exists" \
    '[ "$(cut -d"|" -f1 "$T/g3")" = "$(cut -d"|" -f2 "$T/g3")" ]'
check "finish_status failed: cert_sha256 is written on a failed write too" \
    '[ "$(cut -d"|" -f1 "$T/g3b")" = "$(cut -d"|" -f2 "$T/g3b")" ]'
check "write_status: an explicit 7th argument overrides the live certificate value" \
    '[ "$(cat "$T/g3c")" = deadbeef ]'
check "finish_status EXIT ROLLED_BACK: true is carried into the failed write" \
    '[ "$(cat "$T/g4")" = True ]'

# --- install/update.sh: request validation, downgrade, the URL it builds,
# and the rollback on a failed update (docs/INSTALL-STATE.md "Rollback")
UT=$T/ustate
UPFX=$T/uopt
ME=$(id -un)

reset_update_state() {
    rm -rf "$UT" "$UPFX" "$T/src" "$T/v"*
    mkdir -p "$UT/update"
}

seed_installed() { # VERSION: a prior install.json in state "done"
    cat > "$UT/install.json" <<EOF
{"state": "done", "version": "$1", "phase": 6, "phase_name": "verify",
 "started": "2026-01-01T00:00:00+00:00", "finished": "2026-01-01T00:05:00+00:00", "exit": 0,
 "reason": null, "message": null, "log": "$UT/install.log", "log_offset": 0,
 "cert_sha256": null, "rolled_back": false}
EOF
}

seed_old_unpack() { # VERSION: a previous release's unpack, as bootstrap.sh keeps it
    mkdir -p "$UPFX/$1/install"
    printf '#!/bin/sh\nexit 0\n' > "$UPFX/$1/install/install.sh"
    chmod +x "$UPFX/$1/install/install.sh"
}

write_install_args() { # USER PHONES
    printf 'USER=%s\nPHONES=%s\n' "$1" "$2" > "$UT/install-args"
}

write_request() { # VERSION SHA256 [EXTRA_JSON_FIELD]
    printf '{"version": "%s", "sha256": "%s"%s}\n' "$1" "$2" "${3:-}" > "$UT/update/request.json"
}

# a fake release whose install.sh records its own arguments and then plays
# out one outcome, without doing any real installation -- update.sh's own
# logic (did it roll back? what did it write?) is what's under test, not a
# real install
mkfakerelease() { # VERSION OUTCOME
    ver=$1
    case "$2" in
        done)               line='write_status done 6 verify 0 "" ""; exit 0' ;;
        verify_failed)      line='write_status failed 6 verify 1 verify_failed "2 check(s) FAILED."; exit 1' ;;
        install_failed_late) line='write_status failed 4 units 1 install_failed "simulated unit failure"; exit 1' ;;
        install_failed_early) line='write_status failed 1 preflight 1 install_failed "simulated preflight failure"; exit 1' ;;
    esac
    d=$T/src/nowairplaying-$ver
    rm -rf "$T/src"; mkdir -p "$d/install"
    echo "$ver" > "$d/VERSION"
    cat > "$d/install/install.sh" <<EOF
#!/bin/sh
set -eu
printf '%s\n' "\$@" > "$UT/args.received"
VERSION=$ver
STARTED=\${NAP_STARTED:-\$(date -Is)}
LOG=\${NAP_LOG:-$UT/install.log}
LOG_OFFSET=\${NAP_LOG_OFFSET:-0}
. "$REPO/install/status.sh"
$line
EOF
    mkdir -p "$T/v$ver"
    tar -C "$T/src" -czf "$T/v$ver/nowairplaying-$ver-trixie-arm64.tar.gz" "nowairplaying-$ver"
}

run_update() {
    NAP_STATE_DIR=$UT NAP_PREFIX=$UPFX NAP_LIBDIR="$REPO/install" NAP_LOG=$UT/install.log \
        NAP_RELEASE_BASE="file://$T" NAP_CODENAME=trixie \
        sh "$REPO/install/update.sh" > "$UT/out" 2>&1
}
ufield() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' \
    "$UT/install.json" "$1"; }

reset_update_state
write_install_args "$ME" onboard
write_request 'not-a-version' "$(printf '0%.0s' $(seq 64))"
run_update || true
check "update.sh: bad version in request.json -> bad_arguments" \
    '[ "$(ufield state)|$(ufield reason)" = "failed|bad_arguments" ]'

reset_update_state
write_install_args "$ME" onboard
write_request 9.9.9 short
run_update || true
check "update.sh: bad sha256 in request.json -> bad_arguments" \
    '[ "$(ufield state)|$(ufield reason)" = "failed|bad_arguments" ]'

reset_update_state
write_request 9.9.9 "$(printf '0%.0s' $(seq 64))"
run_update || true
check "update.sh: no install-args (never installed by install.sh) -> bad_arguments" \
    '[ "$(ufield state)|$(ufield reason)" = "failed|bad_arguments" ]'

reset_update_state
write_install_args "$ME" onboard
seed_installed 5.0.0
write_request 4.0.0 "$(printf '0%.0s' $(seq 64))"
run_update || true
check "update.sh: a version older than the installed one -> downgrade, not install_failed" \
    '[ "$(ufield state)|$(ufield version)|$(ufield reason)" = "failed|4.0.0|downgrade" ]'

reset_update_state
write_install_args "$ME" onboard
seed_installed 1.0.0
mkfakerelease 1.0.0 done
sha_match=$(sha "$T/v1.0.0/nowairplaying-1.0.0-trixie-arm64.tar.gz")
write_request 1.0.0 "$sha_match"
run_update
check "update.sh: the same version as installed is allowed, as a repair" \
    '[ "$(ufield state)|$(ufield version)|$(ufield reason)" = "done|1.0.0|None" ]'

reset_update_state
write_install_args "$ME" dongle
seed_installed 1.0.0
mkfakerelease 2.0.0 done
sha2=$(sha "$T/v2.0.0/nowairplaying-2.0.0-trixie-arm64.tar.gz")
write_request 2.0.0 "$sha2" ', "url": "https://evil.example.com/pwned.tar.gz"'
run_update
check "update.sh: a bogus url in the request is ignored; ours is built and fetched" \
    '[ "$(ufield state)|$(ufield version)" = "done|2.0.0" ]'
check "update.sh: install-args round trip (--user --phones reached the release's installer)" \
    '[ "$(tr "\n" "|" < "$UT/args.received")" = "--user|$ME|--phones|dongle|" ]'
check "update.sh never reads a url out of its own request" \
    '! grep -q "field .\$REQUEST. url" "$REPO/install/update.sh"'

reset_update_state
write_install_args "$ME" onboard
seed_installed 1.0.0
seed_old_unpack 1.0.0
mkfakerelease 2.0.0 verify_failed
sha2=$(sha "$T/v2.0.0/nowairplaying-2.0.0-trixie-arm64.tar.gz")
write_request 2.0.0 "$sha2"
run_update || true
check "update.sh: verify_failed rolls back, and keeps the new version's reason" \
    '[ "$(ufield state)|$(ufield version)|$(ufield reason)|$(ufield rolled_back)" = "failed|2.0.0|verify_failed|True" ]'

reset_update_state
write_install_args "$ME" onboard
seed_installed 1.0.0
seed_old_unpack 1.0.0
mkfakerelease 2.0.0 install_failed_late
sha2=$(sha "$T/v2.0.0/nowairplaying-2.0.0-trixie-arm64.tar.gz")
write_request 2.0.0 "$sha2"
run_update || true
check "update.sh: install_failed at phase 4 (units) or later also rolls back" \
    '[ "$(ufield state)|$(ufield reason)|$(ufield rolled_back)" = "failed|install_failed|True" ]'

reset_update_state
write_install_args "$ME" onboard
seed_installed 1.0.0
seed_old_unpack 1.0.0
mkfakerelease 2.0.0 install_failed_early
sha2=$(sha "$T/v2.0.0/nowairplaying-2.0.0-trixie-arm64.tar.gz")
write_request 2.0.0 "$sha2"
run_update || true
check "update.sh: install_failed before phase 4 does not roll back" \
    '[ "$(ufield state)|$(ufield version)|$(ufield reason)|$(ufield rolled_back)" = "failed|2.0.0|install_failed|False" ]'

reset_update_state
write_install_args "$ME" onboard
seed_installed 1.0.0
# no seed_old_unpack: nothing to roll back to, even though verify_failed asks for one
mkfakerelease 2.0.0 verify_failed
sha2=$(sha "$T/v2.0.0/nowairplaying-2.0.0-trixie-arm64.tar.gz")
write_request 2.0.0 "$sha2"
run_update || true
check "update.sh: verify_failed with no previous unpack on disk -> not rolled back" \
    '[ "$(ufield state)|$(ufield reason)|$(ufield rolled_back)" = "failed|verify_failed|False" ]'

# --- the review's F1/F4: what root reads back is checked strictly
reset_update_state
write_install_args "$ME" onboard
write_request '1.0.0\nnot-checked' "$(printf '0%.0s' $(seq 64))"
run_update || true
check "update.sh: a multi-line version (one good line) -> bad_arguments" \
    '[ "$(ufield state)|$(ufield reason)" = "failed|bad_arguments" ]'
check "update.sh: the bad version is not echoed into install.json" \
    '! grep -q not-checked "$UT/install.json"'

reset_update_state
write_install_args "$ME" onboard
write_request 2.0.0 "$(printf '0%.0s' $(seq 64))"
mv "$UT/update/request.json" "$UT/real-request.json"
ln -s "$UT/real-request.json" "$UT/update/request.json"
run_update || true
check "update.sh: a request.json that is a symlink -> bad_arguments" \
    '[ "$(ufield state)|$(ufield reason)" = "failed|bad_arguments" ]'

reset_update_state
write_install_args "$ME" onboard
seed_installed '1.0.0/../evil'
seed_old_unpack 1.0.0
mkdir -p "$UPFX/evil/install"
printf '#!/bin/sh\ntouch "%s"\n' "$UT/evil-ran" > "$UPFX/evil/install/install.sh"
mkfakerelease 2.0.0 verify_failed
sha2=$(sha "$T/v2.0.0/nowairplaying-2.0.0-trixie-arm64.tar.gz")
write_request 2.0.0 "$sha2"
run_update || true
check "update.sh: a path-like installed version never becomes the rollback script" \
    '[ ! -e "$UT/evil-ran" ] && [ "$(ufield reason)|$(ufield rolled_back)" = "verify_failed|False" ]'

check "is_version / is_sha256: strict, one line, nothing else" \
    '( . "$REPO/install/status.sh"
       is_version 0.0.2 && is_version 10.20.30 && ! is_version "" && ! is_version 1.2 \
       && ! is_version 1.2.3.4 && ! is_version .1.2 && ! is_version 1..2 && ! is_version "1.2.3
x" && ! is_version 1.2.3/.. && is_sha256 "$(printf "a%.0s" $(seq 64))" \
       && ! is_sha256 "$(printf "A%.0s" $(seq 64))" && ! is_sha256 abc \
       && ! is_sha256 "$(printf "a%.0s" $(seq 64))
$(printf "a%.0s" $(seq 64))" )'

# --- install.sh: put returns 1 for an unchanged file, so a bare call ends the
# install silently under set -e (the first .156 run, 2026-10-03, at
# 20auto-upgrades, which trixie's unattended-upgrades had already written)
check "install.sh: every put is under if, or has || true" \
    '! grep -nE "^[[:space:]]*put " "$REPO/install/install.sh" | grep -v "|| true\$"'

# --- polkit/50-nowairplaying.rules: Wi-Fi moved off polkit entirely, and
# nowairplaying-api, not nowairplaying, is now the only subject
check "polkit rule: no NetworkManager action (Wi-Fi no longer goes through polkit)" \
    '! grep -q "org.freedesktop.NetworkManager" "$REPO/polkit/50-nowairplaying.rules"'
check "polkit rule: nowairplaying-api is the subject" \
    'grep -q "subject.user != \"nowairplaying-api\"" "$REPO/polkit/50-nowairplaying.rules"'
check "polkit rule: the audio account (nowairplaying) is named nowhere as a subject" \
    '! grep -q "subject.user .= \"nowairplaying\"" "$REPO/polkit/50-nowairplaying.rules"'

# --- polkit/50-nowairplaying.rules: valid JS, if this host has node to check it with
if command -v node >/dev/null 2>&1; then
    # node --check picks its parser from the file extension, and .rules isn't
    # one it knows, so check a .js copy of the same content.
    cp "$REPO/polkit/50-nowairplaying.rules" "$T/50-nowairplaying.js"
    check "polkit/50-nowairplaying.rules is syntactically valid JavaScript" \
        'node --check "$T/50-nowairplaying.js"'
else
    echo "skip  polkit JS syntax check: no node on this host"
fi

# --- install/wifi-add.sh: a thin root wrapper, never a password on argv
check "wifi-add.sh execs speakerd.wifi_add, forwarding its own arguments" \
    'grep -q '"'"'exec env PYTHONPATH=/opt/nowairplaying /usr/bin/python3 -m speakerd.wifi_add "$@"'"'"' \
        "$REPO/install/wifi-add.sh"'
check "wifi-add.sh takes no password of its own (comments aside): the only non-comment, non-blank line is the exec" \
    '[ "$(grep -vE "^#|^[[:space:]]*$|^set -eu$" "$REPO/install/wifi-add.sh" | wc -l)" = 1 ]'

# --- systemd/nowairplaying-api.service: its own account, locked down
check "nowairplaying-api.service runs as its own account" \
    'grep -q "^User=nowairplaying-api$" "$REPO/systemd/nowairplaying-api.service" \
        && grep -q "^Group=nowairplaying-api$" "$REPO/systemd/nowairplaying-api.service"'
check "nowairplaying-api.service sets NoNewPrivileges=yes" \
    'grep -q "^NoNewPrivileges=yes$" "$REPO/systemd/nowairplaying-api.service"'

printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
[ "$FAIL" = 0 ]
