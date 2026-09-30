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
