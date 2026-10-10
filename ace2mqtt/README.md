# ace2mqtt

`ace2mqtt` is a Home Assistant add-on for Alfen charging stations. It reads
charger status and publishes it over MQTT with Home Assistant MQTT Discovery.
It also offers a small set of charger controls and an optional RFID-to-evcc
vehicle assignment. There is no web interface.

## Install

Add this repository to the Home Assistant Add-on Store, install **ace2mqtt**,
enter the charger and MQTT settings, and start the add-on. The charger must be
reachable from Home Assistant, its management interface must be enabled, and
an MQTT broker must be running.

## Configure

Required settings are the charger host, username and password, plus the MQTT
host. The defaults use port 443, socket 1, a 6–32 A control range, a 4 kW
Comfort limit, a 10-second polling delay and the `ace2mqtt` MQTT prefix.

To assign a vehicle in evcc, set `evcc_base_url`, `evcc_loadpoint_id`, and
`uid_vehicle_map`. Map each Alfen RFID UID to the vehicle's **internal name**
from evcc, not its display title. For example:

```yaml
evcc_base_url: "http://evcc:7070"
evcc_loadpoint_id: 1
uid_vehicle_map: '{"04AABBCCDDEEFF":"vehicle_name"}'
```

Leave `evcc_base_url` empty to disable vehicle assignment.

## Entities and controls

The add-on discovers charger status sensors, socket current and Comfort power
numbers, Green share, the solar mode (`off`, `comfort`, `green`), socket and
charging-profile switches, and RFID sensors. Control states are read back from
the charger. Socket current and Comfort power are limited by the add-on's
configured bounds.

The optional evcc bridge looks up an authorized RFID in `uid_vehicle_map` and
assigns the matching vehicle to the configured loadpoint. RFID is obtained from
Alfen's transaction history after authorization; rejected scans and cards not
recorded by the charger cannot be identified by this method. The short-lived
`RFID-ID` sensor clears after two seconds; `Laatste RFID-ID` retains the last
recorded card.

Direct start overrides an installed charging profile; it does not start a
charging session by itself. Turning the socket switch off can stop a session.

## Credits and license

This add-on is based on [`alfenctl`](https://github.com/pbasista/alfenctl),
created by [Peter Basista](https://github.com/pbasista). `alfenctl` provides
the Alfen charger communication and command-line interface used by this
project. Many thanks to Peter for making that project available. `alfenctl`
is installed separately from PyPI and is licensed under EUPL-1.2; see its
repository for its source and license notices. The `ace2mqtt` add-on wrapper
is also licensed under EUPL-1.2.

## Development

Run the local tests from the repository root:

```sh
PYTHONPATH=ace2mqtt python3 -m unittest discover -s ace2mqtt/tests
```

Tests that check output and CLI compatibility against `alfenctl==0.1.0` are
skipped unless the pinned package is installed. Local tests do not contact a
charger, MQTT broker, Home Assistant or evcc instance. See
[`docs/cli-audit.md`](docs/cli-audit.md) for the detailed CLI audit and known
runtime boundaries.
