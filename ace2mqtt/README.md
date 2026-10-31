# ace2mqtt

`ace2mqtt` is a Home Assistant add-on for Alfen EV chargers. It polls the
charger, publishes its status over MQTT with Home Assistant MQTT Discovery,
exposes a small set of safe controls, and can optionally tell
[evcc](https://evcc.io) which vehicle is plugged in based on the RFID card
that was used. There is no web interface; everything shows up as entities on a
single **Alfen charger** device.

## Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Entities](#entities)
- [Controls](#controls)
- [MQTT topics](#mqtt-topics)
- [RFID and evcc](#rfid-and-evcc)
- [Reliability behaviour](#reliability-behaviour)
- [Troubleshooting](#troubleshooting)
- [Limitations](#limitations)
- [Development](#development)
- [Credits and license](#credits-and-license)

## Requirements

- An Alfen charger reachable from Home Assistant, with its management
  interface enabled and the installer/admin credentials at hand.
- An MQTT broker, for example the Mosquitto add-on, and the Home Assistant MQTT
  integration.

## Installation

1. In Home Assistant open **Settings → Add-ons → Add-on Store**, use the menu
   **Repositories** and add `https://github.com/tarbax/ace2mqtt`.
2. Install **ace2mqtt**.
3. Fill in the charger settings (see below) and start the add-on.

When the MQTT options are left at their defaults (`core-mosquitto`, no
username), the add-on asks the Supervisor for the broker details, so the
Mosquitto add-on works without any MQTT configuration. Set `mqtt_host`,
`mqtt_username` and `mqtt_password` yourself to use another broker.

## Configuration

Options are grouped in sections. Only the charger host and password are
required; everything else has a sensible default.

### Charger

| Option | Default | Description |
| --- | --- | --- |
| `charger.host` | – | Hostname or IP address of the charger (required). |
| `charger.port` | derived | Optional. Defaults to 443, or 80 when `http` is on, so switching to HTTP no longer needs a port change. |
| `charger.username` / `charger.password` | `admin` / – | Charger credentials. |
| `charger.http` | `false` | Use plain HTTP instead of HTTPS. |
| `charger.socket` | `1` | Socket (1 or 2) that the controls and RFID detection apply to. |

### MQTT

| Option | Default | Description |
| --- | --- | --- |
| `mqtt.host` / `mqtt.port` | `core-mosquitto` / `1883` | MQTT broker. |
| `mqtt.username` / `mqtt.password` | unset | Optional credentials. |
| `mqtt.topic_prefix` | `ace2mqtt` | Prefix of all state, control and availability topics. Must not contain `+`, `#` or spaces. Use a different prefix per charger. |
| `mqtt.discovery_prefix` | `homeassistant` | MQTT Discovery prefix of Home Assistant. |

With the default host and no username, the broker details come from the
Supervisor's MQTT service, so the Mosquitto add-on needs no configuration.

### Limits and polling

| Option | Default | Description |
| --- | --- | --- |
| `limits.current_min` / `limits.current_max` | `6` / `32` | Range of the charging-current control in ampere (1–64). The charger's station-wide limit lowers the maximum further, live. |
| `limits.comfort_power_max_kw` | `4.0` | Upper end of the Comfort power control, 1.35–11 kW in steps of 0.05. |
| `limits.meter_current_max` | `63` | Upper bound (1–100 A) for the two smart-meter controls below. Set it to your main fuse rating. |
| `limits.poll_interval` | `10` | Seconds to wait between polls (5–300). A delay after a poll completes, not a fixed sampling period. |
| `limits.offline_after` | `300` | Seconds of failed polls before the entities become unavailable. During a shorter outage (for example a TLS handshake timeout) they keep their last value; *Last successful poll* shows the staleness. `0` marks them unavailable immediately. |

### evcc (optional)

| Option | Default | Description |
| --- | --- | --- |
| `evcc.url` | unset | Base URL of evcc, for example `http://evcc:7070`. Unset disables the evcc bridge. |
| `evcc.loadpoint` | `1` | evcc loadpoint to assign the vehicle to. |
| `evcc.password` | unset | Only needed when evcc authentication is enabled. |
| `evcc.vehicles` | `[]` | One row per RFID card: its `uid` and the evcc vehicle's internal name. |

```yaml
evcc:
  url: "http://evcc:7070"
  loadpoint: 1
  vehicles:
    - uid: "04AABBCCDDEEFF"
      vehicle: "db:1"
```

Use the vehicle's **internal name** from evcc:

- For a vehicle created through the evcc web interface, this is usually
  `db:<number>`, for example `db:1` or `db:2`. Enter the complete value,
  including the `db:` prefix. Use the actual ID of your vehicle; the number
  is not the loadpoint number.
- For a vehicle defined in `evcc.yaml`, use its `name` value, for example
  `vehicle: "my_car"` when the vehicle configuration has `name: my_car`.

The display title shown in evcc (such as “Family car”) is a separate value.
You can check the internal names in evcc's `/api/state` response: the keys
under `vehicles` identify the configured vehicles, while each vehicle's
`title` is its display name.

UIDs are matched case-insensitively and ignore separators, so `04:aa:bb:cc:dd:ee:ff`
and `04AABBCCDDEEFF` are the same card.

### General

| Option | Default | Description |
| --- | --- | --- |
| `solar_controls` | `true` | Show *Comfort charging power*, *Green share* and *Charging mode*. Turn off when the firmware does not support load balancing; the entities are then removed and not polled. |
| `meter_controls` | `false` | Adds writable *Maximum meter current* and *Safe current (P1 failsafe)* entities (see [Smart meter controls](#smart-meter-controls)). Off by default because they are installer settings of the grid connection. |
| `log_level` | `info` | `debug` logs every `alfenctl` call with its duration; useful when troubleshooting. |

### Upgrading from versions before 0.2

Only the grouped configuration is supported from `0.2.0-beta.6` onward.
When upgrading from a version before 0.2, enter your settings under `charger`,
`mqtt`, `limits` and `evcc` before starting the add-on. Configure RFID cards in
`evcc.vehicles`. The old flat fields and automatic migration have been removed.

## Entities

All entities belong to the device **Alfen charger**, which links to the
charger's own web interface. Entity names are relative to the device. Entity
and unique IDs are stable between versions.

### Smart meter controls

With `meter_controls: true` two extra number entities (category *Configuration*,
entered in a box, not a slider) can change load-balancing settings of the charger:

| Entity | Charger setting | Range |
| --- | --- | --- |
| Maximum meter current | the grid connection limit that active load balancing keeps the house below (`alfenctl lb set --max-meter-current`) | 1 to `limits.meter_current_max` A |
| Safe current (P1 failsafe) | the current the charger falls back to when the smart meter (P1) stops answering (`alfenctl lb set --safe-current`) | 0 to `limits.meter_current_max` A |

Both accept whole amperes only. After each write the value is read back from
the charger. They replace the read-only *Maximum installation current* and
*Safe current limit* diagnostic sensors, which show the same values. A wrong
*Maximum meter current* can overload your main fuse; set `limits.meter_current_max`
to the real rating before enabling this.

## Controls

| Entity | Type | Notes |
| --- | --- | --- |
| Socket *n* enabled | switch | Puts the socket in or out of service. Switching off can stop a running session. |
| Charging profile override | switch | Alfen "direct start": overrides an installed charging profile. It does not start a session by itself. |
| Charging current | number (A) | Maximum current of the selected socket, within the configured range. |
| Comfort charging power | number (kW) | Solar *Comfort* level, 1.35 kW up to `comfort_power_max_kw`. |
| Green share | number (%) | Solar *Green* share, 0–100 %. |
| Charging mode | select | `off`, `comfort` or `green`. |

### Sensors

Status fields reported by the charger are discovered automatically with a
readable name, icon, unit and Home Assistant device or state class, so power
and energy can be used in the Energy dashboard.

| Entity | Class / notes |
| --- | --- |
| Active power | power (W) |
| Energy delivered, Energy consumed | energy (kWh), total increasing |
| Voltage L1–L3, Current L1–L3 | voltage (V), current (A) |
| Temperature | temperature (°C) |
| Station operative, Socket *n* operative | binary |
| Socket *n* state, Socket *n* vehicle state | text (state and IEC 61851 mode 3 state) |
| Last RFID card | last card recorded for the selected socket |
| RFID card scan | pulses the UID for two seconds, handy as an automation trigger |
| Maximum station current, Maximum installation current, Safe current limit | diagnostic |
| Socket *n* display, LED, power state, error code, error, error severity | diagnostic |
| Last successful poll | diagnostic timestamp; keeps its value while the charger is unreachable |

Fields the charger stops reporting are removed again after three polls without
them. Unknown fields are still discovered, using a readable name derived from
the field.

### Availability

All entities except *Last successful poll* become unavailable when the add-on
stops or cannot read the charger (MQTT last-will plus an explicit `offline`).

## Controls

- Control values are validated before anything is sent to the charger, and no
  free text ever reaches a shell.
- After every write the value is read back from the charger and published, so
  the entity shows what the charger actually uses, not what was requested.
  A failed write or an unreadable value never echoes the request.
- Retained control messages are ignored, so a broker restart cannot replay
  an old command.
- Control states are also refreshed every third poll to pick up changes made
  elsewhere, for example in the Alfen app.
- The legacy button-style topics (`socket_enable`, `socket_disable`,
  `direct_start_on`, `direct_start_off`, payload `PRESS`) are still accepted
  but no longer discovered.

## MQTT topics

With the default prefix `ace2mqtt`:

| Topic | Purpose |
| --- | --- |
| `ace2mqtt/availability` | `online` / `offline` (retained, last will). |
| `ace2mqtt/state/<entity>` | State of an entity (retained, except `rfid_id_pulse`). |
| `ace2mqtt/control/<name>` | Command topics: `socket`, `charging_profile_override` (`ON`/`OFF`), `current`, `comfort_power`, `green_share`, `solar_mode`. |

The MQTT client id is derived from the charger host, so several add-on
instances can share a broker as long as each has its own topic prefix.

## RFID and evcc

The charger does not announce card scans over its API. The add-on therefore
reads the charger's transaction history after a vehicle connects and picks the
card of the transaction that belongs to this plug-in:

1. While no vehicle is connected, the newest transaction record is remembered
   as a baseline.
2. When a vehicle connects, the add-on checks for a newer transaction for up
   to three minutes (at most every 15 seconds).
3. The card found is published to *Last RFID card* and *RFID card scan*, and if
   it is listed in `uid_vehicle_map`, the vehicle is assigned to the evcc
   loadpoint. Unknown cards are ignored; evcc errors other than HTTP 429 and
   5xx are not retried.

If the add-on starts during an active session there is no baseline, and it waits
for the next session instead of risking an old card.

## Reliability behaviour

- A failing poll does not rebuild the MQTT connection or forget the session
  state. Entities keep their last value for `limits.offline_after` seconds,
  after which the add-on reports `offline`. It retries after 10, 20 and then
  30 seconds and resumes where it left off.
- Commands run on a separate worker thread, so a slow charger cannot stall MQTT
  keepalives.
- Discovery configs and states are only published when they change, and again
  after an MQTT reconnect.
- The add-on handles SIGTERM, so it stops quickly and marks itself offline.

## Troubleshooting

- **No entities** – check the add-on log. Verify the charger host, port and
  credentials (set `log_level: debug` for details), and that the MQTT integration uses the same discovery prefix.
- **`static load balancing is on, but this station is not licensed for it`** – a setting on the charger itself (that load-balancing mode is switched on without a licence). It does not affect ace2mqtt; each distinct `alfenctl` warning is logged once per hour and repeats go to debug level.
- **Entities flip to unavailable now and then** – the log shows `cannot reach the charger … handshake operation timed out`. The charger drops TLS connections when busy (for example while the Alfen app or another client is connected). Raise `limits.offline_after`, and increase `limits.poll_interval` to reduce load.
- **Warnings that a value cannot be read** – some firmware versions do not
  support every property (Comfort power, Green share). The previous value is
  kept and the warning repeats; turn off `solar_controls` to hide them.
- **Everything unavailable** – the charger could not be polled; see the log
  for the reason. *Last successful poll* shows when it last worked.
- **evcc vehicle not assigned** – the UID is not in `uid_vehicle_map`, the
  vehicle name is not evcc's internal name (HTTP 404 in the log), or the card
  was not recorded as a transaction.

## Limitations

- Tested against the output of a real charger for parsing, but not end to end:
  writes, MQTT/Home Assistant behaviour and the evcc password login are only
  covered by unit tests against the pinned `alfenctl` CLI.
- Every control readback and the status call run one `alfenctl` process each,
  serialised on the charger connection (Comfort power and Green share share one call).
- RFID is derived from transactions: rejected scans and cards the charger does
  not record cannot be identified, and a short authorisation can fall between
  polls.
- Only one socket is controlled per add-on instance (`charger.socket`); status
  of both sockets is published.

## Development

Run the tests from the repository root:

```sh
pip install -r ace2mqtt/requirements.txt
PYTHONPATH=ace2mqtt:ace2mqtt/tests python3 -m unittest discover -s ace2mqtt/tests
```

Without the pinned `alfenctl==0.2.0` package the upstream contract tests are
skipped. The tests never contact a charger, broker, Home Assistant or evcc. CI
also runs `ruff check`. See [`docs/cli-audit.md`](docs/cli-audit.md) for the CLI
audit and [`CHANGELOG.md`](CHANGELOG.md) for changes.

## Credits and license

The `ace2mqtt` add-on is maintained by [tarbax](https://github.com/tarbax)
and is licensed under EUPL-1.2.

The add-on uses [`alfenctl`](https://github.com/pbasista/alfenctl) for Alfen
charger communication and its command-line interface. It is installed separately
from PyPI and licensed under EUPL-1.2; see its repository for source and license
notices.
