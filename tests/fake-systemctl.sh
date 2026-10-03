#!/bin/sh
# fake systemctl for offline power and update testing: logs, and fails on demand.
# `show -p ActiveState` reports "activating", like the oneshot update unit mid-run.
echo "FAKE-SYSTEMCTL: $*" >> "${FAKE_SYSTEMCTL_LOG:?}"
if [ "${FAKE_SYSTEMCTL_FAIL:-0}" = "1" ]; then echo "simulated systemctl failure" >&2; exit 1; fi
case " $* " in *" show "*ActiveState*) echo activating ;; esac
exit 0
