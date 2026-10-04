# Node API and setup page (v2)

**Status: implemented for `0.0.2`, tested offline, not yet run on a Pi.** It replaces
the v1 draft, which was never built.

**Agreed with Home Assistant (`kohler-anthem-plus#003`):**
- **Install over SSH, then this API** (2026-09-30). The install is in
  [INSTALL-STATE](INSTALL-STATE.md).
- **No MQTT between Home Assistant and the node** (2026-10-02). Home Assistant talks to
  the node directly through this API, builds the entities itself, and owns the one
  device card. Since Core 2026.8 a device belongs to exactly one config entry, so an MQTT
  card could no longer merge with Home Assistant's own.
- **HTTPS with a pinned certificate.** Home Assistant reads the fingerprint over SSH
  during the install, and again whenever the certificate changes (Reconnect).
- **State is pushed with SSE.** Commands are plain requests.
- **Two accounts** (2026-10-02). The API runs as `nowairplaying-api`, the only account
  with polkit grants. The audio stack runs as `nowairplaying` with none. See
  [Privileges](#privileges).
- **polkit for the update unit and logind only.** Nothing the API can trigger runs as
  root except that.
- **Wi-Fi is not an API call.** Home Assistant adds a network over SSH with `sudo`, or
  the owner puts a file on the SD card. See [Wi-Fi](#wi-fi).

Two clients use the API:

- **the Home Assistant integration** (`kohler_anthem_plus`): it claims the node, pairs it
  with the amplifier, shows its state, and runs updates and reboots;
- **the node's own setup page**, opened in a phone or computer browser, for people
  without Home Assistant.

MQTT stays in speakerd as an option for people without Home Assistant. It's set in
`config.toml` by hand; the API doesn't configure it. See [MQTT](#mqtt).

## Principles

- **Local only.** The API serves the home network: no cloud, no port forwarding, no
  remote access.
- **A token only travels over HTTPS.** The setup page is plain HTTP, so browsers show no
  certificate warning. It can do only what needs no token.
- **Our own accounts.** Nothing runs as the login user, so the owner's own scripts or a
  code agent on the same Pi can't break it by accident.
- **Least privilege.** The ports are above 1024, so no capability is needed. Root is
  reached only through the polkit rules under [Privileges](#privileges), which only the
  API service's account holds.
- **One API, two clients.** The setup page calls the same endpoints Home Assistant
  calls.

## The service

Two processes, two accounts:

| | The API service | speakerd |
|---|---|---|
| Account | `nowairplaying-api` (system account, home `/var/lib/nowairplaying-api`) | `nowairplaying` (system account, home `/var/lib/nowairplaying/home`, mode 0700) |
| Unit | `nowairplaying-api.service`, a system unit, sandboxed (`ProtectSystem=strict`, `NoNewPrivileges`) | `speakerd.service`, a user unit of `nowairplaying` with linger, so it starts at boot with nobody logged in |
| Does | HTTPS and the setup page, the claim and its token, zeroconf, updates, reboot and power-off, the system-wide checks | Bluetooth (the amp, phones, the pairing agent), AirPlay metadata, renaming, the session checks, MQTT |
| polkit grants | the update unit, reboot, power-off | none |
| State | `/var/lib/nowairplaying-api/api.json`, mode 0600: the claim (the token's SHA-256, `claimed_by`, `area`, the time) | `~nowairplaying/.local/state/speakerd/state.json`: the amp, the name, auto-reconnect |

- **`/var/lib/nowairplaying` itself is root's** (`root:root`, mode 0755, so the SSH user
  can read `install.json`). It holds what root reads back, `install.json` and
  `install-args`, and the API account's `claim/`, `update/` and `tls/`. Neither account
  can rename anything in it.
- **HTTPS API:** port **8443**, all interfaces. Home Assistant reads the port from the
  zeroconf record and never hard-codes it.
- **HTTP setup page:** port **8080**, all interfaces: `http://<hostname>.local:8080/`.
- **Between them:** speakerd listens on `/run/nowairplaying/speakerd.sock`. Its directory
  is setgid group `nowairplaying-api`, mode 2750, so only the API service can connect.
  Newline-delimited JSON; speakerd pushes its audio state on every change.
- **speakerd** starts with or without an amp, and applies a pairing or a rename live,
  with no restart. If it's down, the API still answers: `/info`, `/verify` (with
  `speakerd_running` failed) and `/state` work, and audio commands return
  `502 failed`.
- **Certificate:** `/var/lib/nowairplaying/tls/cert.pem` and `key.pem`, owned by
  `nowairplaying-api`. Self-signed EC P-256, valid for 100 years, made once by the
  install.

**The certificate is kept** across re-runs and updates. A new one is made only if the
files are missing. Home Assistant pins it by fingerprint, not by name or expiry, so the
long lifetime costs nothing.

## Discovery

The node advertises itself with mDNS/zeroconf:

- **Service type:** `_nowairplaying._tcp`, on the HTTPS API port.
- **Instance name:** the node's display name, e.g. `Bathroom Speaker`.
- **TXT records** (the keys are unchanged since 2026-09-30; only `api` moved to 2):

| Key | Example | Meaning |
|---|---|---|
| `api` | `2` | API major version |
| `ver` | `0.0.2` | NowAirPlaying release |
| `mac` | `B8:27:EB:12:34:56` | the onboard Bluetooth adapter (`hci0`), uppercase with colons. Home Assistant uses it as the config entry's `unique_id` |
| `id` | `nowairplaying_123456` | node id: stable, derived from the MAC, never renamed |
| `state` | `unclaimed` | `unclaimed` or `claimed`. The install itself is tracked in `install.json` |

Home Assistant's manifest matches `_nowairplaying._tcp.local.` and offers "New audio
node found" for `state=unclaimed`. The record carries no fingerprint, since anyone on
the network can publish a TXT record.

## Trust: the pinned certificate

- **Installed over SSH (the normal path).** `install.json` carries `cert_sha256`: the
  SHA-256 of the certificate in DER form, lowercase hex. It's written on every
  `install.json` write once the certificate exists, so it's always the certificate the
  API serves. HA reads it over the same SSH session and pins it before its first API
  call. Nothing is trusted on first contact.
- **Installed by hand,** then found by Home Assistant through zeroconf: HA still logs in
  over SSH, the same as for any other node, and reads `cert_sha256` from `install.json`
  before its first HTTPS call. The zeroconf record only tells HA that the node exists.
  With no SSH access there is no pin, and HA doesn't claim the node. There is no trust on
  first use (confirmed by HA on kap#003, 2026-10-03).
- **A changed certificate** (the files were deleted and a re-run made new ones): HA stops
  calling the node and raises a repair with a **Reconnect** button (agreed 2026-10-02).
  - Reconnect logs in over SSH with HA's key, against the pinned SSH host key, reads
    `cert_sha256` from `install.json` again and pins the new value. No password and no
    re-claim; the token and the amp pairing are untouched.
  - If the SSH host key changed too (a rebuilt card), Reconnect refuses. The repair then
    says to start fresh: the [reset file](#releasing-a-claim), then add the node again.

## Ownership: unclaimed and claimed

| State | Who may change the node | Setup page |
|---|---|---|
| **unclaimed** (fresh, or standalone) | anyone on the home network, with no token | full controls: pair the amp, connect, disconnect, forget, phones, verify |
| **claimed** (by Home Assistant) | only the holder of the token, over HTTPS | read-only status plus "Managed by Home Assistant at `<host>`". Amp connect and disconnect stay available (see below) |

- **A standalone node stays unclaimed.** The home network is trusted for a device you
  just set up yourself, the same way a new speaker or streaming stick is.
- **Claiming** is the one step that locks the node to one Home Assistant.
  - **A planted claim token, when present.** Before the install, Home Assistant writes
    `~/.config/nowairplaying/claim-token` over SSH, as the login user with no `sudo`.
    - The file holds the lowercase hex SHA-256 of 32 random bytes. It is mode 0600.
    - **The install moves it** to `/var/lib/nowairplaying/claim/claim-token`, owned by
      `nowairplaying-api`, mode 0600. From `0.0.4` a planted token replaces one already
      there: a fresh plant always comes from the newest setup.
    - That directory is mode 2770, group `nowairplaying-api`. The install adds the
      login user (`--user`) to that group. From their next SSH login, they can plant a
      fresh token there directly, still without `sudo`. This is supported and stays.
    - **Plant it mode 0640, with group `nowairplaying-api`,** which must be able to
      read it. The directory gives its group only to a file **created** in it. `mv`
      keeps a file's own group, so a file written elsewhere (the home folder, `/tmp`)
      and moved in keeps the login user's group, and the API can't read it. Write a
      temporary file inside `claim/` and `mv -f` it over `claim-token`, or
      `chgrp nowairplaying-api` it before the move.
    - **A planted file the API can't use locks the claim, it never opens it.** One it
      can't read, or that isn't 64 hex characters, makes `/claim` return
      `409 claim_token_invalid`, and `/info` still reports `"claim": "token"`.
    - `/claim` then needs `Authorization: Bearer <hex>`, where `<hex>` is the 32 random
      bytes themselves in hex, **never the file's contents**. The node hashes the raw
      bytes, not the hex text. Both strings are 64 hex characters, so they are easy to
      swap:

      ```python
      raw = secrets.token_bytes(32)
      plant = hashlib.sha256(raw).hexdigest()  # → the claim-token file
      bearer = raw.hex()                       # → Authorization: Bearer <bearer>
      ```
    - The file is read on **every** `/claim` request, never once at startup. Whitespace
      around its contents is ignored.
    - A successful claim deletes it, before anything is claimed.
    - `GET /info` reports `"claim": "token"` or `"open"`.
  - **Otherwise the first claimer wins.** The node doesn't check who is asking. Home
    Assistant only claims a node whose certificate it has already pinned over SSH (see
    [Trust](#trust-the-pinned-certificate)).
- **Re-claiming** (from `0.0.4`): on a claimed node, `/claim` with a planted token's
  bearer replaces the claim and returns a new token. The old token stops working, and
  event streams opened with it end.
  - Planting needs an SSH login in group `nowairplaying-api`, the same proof of
    ownership Reconnect relies on.
  - It frees a node whose claim reply was lost (no one holds its token), and lets a
    rebuilt Home Assistant take its node back.
  - Without a planted token, a claimed node still answers `409 already_claimed`.
- **The token** is 32 random bytes, returned once by `/claim`. The node stores only its
  SHA-256. It is sent as `Authorization: Bearer <token>`.

**Amp connect and disconnect on a claimed node:** the page keeps these two buttons, so
someone standing next to the amp can free it (for example to pair a phone with the
Kohler directly) without opening Home Assistant. They don't change the configuration.

### Releasing a claim

- **From Home Assistant:** deleting the node's config entry calls `POST /release`, best
  effort. HA also removes everything it made for the node. If the node is unreachable,
  the entry is removed anyway and the reset file is the fallback.
- **Without Home Assistant,** for example if it's gone for good:
  1. Power the Pi off.
  2. Put an empty file named `nowairplaying-reset` on the SD card's boot partition. It
     shows up as a small drive on any computer.
  3. Power the Pi back on.

  At boot, a root unit turns the file into `/var/lib/nowairplaying-api/reset-request`
  and deletes it; the API service clears its claim and deletes the request. Wi-Fi, the
  certificate and all Bluetooth pairings are kept. Physical access to the card is the
  proof of ownership.

## Endpoints

All paths are under `/api/v2`. Bodies are JSON. The **Auth** column:

- **none:** anyone, over HTTP or HTTPS.
- **owner:** on an unclaimed node, anyone. On a claimed node, the token, over HTTPS only.
- **token:** claimed nodes only, with the token, over HTTPS only. On an unclaimed node
  these return `403 claim_required`: updates and power are Home Assistant features.
- **open:** anyone, even on a claimed node: amp connect and disconnect, and the audio
  restart. They change nothing in the configuration.

A request that carries an `Authorization` header over HTTP is refused with
`403 https_required`, and the node logs it. The token never needs to cross the
network in the clear.

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /info` | none | identity and claim state |
| `GET /verify` | none | health checks |
| `GET /state` | owner | the full state, once |
| `GET /events` | owner | the full state, then every change (SSE) |
| `POST /claim` | none, HTTPS only. Unclaimed, or with a planted token | lock the node to one Home Assistant |
| `POST /release` | token | undo the claim |
| `POST /amp/scan` | owner | start a Bluetooth scan for the amp |
| `GET /amp/found` | owner | devices seen by the scan |
| `POST /amp/pair` | owner | pair, trust and connect the amp |
| `POST /amp/connect` | open | connect the paired amp |
| `POST /amp/disconnect` | open | release the amp; it stays paired |
| `POST /amp/reconnect` | owner | disconnect, wait, connect: restores the amp's track display ("fix metadata") |
| `PUT /amp/auto-reconnect` | owner | `{"on": true}` or `{"on": false}` |
| `POST /amp/forget` | owner | unpair the amp |
| `POST /audio/restart` | open | from `0.0.4`: restart the audio stack and bring the amp's audio back |
| `POST /phones/pairing` | owner | open or close the phone pairing window |
| `POST /phones/{mac}/connect` | owner | connect a paired phone |
| `POST /phones/{mac}/disconnect` | owner | disconnect a phone |
| `DELETE /phones/{mac}` | owner | forget a phone |
| `POST /media/{source}/{action}` | owner | `source` is `airplay` or `bluetooth`; `action` is `playpause`, `next` or `previous` |
| `PUT /node/name` | token | rename the node |
| `POST /node/reboot` | token | reboot the Pi |
| `POST /node/shutdown` | token | power the Pi off |
| `POST /node/update` | token | install another release |

Commands return once the node has acted, with `{"ok": true}` or an
[error](#errors). The resulting state arrives as an SSE event; a client never needs to
poll.

### `GET /info`

```json
{
  "api": 2,
  "version": "0.0.2",
  "id": "nowairplaying_123456",
  "name": "Bathroom Speaker",
  "mac": "B8:27:EB:12:34:56",
  "state": "claimed",
  "claim": "open",
  "claimed_by": "homeassistant.local",
  "area": "Bathroom",
  "phones": "onboard",
  "amp": {"mac": "F4:4E:FD:00:00:00", "name": "Kohler Amplifier",
          "paired": true, "connected": true, "audio": true}
}
```

`amp` is `null` before pairing. `amp.audio` is there from `0.0.4`, as in the state. `claim` is `token` while a planted token waits and `open`
otherwise. **No response ever contains a password, a key or the token** (except
`/claim`'s one-time reply).

### `POST /claim`

```json
{"name": "Bathroom Speaker", "area": "Bathroom", "claimed_by": "homeassistant.local"}
```

- `200` → `{"token": "…"}`.
- `409 already_claimed` if the node is claimed and no token is planted.
  `409 claim_token_invalid` if a planted token file is there but unusable.
  `401 unauthorized` if a token is planted and the bearer doesn't match it.
- On a claimed node with a planted token, it [re-claims](#ownership-unclaimed-and-claimed).
- `area` and `claimed_by` are optional: each at most 255 characters, with no control
  characters and no Unicode line or paragraph separators (`400 bad_request`
  otherwise).
- `name` is optional and follows the same rule as [`PUT /node/name`](#put-nodename):
  1–40 characters, no quotes, backslashes, `/`, `&` or control characters. A bad name
  is `400 bad_request` and nothing is claimed. The token checks come first, so a wrong
  bearer is still `401`.
- `502 failed` if the used planted token can't be deleted. Nothing is claimed then: a
  plant left behind would let its holder re-claim the node at any time.
- HTTPS only. Over HTTP it returns `403 https_required`.
- The node id stays the MAC-derived `id`. `name` sets the display name (and the AirPlay
  name); `area` is only stored and returned for HA. So a later rename never re-keys
  anything.
- **Effect:** speakerd stops publishing MQTT discovery and clears what it had published,
  if MQTT is configured (see [MQTT](#mqtt)). The TXT `state` becomes `claimed`.

### `POST /release`

- **Effect:** the claim is cleared and the TXT `state` returns to `unclaimed`. MQTT
  discovery resumes if MQTT is configured. All pairings are kept.
- `200` → `{"ok": true}`.

### `GET /state` and `GET /events`

`GET /state` returns the state object once. `GET /events` is a Server-Sent Events
stream (`text/event-stream`):

- **The first event** is `event: state` with the full object.
- **After that,** `event: change` carries an object holding only the top-level keys that
  changed. Each one **replaces** that key's value whole; there's no deeper merging.
- **Every event has an `id:`,** the node's change counter. A gap means a missed event;
  the client reconnects.
- **A comment line** (`: ping`) goes out every 15 seconds, so a dead link shows within
  about 30 seconds.
- **On reconnect,** the client always gets a fresh full `state`. `Last-Event-ID` is
  ignored.
- **The stream ends** when the claim is released or speakerd stops. The client
  reconnects with backoff.

**The state object:**

```json
{
  "node": {"name": "Bathroom Speaker", "version": "0.0.4",
           "update": {"state": "idle", "version": null, "phase_name": null,
                      "reason": null, "message": null, "rolled_back": false},
           "audio_restart": {"running": false, "last_result": null}},
  "network": {"link": "wifi", "ssid": "Home", "signal": 71, "ip": "192.168.1.42"},
  "amp": {"mac": "F4:4E:FD:00:00:00", "name": "Kohler Amplifier",
          "paired": true, "connected": true, "audio": true, "auto_reconnect": true,
          "last_result": {"ok": true, "error": null, "at": "2026-10-02T15:40:12-07:00"}},
  "phones": {
    "pairing": {"open": false, "until": null, "last_paired": null},
    "devices": [{"mac": "AA:BB:CC:DD:EE:01", "name": "Pat's iPhone", "connected": true,
                 "last_result": {"ok": true, "error": null, "at": "…"}}]
  },
  "source": "airplay",
  "bluetooth_streaming": false,
  "now_playing": {
    "airplay": {"status": "playing", "title": "…", "artist": "…", "album": "…",
                "client": "Pat's iPhone"},
    "bluetooth": {"status": "idle", "title": null, "artist": null, "album": null,
                  "duration": null, "position": null, "device": null}
  }
}
```

| Key | Values |
|---|---|
| `source` | `airplay`, `bluetooth`, `both` or `idle` |
| `now_playing.*.status` | `playing`, `paused` or `idle`. A source that goes idle clears its track fields |
| `now_playing.bluetooth` | `duration` and `position` in milliseconds when the phone reports them; `device` is the phone's name (its roster name, else its Bluetooth name), for display. Match on a phone by `phones.devices[].mac` |
| `amp` | `null` before pairing |
| `amp.audio` | from `0.0.3`: the Pi's audio link to the amp (A2DP) is up. `connected` alone doesn't mean AirPlay can play: the control link can stay up with the audio link gone. speakerd restores it on its own (see auto-reconnect) |
| `amp.last_result`, `devices[].last_result` | the most recent connect, disconnect or audio-link restore attempt: `ok`, BlueZ's error name if it failed, and when |
| `phones.pairing` | `until` is when the window closes; `last_paired` is the MAC of the last phone paired in it |
| `network.link` | `wifi` or `ethernet`. `ssid` and `signal` (0–100) are `null` on Ethernet |
| `node.update` | progress of an update; see below |
| `node.audio_restart` | from `0.0.4`: `running`, and `last_result` (`ok`, `error`, `at`) of the latest [audio restart](#post-audiorestart), `null` before the first |

**No volume.** speakerd has no volume control. Volume follows the sender: the phone or
Mac sets it, and it reaches the Kohler over AVRCP. The amp has no absolute volume, so a
level can't be set from outside. A relative step may come later.

### `POST /amp/scan`, `GET /amp/found`

- `POST /amp/scan`, body `{"seconds": 20}` (at most 30). `202` → `{"ok": true}`, or
  `409 busy` if a scan or pair is already running.
- `GET /amp/found` returns:

  ```json
  {"scanning": true,
   "devices": [{"mac": "F4:4E:FD:00:00:00", "name": "Kohler Amplifier",
                "rssi": -58, "likely_amp": true}]}
  ```

  `likely_amp` marks a device that advertises as an audio sink. It's only a hint for
  sorting the list; the user still picks.

### `POST /amp/pair`

- Body: `{"mac": "F4:4E:FD:00:00:00"}`. The user must first put the amp in pairing mode.
- **Effect:** pair, trust and connect, then store the amp in speakerd's config. No
  restart.
- **The Kohler needs a PIN** (confirmed 2026-09-28). It uses legacy PIN pairing with the
  fixed PIN `0000`. speakerd's agent answers BlueZ's `RequestPinCode` with it. A
  NoInputNoOutput agent fails with `org.bluez.Error.AuthenticationFailed`.
- `200` → `{"ok": true}`, or `502 pair_failed` with the BlueZ error in `message`.
- **Pairing mode on the Kohler** (tested 2026-09-29): the amp has no pairing button.
  Pairing mode started on the Anthem+ screen works. Removing a device on the screen
  unpairs **every** device, not just that one. Whether the amp can be paired without
  the screen, for example after a power cycle, is not yet known.

### The other amp commands

- **connect / disconnect:** disconnect is deliberate. Auto-reconnect stays off until the
  next connect, the same rule as Home Assistant's switch. `502 failed` if BlueZ refuses.
- **reconnect:** disconnect, wait, connect. Any phone stays connected throughout.
- **auto-reconnect:** whether speakerd reconnects the amp when it drops. It's stored, so
  it survives a reboot. From `0.0.3` it also covers the audio link: if the amp stays
  connected without it for 10 seconds after its latest connect, speakerd reconnects the
  audio link alone, then, if that isn't enough, does a full reconnect (three attempts in
  all). If those fail, it keeps asking for the audio link alone every 60 seconds, for as
  long as the amp stays connected without it. That never disconnects the amp. A
  deliberate disconnect, or auto-reconnect off, leaves both alone.
- **forget:** removes the pairing and clears the amp from the config. speakerd keeps
  running with no amp.

### `POST /audio/restart`

The repair for broken audio (from `0.0.4`). For Home Assistant, a "Restart audio"
button; the setup page has one too.

- `202` → `{"ok": true}`. It runs on in the background, and `node.audio_restart`
  follows it.
- `409 busy` while one is running, and for 60 seconds after one ends: it's open to
  the whole network, so nothing can keep the audio down by restarting it in a loop.
  `message` says how many seconds are left.
- **What it does:**
  1. Restarts PipeWire, WirePlumber and shairport-sync, in that order. AirPlay drops
     for a few seconds, and a phone's Bluetooth audio pauses.
  2. Brings the amp's audio link back, which the restart always takes down: the audio
     link alone first, then a full reconnect, three attempts in all. If the amp
     dropped, it connects it. This ignores the auto-reconnect switch, since someone
     asked for working audio; a deliberate disconnect still wins.
- `last_result.ok` is `false` with `error` when the restart failed or the amp's audio
  link didn't come back. Sound is normally back within 10 to 30 seconds.
- The services belong to the audio account, which restarts them itself. No privilege is
  involved, and the API account never touches them.

### Phones

- **`POST /phones/pairing`,** body `{"seconds": 120}` to open (at most 300) or
  `{"open": false}` to close.
  - While it's open, the node is discoverable as "Bluetooth <name>", and speakerd's agent
    accepts a phone that pairs.
  - Each new phone shows in `phones.pairing.last_paired` and in `phones.devices`.
  - The window closes by itself at `until`.
- **connect, disconnect, forget:** for a phone in `phones.devices`. `404 not_found` for
  an unknown MAC.
- **Phones stay connected** together with the amp. Their audio is mixed, and the amp's
  screen shows the most recent sender.
- `GET /info` reports `phones`: `onboard` (version A, the Pi's own radio) or `dongle`
  (version B, a USB dongle for range).

### `PUT /node/name`

- Body `{"name": "Bathroom Speaker"}`, 1–40 characters, with no quotes, backslashes,
  `/`, `&` or control characters (a newline would break shairport-sync's config).
- Renames the AirPlay receiver, the Bluetooth name and the zeroconf instance. The `id`
  and `mac` never change.
- shairport-sync restarts, so AirPlay drops for a few seconds.

### `POST /node/reboot`, `POST /node/shutdown`

- `202` → `{"ok": true}`, then the node goes down about two seconds later, so the reply
  gets out first.
- They run through logind with the polkit rule below. They aren't stored or queued, so
  a stale request can never repeat after a reboot.

### `POST /node/update`

- Body: `{"version": "0.0.3", "sha256": "<64 hex>"}`, from the pin in Home Assistant's
  integration.
- `202` → `{"ok": true}`. Progress shows in `node.update`, and a client follows it like
  any other change.
- `409 busy` if an install or update is already running.
- `409 downgrade` if `version` is lower than the installed one. The same version is
  allowed and reinstalls it, as a repair. The update unit checks this again as root,
  and records `failed` / `downgrade` in `install.json` if a request ever gets past
  the API.

**How it works:**
1. speakerd writes the request to `/var/lib/nowairplaying/update/request.json`.
2. The API service starts the fixed system unit `nowairplaying-update.service`. A
   polkit rule lets `nowairplaying-api` start that one unit and nothing else.
3. The unit runs as root.
   - It reads the request and checks `version` against `N.N.N` and `sha256` against 64
     lowercase hex characters.
   - **It builds the URL itself:** our GitHub release asset for that version. The URL
     isn't part of the request, so a stolen token can install only a release we
     published.
   - It runs the same bootstrap as the first install, with the `--user`, `--name` and
     `--phones` recorded then.
4. `install.json` tracks it like any install. `node.update` mirrors it:
   `state` (`idle`, `running`, `done` or `failed`), `version`, `phase_name`, `reason`
   and `message`.
5. **Rollback.** The previous release stays unpacked in `/opt/nowairplaying/<old ver>`.
   If the new one fails its verify step, the unit reinstalls the old one. It then
   reports `failed` with `rolled_back: true`.

speakerd restarts during an update, so the SSE stream drops. The client reconnects and
reads the outcome from `node.update`.

**Home Assistant's Update entity** compares `node.version` with the integration's pin.
Installing calls this endpoint.

### `GET /verify`

```json
{"ok": false,
 "checks": [
   {"id": "bluez_version", "ok": true, "detail": "bluetoothd 5.82"},
   {"id": "pipewire_version", "ok": true, "detail": "1.4.2"},
   {"id": "amp_connected", "ok": false, "detail": "not connected"}
 ]}
```

Check ids are stable. All checks test the **running** system, not the installed
packages:

| id | Passes when |
|---|---|
| `bluez_version` | the running `bluetoothd` is at least 5.82 |
| `pipewire_version` | PipeWire 1.4 or later runs in `nowairplaying`'s session |
| `wireplumber_version` | WirePlumber 0.5.8 or later runs in `nowairplaying`'s session |
| `packages_held` | the NowAirPlaying packages (nqptp, shairport-sync) are apt-held |
| `shairport_airplay2` | shairport-sync runs and `-V` contains `-AirPlay2-` |
| `shairport_dbus` | `org.gnome.ShairportSync` is owned on `nowairplaying`'s session bus |
| `no_mpris` | no `org.mpris.MediaPlayer2.*` player is on the session or system bus, and `mpris-proxy` isn't running |
| `nqptp_active` | `nqptp.service` is active |
| `mdns` | `avahi-daemon` runs and `/etc/nsswitch.conf` has `mdns4_minimal` |
| `polkit_rules` | `/usr/share/polkit-1/rules.d/50-nowairplaying.rules` is present and is ours (it names the update unit) |
| `speakerd_running` | the speakerd user service is active |
| `amp_paired` | the configured amp is paired and trusted |
| `amp_connected` | the amp is connected |
| `amp_audio` | from `0.0.3`: the amp's audio link (A2DP) is up. Fails with "connected, but no audio link" when only the control link is up |
| `amp_player` | exactly one player, speakerd's `/org/speakerd/player`, is registered on the amp's adapter |
| `mqtt_connected` | only when MQTT is configured: speakerd is connected to the broker |

**Which checks raise a repair** (agreed 2026-09-30): only a check that passed before and
then fails several runs in a row. That covers AirPlay 2, PipeWire, nqptp, mDNS,
speakerd and the polkit rules. "Amp not paired" raises one too. `amp_connected`,
`amp_audio`, the player and MQTT only show on the card, since a sleeping amp is normal.

### Errors

Every error is JSON: `{"error": "<code>", "message": "<human text>"}`.

| HTTP | `error` | When |
|---|---|---|
| 400 | `bad_request` | a field is missing or invalid |
| 401 | `unauthorized` | the token is missing or wrong on a claimed node |
| 403 | `https_required` | a token was sent over HTTP, or `/claim` was called over HTTP |
| 403 | `claim_required` | a **token** endpoint on an unclaimed node |
| 404 | `not_found` | no such endpoint, no paired amp, or an unknown phone |
| 409 | `already_claimed` | `/claim` on a claimed node, with no token planted |
| 409 | `claim_token_invalid` | `/claim` while a planted token file is unreadable or not 64 hex characters |
| 409 | `busy` | a scan, pair, update or audio restart is already running, or an audio restart ended less than 60 s ago |
| 409 | `downgrade` | `/node/update` to an older version |
| 502 | `pair_failed`, `failed` | BlueZ or systemd refused, or speakerd isn't running. `message` carries the error |

## Privileges

The install runs as root once. It adds one rules file,
`/usr/share/polkit-1/rules.d/50-nowairplaying.rules` (readable by everyone, so the
`polkit_rules` check can read it). After that, **nothing of ours runs as root**: root
system services act for **`nowairplaying-api` only**, on exactly these actions:

| Action | For |
|---|---|
| `org.freedesktop.systemd1.manage-units`, only with `unit` = `nowairplaying-update.service` and `verb` = `start` | `/node/update` |
| `org.freedesktop.login1.reboot`, `org.freedesktop.login1.power-off`, and their `-multiple-sessions` variants | `/node/reboot`, `/node/shutdown` |

- **Exact action ids and the exact account,** with no prefixes. `pkexec` isn't
  installed.
- **Why two accounts:** shairport-sync listens to the whole home network. A bug there
  should reach the audio, not the update unit or reboot, so the audio account
  (`nowairplaying`) holds no grants. speakerd can't trigger an update or a reboot, and
  the API service only does either for a request with the claim token.
- **The update unit trusts nothing but two fields** from the request file the API
  account writes: `version` must match `N.N.N` and `sha256` 64 lowercase hex
  characters, both checked before use and never passed through a shell. The download
  address is built from the version, in the unit.
- **Root never acts on a path either account controls.** The install and the update
  unit read and write the audio account's home only as that account (`runuser`), so a
  link planted there can't redirect a root write or read. In the API account's
  `claim/` and `tls/` root never runs `chown` or `chmod`, which would follow a link; it
  only creates missing files, and `install` replaces a link rather than writing through
  it. The update unit refuses a `request.json` that is a link, and re-checks
  the installed version in `install.json` (`N.N.N`) before it builds a rollback path
  from it.
- **The login user and anything the owner runs** get nothing new from the rules.
- **OS updates aren't done from Home Assistant.** The install turns on Debian's
  `unattended-upgrades` for security updates. Our two packages are apt-held, so those
  updates never replace them.

## Wi-Fi

No account the API runs as can change networking (agreed 2026-10-02): NetworkManager's
polkit actions can't be limited to Wi-Fi. A new network is added **alongside** the
current one, never instead of it, so nothing is switched: NetworkManager uses whichever
known network is in range. A changed router is picked up on its own.

**From Home Assistant, over SSH:** "Add Wi-Fi network" asks for the Pi's password once,
then runs:

```sh
sudo -S -k -p '' /usr/local/lib/nowairplaying/wifi-add.sh
```

with this on stdin: the sudo password as the first line, then the network, in the
format below. Nothing goes on a command line, where every local user could read it.

**From the SD card,** for a Pi that has dropped off the network: put a file named
`nowairplaying-wifi.txt` on the boot partition (the small drive any computer shows).
At boot, a root unit adds the network, then deletes the file whatever the outcome,
since it holds a password. It's the same proof of ownership as the reset file: the card
in someone's hand. Nothing on the network can trigger it.

**The format, for both:**

```
ssid=Home Network
password=the password
hidden=yes
```

- One `key=value` per line. The value is everything after the first `=`, taken as is,
  so nothing needs quoting.
- `ssid` is required (1–32 bytes). `password` is 8–63 characters; leave the line out
  for an open network. `hidden=yes` is optional, for a network that doesn't broadcast
  its name.
- Blank lines and lines starting with `#` are skipped. A Windows editor's BOM and CRLF
  line ends are fine.
- Adding the same SSID again replaces the earlier profile it made.

**Output:** one line, `ok: added 'Home Network'` or `error: …`. Exit status 0 when
added, 2 for bad input, 1 when NetworkManager refused.

**An Ethernet cable is the last fallback:** plug the Pi into the router for a minute,
then add the network from Home Assistant.

## MQTT

- **For nodes without Home Assistant.** It's set by hand in `config.toml` (`[mqtt]`), as
  in speakerd today.
- **On a claimed node,** speakerd removes its retained discovery with empty publishes,
  and stops publishing discovery. Someone who runs a broker as well doesn't get a second
  device card. State topics carry on, for anyone's own automations.
- **The API never reads or writes MQTT settings,** and Home Assistant never needs them.

## The setup page

At `http://<hostname>.local:8080/` the node serves one plain page with no framework.
It calls the endpoints above with no token:

1. **Status:** name, whether the amp is connected, and the verify checks as green or red.
2. **Pair the amplifier:** "Put the amplifier in pairing mode, then press Scan", then a
   list with likely amps first. The user picks one, presses Pair, and sees the result.
3. **Amplifier:** Connect, Disconnect, Restart audio, Reconnect and Forget buttons. An
   amp connected without its audio link shows "connected, but no audio link".
4. **Phones:** "Let a phone pair (2 minutes)", the phone list, and Connect, Disconnect
   and Forget buttons.
5. **When claimed:** "Managed by Home Assistant at `<host>`". Status, amp
   Connect/Disconnect and Restart audio stay; every other control is hidden.

**Why the page is HTTP:** a node can only offer HTTPS with a self-signed certificate,
and every phone and browser shows a full-screen warning for one. For a beginner's first
contact with the device, that's the wrong message. The page never handles a token, so
HTTP costs nothing there.

## Settled questions

- **The setup page is at `:8080`** (the owner, 2026-10-02).
- **A changed certificate is re-pinned over SSH** with Reconnect; the reset file is
  only the fallback (the owner, 2026-10-02).
