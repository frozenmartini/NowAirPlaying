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
