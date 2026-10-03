"""Add a Wi-Fi network: `python3 -m speakerd.wifi_add [--file F [--delete]]`.

Run as root only, through /usr/local/lib/nowairplaying/wifi-add.sh, in two ways
(docs/SETUP-API.md#wi-fi):
- Home Assistant over SSH: `sudo -S /usr/local/lib/nowairplaying/wifi-add.sh`,
  the sudo password as stdin's first line, then the lines below.
- nowairplaying-wifi.service at boot, with the file the owner put on the boot
  partition: `--file /boot/firmware/nowairplaying-wifi.txt --delete`.

The input, from stdin or the file:

    ssid=Home Network
    password=the password
    hidden=yes          (optional; for a network that doesn't broadcast its name)

One key=value per line; the value is everything after the first "=", taken
as is, so nothing needs quoting. Blank lines and lines starting with # are
skipped; a Windows editor's BOM and CRLF line ends are fine. No password line
means an open network.

The network is added alongside the existing ones, never instead of them, and
nothing is switched: NetworkManager uses whichever known network is in range.
An earlier profile this tool made for the same SSID is replaced.

Prints one line, "ok: ..." or "error: ...", and exits 0 on success, 2 for bad
input, 1 when NetworkManager refused. --delete removes the file whatever the
outcome, since it holds a password.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from .netman import NmClient, NmError

NM_WAIT_S = 30


class InputError(Exception):
    pass


def parse(text: str) -> tuple[str, str | None, bool]:
    fields: dict[str, str] = {}
    for raw in text.lstrip("﻿").splitlines():
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip().lower()
        if not sep or key not in ("ssid", "password", "hidden"):
            raise InputError(f"unexpected line: {key or line!r} (want ssid=, password=, hidden=)")
        if key in fields:
            raise InputError(f"{key} is given twice")
        fields[key] = value
    ssid = fields.get("ssid", "")
    if not 1 <= len(ssid.encode()) <= 32:
        raise InputError("ssid: 1-32 bytes")
    password = fields.get("password") or None
    if password is not None and not 8 <= len(password) <= 63:
        raise InputError("password: 8-63 characters, or no password line for an open network")
    hidden = fields.get("hidden", "no").strip().lower()
    if hidden not in ("yes", "no", "true", "false", "1", "0"):
        raise InputError("hidden: yes or no")
    return ssid, password, hidden in ("yes", "true", "1")


async def add(ssid: str, password: str | None, hidden: bool) -> None:
    nm = NmClient()
    await nm.start()
    try:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + NM_WAIT_S
        while True:  # at boot NetworkManager may still be starting
            try:
                await nm.add_wifi(ssid, password, hidden)
                return
            except NmError:
                if loop.time() >= deadline:
                    raise
                await asyncio.sleep(2)
    finally:
        await nm.stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="wifi-add")
    p.add_argument("--file", help="read the network from this file instead of stdin")
    p.add_argument("--delete", action="store_true", help="delete --file afterwards")
    args = p.parse_args()
    if args.delete and not args.file:
        p.error("--delete needs --file")
    code = 0
    try:
        if args.file:
            with open(args.file, encoding="utf-8-sig", errors="strict") as f:
                text = f.read()
        else:
            text = sys.stdin.read()
        ssid, password, hidden = parse(text)
        asyncio.run(add(ssid, password, hidden))
        print(f"ok: added {ssid!r}" + (" (hidden)" if hidden else ""))
    except (InputError, UnicodeDecodeError) as e:
        print(f"error: {e}")
        code = 2
    except OSError as e:
        print(f"error: {e}")
        code = 2
    except NmError as e:
        print(f"error: NetworkManager refused: {e}")
        code = 1
    finally:
        if args.delete:
            try:
                os.remove(args.file)
            except FileNotFoundError:
                pass
    sys.exit(code)


if __name__ == "__main__":
    main()
