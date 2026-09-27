#!/bin/bash
# fake sudo for offline _system_command testing
echo "FAKE-SUDO: $@" >> "${FAKE_SUDO_LOG:?}"
if [ "${FAKE_SUDO_FAIL:-0}" = "1" ]; then echo "simulated systemctl failure" >&2; exit 1; fi
exit 0
