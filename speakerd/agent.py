"""BlueZ pairing agent: PIN 0000 for the amp, phones only in the pairing window.

Registered as the default agent with capability KeyboardDisplay, the same as
the bluetoothctl agent the Kohler was proven against. The Kohler uses legacy
PIN pairing with the fixed PIN 0000; a NoInputNoOutput agent makes BlueZ fail
it with AuthenticationFailed. Phones use Secure Simple Pairing and arrive at
RequestConfirmation, which is accepted only while the window is open.

All decisions come from a `policy` object providing:
    pin_for(mac) -> str | None          # None rejects
    accept_pairing(mac) -> bool         # SSP confirmation / just-works
    accept_service(mac, uuid) -> bool   # an untrusted device's profile connect
"""
from __future__ import annotations

import logging
import re

from dbus_next import DBusError
from dbus_next.service import ServiceInterface, method

log = logging.getLogger("speakerd.agent")

AGENT_PATH = "/org/speakerd/agent"
CAPABILITY = "KeyboardDisplay"
_REJECTED = "org.bluez.Error.Rejected"

_DEV_RE = re.compile(r"/dev_((?:[0-9A-Fa-f]{2}_){5}[0-9A-Fa-f]{2})$")


def _mac(device_path: str) -> str:
    m = _DEV_RE.search(device_path)
    return m.group(1).replace("_", ":").upper() if m else device_path


class PairingAgent(ServiceInterface):
    def __init__(self, policy):
        super().__init__("org.bluez.Agent1")
        self._policy = policy

    def _reject(self, what: str, device: str):
        log.info("agent: rejected %s from %s", what, _mac(device))
        raise DBusError(_REJECTED, f"{what} not allowed now")

    @method()
    def Release(self):
        log.info("agent: released by BlueZ")

    @method()
    def RequestPinCode(self, device: "o") -> "s":
        pin = self._policy.pin_for(_mac(device))
        if pin is None:
            self._reject("PIN pairing", device)
        log.info("agent: PIN requested by %s, answered", _mac(device))
        return pin

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):
        log.info("agent: PIN display for %s", _mac(device))

    @method()
    def RequestPasskey(self, device: "o") -> "u":
        # nothing here can type a passkey the other side shows
        self._reject("passkey entry", device)

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):
        pass

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):
        if not self._policy.accept_pairing(_mac(device)):
            self._reject("pairing", device)
        log.info("agent: pairing confirmed for %s", _mac(device))

    @method()
    def RequestAuthorization(self, device: "o"):
        if not self._policy.accept_pairing(_mac(device)):
            self._reject("pairing", device)
        log.info("agent: pairing authorized for %s", _mac(device))

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):
        if not self._policy.accept_service(_mac(device), uuid):
            self._reject(f"service {uuid}", device)

    @method()
    def Cancel(self):
        log.info("agent: request cancelled by BlueZ")
