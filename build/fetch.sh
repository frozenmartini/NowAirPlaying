#!/bin/sh
# Download the pinned sources into build/out/src and verify their sha256.
# Runs as a normal user. Safe to re-run: verified files are kept.
set -eu
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/versions.env"
SRC="$HERE/out/src"
mkdir -p "$SRC"

fetch() {  # fetch <file> <url> <sha256>
    f="$SRC/$1"
    if [ -f "$f" ] && echo "$3  $f" | sha256sum -c --status; then
        echo "ok      $1"
        return
    fi
    curl -fsSL -o "$f.part" "$2"
    if ! echo "$3  $f.part" | sha256sum -c --status; then
        echo "CHECKSUM MISMATCH for $1 — refusing to use it" >&2
        sha256sum "$f.part" >&2
        rm -f "$f.part"
        exit 1
    fi
    mv "$f.part" "$f"
    echo "fetched $1"
}

fetch "bluez-$BLUEZ_VERSION.tar.xz"          "$BLUEZ_URL"     "$BLUEZ_SHA256"
fetch "shairport-sync-$SHAIRPORT_VERSION.tar.gz" "$SHAIRPORT_URL" "$SHAIRPORT_SHA256"
fetch "nqptp-$NQPTP_VERSION.tar.gz"          "$NQPTP_URL"     "$NQPTP_SHA256"
