# Setup API and setup page

**Status: draft for review. Nothing here is implemented yet.**

**Superseded in part (2026-09-28):** the "Wi-Fi first" flow and every mention of the
comitup hotspot below are dropped. The setup now starts in Home Assistant (see
[ROADMAP](ROADMAP.md#3-guided-setup)). Whether Home Assistant uses this API or SSH is
still open. The pairing facts under `POST /bt/pair` were confirmed on a real amp.

A NowAirPlaying node is set up over the home network through one small HTTP API.
Two clients use the same API:

- **the node's own setup page**, opened in a phone or computer browser, for people
  without Home Assistant (standalone);
- **the Home Assistant integration** (`kohler_anthem_plus`), which finds the node,
  hands it an MQTT login, pairs it with the amplifier and watches its health.

The API can do a short list of things and nothing else.

## Principles

- **Local only.** The API serves the home network and nothing else: no cloud, no
  port forwarding, no remote access.
- **Never on the setup hotspot.** While the Pi is in its open setup hotspot, the API
  is not running at all. See [Wi-Fi first](#wi-fi-first).
- **Plain HTTP.** See [Why HTTP, not HTTPS](#why-http-not-https).
- **Least privilege.** The service runs as the node's normal user, not root. It holds
  one extra capability: binding port 80.
- **One API, two clients.** The setup page is plain HTML and JavaScript calling the
  same endpoints Home Assistant calls, so both paths are tested by the same code.

## Wi-Fi first

The API only exists once the node is on the home network.

1. **First boot:** with no Ethernet and no known Wi-Fi, comitup brings up an open
   hotspot named `NowAirPlaying-<nnnn>`, where `<nnnn>` is a persistent per-device
   number. The hotspot's address is `10.41.0.1`.
2. **Pick a network:** the user joins the hotspot from a phone, and comitup's own page
   lists nearby networks. They pick theirs and enter the password.
3. **The Pi joins that network** and drops the hotspot.

comitup's `web_service` option names a systemd service that comitup stops while the
hotspot is up and starts once the Pi is on a real network. That service is
`nowairplaying-setup.service`. It is left disabled in systemd so that comitup alone
manages it, and so the API is never reachable from the open hotspot. With Ethernet the
hotspot never appears, and the service simply runs. The Ethernet case is still to be
confirmed on a real Pi.

## The service

| | |
|---|---|
| Unit | `nowairplaying-setup.service` (a system unit with `User=` the node user and `AmbientCapabilities=CAP_NET_BIND_SERVICE`) |
| Listens on | port 80, all interfaces (only ever up while the Pi is on the home network) |
| Address | `http://nowairplaying-<nnnn>.local/`. comitup publishes the same name as the hotspot as the Pi's `.local` host name. |
| State file | `~/.config/nowairplaying/setup.json`, mode 0600 |
| Writes | `~/.config/speakerd/config.toml` (the `[mqtt]`, `[node]` and `[bluetooth]` amp settings), then restarts the speakerd user service |

## Discovery

The node advertises itself with mDNS/zeroconf:

- **Service type:** `_nowairplaying._tcp`, port 80. This replaces the earlier
  placeholder `_chorus-setup._tcp`; the public name should say what the device is.
- **Instance name:** the node's display name, e.g. `Bathroom Speaker`.
- **TXT records:**

| Key | Example | Meaning |
|---|---|---|
| `api` | `1` | API major version |
| `ver` | `1.0.0` | NowAirPlaying release |
| `mac` | `B8:27:EB:12:34:56` | the Pi's Bluetooth adapter MAC, uppercase with colons. Home Assistant uses it as the config entry's `unique_id` and as the device-card `bluetooth` connection. |
| `id` | `nowairplaying_123456` | node id: stable, derived from the MAC, never renamed |
| `state` | `unclaimed` | `unclaimed` or `claimed` |

Home Assistant's manifest matches `_nowairplaying._tcp.local.` and offers "New audio
node found" for `state=unclaimed`.

## Ownership: unclaimed and claimed

| State | Who may change the node | Setup page |
|---|---|---|
| **unclaimed** (fresh, or standalone) | anyone on the home network, with no token | full controls: pair, connect, disconnect, forget, verify |
| **claimed** (by Home Assistant) | only the holder of the token, i.e. Home Assistant | read-only status plus "Managed by Home Assistant at `<host>`"; the Bluetooth connect and disconnect buttons stay available (see below) |

- **A standalone node stays unclaimed.** The home network is trusted for a device you
  just set up yourself, the same way a new speaker or streaming stick is.
- **Claiming** is the one step that locks the node to one Home Assistant. The first
  claimer wins.
- **Releasing a claim:**
  - **From Home Assistant:** deleting the node's config entry calls `POST /release`.
  - **Without Home Assistant**, for example if it's gone for good: power the Pi off, put
    an empty file named `nowairplaying-reset` on the SD card's boot partition (it shows
    up as a small drive on any computer), and power it back on. The node clears its
    claim and MQTT login, then deletes the file. Wi-Fi and the amp pairing are kept.
    Physical access to the card is the proof of ownership.
- **The token** is 32 random bytes, returned once by `/claim`. The node stores only its
  SHA-256. It is sent as `Authorization: Bearer <token>`.

**Connect and disconnect on a claimed node:** the page keeps these two buttons, so
someone standing next to the amp can free it (for example to pair a phone with the
Kohler directly) without opening Home Assistant. They don't change the configuration.
Pair, forget and release still need the token.

## Endpoints (v1)

All paths are under `/api/v1`. Bodies are JSON. **Auth** means a valid token is
required when the node is claimed; when it is unclaimed, anyone on the network may call
it.

| Method and path | Auth | Purpose |
|---|---|---|
| `GET /info` | none | identity and state |
| `GET /verify` | none | health checks |
| `POST /claim` | none, unclaimed only | lock the node to one Home Assistant and give it an MQTT login |
| `POST /mqtt` | token | replace the MQTT login (rotation) |
| `POST /release` | token | undo the claim |
| `POST /bt/scan` | auth | start a Bluetooth scan |
| `GET /bt/found` | none | devices seen by the scan |
| `POST /bt/pair` | auth | pair, trust and connect the amp |
| `POST /bt/connect` | none | connect the paired amp |
| `POST /bt/disconnect` | none | disconnect the amp (it stays paired) |
| `POST /bt/forget` | auth | unpair the amp |

### `GET /info`

```json
{
  "api": 1,
  "version": "1.0.0",
  "id": "nowairplaying_123456",
  "name": "Bathroom Speaker",
  "area": "Bathroom",
  "mac": "B8:27:EB:12:34:56",
  "state": "claimed",
  "claimed_by": "homeassistant.local",
  "amp": {"mac": "F4:4E:FD:00:00:00", "name": "Kohler Amplifier",
          "paired": true, "connected": true},
  "mqtt": {"configured": true, "connected": true}
}
```

`amp` is `null` before pairing. **No response ever contains a password or the token.**

### `POST /claim`

```json
{
  "name": "Bathroom Speaker",
  "area": "Bathroom",
  "mqtt": {"host": "homeassistant.local", "ip": "192.168.1.10", "port": 1883,
           "username": "nowairplaying_123456", "password": "…"}
}
```

- `200` → `{"token": "…"}`. `409 already_claimed` if the node is claimed.
- **Effect:** writes `[mqtt]` and `[node]` into speakerd's config and restarts speakerd.
  speakerd then publishes its Home Assistant discovery.
- `ip` is the fallback when `host` doesn't resolve, for example across VLANs.
  Home Assistant reads its hostname from the Supervisor at claim time.
- The node id stays the MAC-derived `id`. `name` and `area` only set the display name
  and the suggested area, so a later rename never re-keys any entity.
- The call returns once the config is written. Home Assistant then polls
  `GET /verify` until `mqtt_connected` passes, or times out and reports it.

### `POST /mqtt`

Same `mqtt` object as `/claim`. Replaces the login and restarts speakerd. `200` →
`{"ok": true}`.

### `POST /release`

- **Effect:** speakerd first clears its retained Home Assistant discovery and
  announcement topics with empty publishes, so no ghost entities remain. Then `[mqtt]`
  is removed, speakerd restarts standalone, and the state returns to `unclaimed`.
- The amp pairing is kept.
- `200` → `{"ok": true}`.

### `POST /bt/scan`, `GET /bt/found`

- `POST /bt/scan`, body `{"seconds": 20}` (at most 30). `202` → `{"ok": true}`, or
  `409 busy` if a scan or pair is already running.
- `GET /bt/found` returns:

  ```json
  {"scanning": true,
   "devices": [{"mac": "F4:4E:FD:00:00:00", "name": "Kohler Amplifier",
                "rssi": -58, "likely_amp": true}]}
  ```

  `likely_amp` marks a device that advertises as an audio sink. It's only a hint for
  sorting the list; the user still picks.

### `POST /bt/pair`

- Body: `{"mac": "F4:4E:FD:00:00:00"}`. The user must first put the amp in pairing mode.
- **Effect:** pair, trust, connect, write `amp_mac` and `amp_name` into speakerd's
  config, and restart speakerd.
- **The Kohler needs a PIN** (confirmed 2026-09-28). It uses legacy PIN pairing with the
  fixed PIN `0000`. The node's agent must answer BlueZ's `RequestPinCode` with it. A
  NoInputNoOutput agent fails with `org.bluez.Error.AuthenticationFailed`.
- `200` → `{"ok": true}`, or `502 pair_failed` with the BlueZ error in `message`.
- **To confirm on a real amp:** how its pairing mode is entered, for the setup page's
  instructions.

### `POST /bt/connect`, `POST /bt/disconnect`, `POST /bt/forget`

- **connect / disconnect:** connect or disconnect the paired amp. Disconnect is
  deliberate: speakerd's auto-reconnect stays off until the next connect, the same rule
  as the Home Assistant switch. Both return `{"ok": true}` or `502 failed`.
- **forget:** removes the pairing and clears `amp_mac`. speakerd keeps running, with no
  amp.

### `GET /verify`

```json
{"ok": false,
 "checks": [
   {"id": "bluez_version", "ok": true, "detail": "bluetoothd 5.87"},
   {"id": "pipewire_version", "ok": true, "detail": "1.4.2"},
   {"id": "amp_connected", "ok": false, "detail": "not connected"}
 ]}
```

Check ids are stable; Home Assistant raises a repair issue when one that passed starts
failing. All of them check the **running** system, not the installed packages:

| id | Passes when |
|---|---|
| `bluez_version` | the running `bluetoothd` is 5.87 |
| `pipewire_version` | PipeWire ≥ 1.4 runs in the node user's session |
| `wireplumber_version` | WirePlumber ≥ 0.5.8 runs in the node user's session |
| `packages_held` | PipeWire, WirePlumber and the NowAirPlaying packages are apt-held |
| `shairport_airplay2` | shairport-sync runs and `-V` contains `-AirPlay2-` |
| `shairport_dbus` | `org.gnome.ShairportSync` is owned on the node user's session bus |
| `no_mpris` | no `org.mpris.MediaPlayer2.*` player is on the session or system bus, and `mpris-proxy` isn't running |
| `nqptp_active` | `nqptp.service` is active |
| `mdns` | `avahi-daemon` runs and `/etc/nsswitch.conf` has `mdns4_minimal` |
| `speakerd_running` | the speakerd user service is active |
| `amp_paired` | the configured amp is paired and trusted |
| `amp_connected` | the amp is connected |
| `amp_player` | exactly one player, speakerd's `/org/speakerd/player`, is registered on the amp's adapter |
| `mqtt_connected` | claimed nodes only: speakerd is connected to the broker |

### Errors

Every error is JSON: `{"error": "<code>", "message": "<human text>"}`.

| HTTP | `error` | When |
|---|---|---|
| 400 | `bad_request` | missing or invalid field |
| 401 | `unauthorized` | token missing or wrong on a claimed node |
| 404 | `not_found` | no such endpoint, or no paired amp for connect or disconnect |
| 409 | `already_claimed` | `/claim` on a claimed node |
| 409 | `busy` | a scan or pair is already running |
| 502 | `pair_failed`, `failed` | BlueZ or systemd refused. `message` carries its error. |

## The setup page

At `http://nowairplaying-<nnnn>.local/` the node serves one plain page with no
framework, which calls the endpoints above:

1. **Status:** name, amp connected or not, and the verify checks as green or red.
2. **Pair the amplifier:** "Put the amplifier in pairing mode, then press Scan" → a list
   with likely amps first → pick one → Pair → result.
3. **Amplifier:** Connect, Disconnect and Forget buttons.
4. **When claimed:** "Managed by Home Assistant at `<host>`". Status, Connect and
   Disconnect stay; the other controls are hidden.

## Why HTTP, not HTTPS

- **The setup page needs HTTP.** A node can only offer HTTPS with a self-signed
  certificate, and every phone and browser shows a full-screen security warning for
  one. For a beginner's first contact with the device, that is the wrong message.
- **The Home Assistant integration is unaffected either way.** It calls the node from
  HA's server side, not from the browser, so whether HA itself is served over http or
  https makes no difference.
- **The exposure matches what is already there.** The MQTT password crosses the home
  network once, in `/claim`. MQTT itself then runs as plain MQTT on port 1883, the
  Mosquitto add-on's default, which sends the same login in the clear on every
  connect.
- **Possible later upgrade:** HTTPS for the API only, with the certificate fingerprint
  in the zeroconf TXT record for Home Assistant to pin. The page stays HTTP. Not
  planned for v1.

## Open questions

These go to Home Assistant's integration owner:

1. `_nowairplaying._tcp` as the service type, and `mac` as the `unique_id`: agreed?
2. Deleting the config entry calls `POST /release`. If the node is unreachable at that
   moment, the entry is still removed, and the reset file is the fallback. Acceptable?
3. Should the integration rotate the MQTT login via `POST /mqtt`, or is one login per
   claim enough?
4. Which `/verify` check ids should raise a repair issue, and which should only show on
   the device page? `amp_connected` probably shouldn't raise one, since a sleeping amp
   is normal.
