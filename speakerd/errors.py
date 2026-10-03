"""What both sides of the node API share: its error type and input checks.

speakerd raises ApiError for a bad audio command; the control socket carries
it to the API service (control.py), which returns it to the HTTP client as
{"error": code, "message": message}.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
# quotes, backslash, / and & break the installer's sed and shairport-sync's
# config string; a control character (a newline) breaks the config's line
_BAD_NAME = re.compile(r'["\\/&\x00-\x1f\x7f]')


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = ""):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code

    def as_dict(self) -> dict:
        return {"status": self.status, "code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, d: dict) -> "ApiError":
        return cls(int(d.get("status", 502)), str(d.get("code", "failed")),
                   str(d.get("message", "")))


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def valid_name(name) -> str:
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 40 or _BAD_NAME.search(name):
        raise ApiError(400, "bad_request",
                       "name: 1-40 characters, no quotes, backslashes, /, & or control characters")
    return name.strip()


def canon_mac(raw) -> str:
    mac = str(raw or "").strip().upper().replace("-", ":")
    if not _MAC_RE.match(mac):
        raise ApiError(400, "bad_request", f"not a Bluetooth address: {raw!r}")
    return mac


def int_in(value, low: int, high: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ApiError(400, "bad_request", f"{what}: {low}-{high}")
    return value
