# Install state and the bootstrap

How Home Assistant installs NowAirPlaying over SSH, and how it follows, rejoins or
re-runs an install. The contract was agreed with the `kohler_anthem_plus` integration
in 2026-09.

## Releases

- **Asset:**
  `https://github.com/frozenmartini/NowAirPlaying/releases/download/v<ver>/nowairplaying-<ver>-trixie-arm64.tar.gz`
  - Its hash is at the same URL plus `.sha256`, in `sha256sum` format.
  - The tag has the `v`; the file name doesn't.
- **Contents:** the tagged tree plus the built trixie packages, under one top directory
  `nowairplaying-<ver>/`, which carries a `VERSION` file. `build/release.sh` makes it.
- **Pinned:** each `kohler_anthem_plus` version pins one release version and its
  SHA-256. Nothing follows "latest".

## The bootstrap

`install/bootstrap.sh` is the only code Home Assistant runs as root on the Pi.

- **Where HA gets it:** each `kohler_anthem_plus` version carries the copy from the
  release it pins. A fresh Pi has none of our code on it, so the bootstrap can't come
  from the release itself.
- **How HA runs it:** HA uploads it over SFTP, for example to
  `~/.cache/nowairplaying/bootstrap-<ver>.sh`, and runs it with the `sudo` password on
  stdin:

```sh
LC_ALL=C sudo -S -k -p '' systemd-run --unit=nowairplaying-install --collect --quiet \
    /bin/sh ~/.cache/nowairplaying/bootstrap-<ver>.sh \
    --version <ver> --url <asset url> --sha256 <64 hex> \
    --user <ssh user> --name <room> --phones onboard|dongle
```

**Options:**

| Option | Value |
|---|---|
| `--version` | `N.N.N`, the pinned release |
| `--url` | the asset URL: `https://….tar.gz`, or `file:///….tar.gz` for tests |
| `--sha256` | 64 lowercase hex characters |
| `--user` | the SSH login user (the Imager user). Required: under `systemd-run` there is no `SUDO_USER`. From `0.0.2` the audio runs as the system account `nowairplaying`, not as this user. The install moves this user's planted `claim-token` into place and adds them to group `nowairplaying` ([SETUP-API](SETUP-API.md#ownership-unclaimed-and-claimed)) |
| `--name` | the room name, e.g. `Bathroom`. It must not start with `-`, or contain quotes, backslashes, `/` or `&` |
| `--phones` | `onboard` (version A, the default) or `dongle` (version B). Phone Bluetooth isn't built yet, so it changes nothing so far |

**What it does:**
1. Checks the options. A bad option is recorded as `failed` / `bad_arguments`.
2. Writes `install.json` as `installing`, phase 0 `download`.
3. Downloads into a fresh `mktemp -d` and checks the SHA-256.
4. Unpacks into `/opt/nowairplaying/<ver>`, replacing an earlier unpack of the same
   version, and checks the release's `VERSION`.
5. Runs `install.sh --user … --name … --phones …`, which keeps `install.json` up to
   date from there on.

The fixed unit name means two installs can never run at once: systemd refuses a second
unit with that name. `systemctl is-active nowairplaying-install` needs no `sudo`.

## install.json

`/var/lib/nowairplaying/install.json` is written by root and is mode 0644. It holds no
secrets. It is replaced atomically, so a reader never sees half a file. The first install
and every later update ([`POST /node/update`](SETUP-API.md#post-nodeupdate)) write the
same file.

```json
{"state": "failed", "version": "0.0.1",
 "phase": 0, "phase_name": "download",
 "started": "2026-10-01T14:03:11-07:00", "finished": "2026-10-01T14:03:40-07:00", "exit": 22,
 "reason": "download_failed", "message": "curl: (22) The requested URL returned error: 404",
 "log": "/var/log/nowairplaying-install.log", "log_offset": 48213,
 "cert_sha256": null, "rolled_back": false}
```

| Field | Meaning |
|---|---|
| `state` | `installing`, `failed` or `done` |
| `version` | the release being installed; `null` if `--version` itself was bad |
| `phase`, `phase_name` | `0 download`, `1 preflight`, `2 apt`, `3 packages`, `4 units`, `5 configure`, `6 verify`: the last one reached |
| `started`, `finished` | ISO 8601. `finished` is `null` while installing |
| `exit` | the exit status; `null` while installing |
| `reason` | `null` unless failed: `bad_arguments`, `download_failed`, `hash_mismatch`, `unpack_failed`, `preflight_conflict`, `install_failed`, `verify_failed` or `downgrade` |
| `message` | `null` unless failed: the error line, e.g. the installer's last `ERROR:` line or `2 check(s) FAILED.` |
| `log`, `log_offset` | the log, 0644 and appended across runs, and the byte where this run starts |
| `cert_sha256` | from `0.0.2`, once `done`: the SHA-256 of the API certificate in DER form, lowercase hex. HA pins it ([SETUP-API](SETUP-API.md#trust-the-pinned-certificate)). `null` before |
| `rolled_back` | `true` when an update failed verify and the previous release was reinstalled |

- **`preflight_conflict`** means the Pi already runs something NowAirPlaying would
  break: a desktop, another AirPlay receiver, or another program on our ports (8443,
  8080, UDP 319/320). Nothing was changed. `message` names the conflict. Ordinary
  scripts and agents are not conflicts.
- **`done`** means the installer finished with exit 0. Steps may still be left, such as
  pairing the amp.
- **`install.sh --verify`** only checks, and never touches `install.json`.
- **A manual install** from a checkout records its runs the same way. `version` then
  comes from the checkout's `VERSION`.

## What a new setup flow does

After the SSH login, HA looks at the unit first and then at the file:

| `install.json` | unit | HA does |
|---|---|---|
| missing | – | a fresh install |
| `installing` | active | rejoin: follow the log from `log_offset`; no password needed |
| `installing` | not active | the Pi rebooted mid-run, or the run was killed: treat it as `failed` |
| `failed` | – | show `message` and the log's last lines, then offer a re-run |
| `done`, same version | – | skip to the claim |
| `done`, older version | – | an upgrade: re-run |

A re-run asks for the password again, since it's never stored. Every phase checks
before it acts, so a re-run resumes where the last one stopped.

**After the install**, the node's zeroconf TXT `state` (`unclaimed` or `claimed`) takes
over from this file ([SETUP-API](SETUP-API.md#discovery)).
