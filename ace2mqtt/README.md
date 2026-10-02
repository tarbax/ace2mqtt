# ace2mqtt

Home Assistant add-on that reads an Alfen charging station using the upstream
[`alfenctl`](https://github.com/pbasista/alfenctl) project and publishes the
status through MQTT. MQTT entities are announced with Home Assistant
MQTT Discovery. There is no web interface.

## Installation

Add this repository to the Home Assistant Add-on Store, install **ace2mqtt**,
fill in the charger and MQTT settings, then start the add-on. MQTT must be
enabled on your broker. The charger must be reachable from Home Assistant and
have the management API enabled.

## Settings

| Setting | Purpose |
| --- | --- |
| `charger_host` | IP address or hostname of the Alfen charger; a reserved IP is recommended |
| `charger_port` | Alfen management port (normally 443) |
| `charger_username` / `charger_password` | Charger login |
| `charger_http` | Use HTTP for older stations |
| `socket_number` | Socket selected by control buttons and current command |
| `current_min` / `current_max` | Locally allowed current range in A (1–80; defaults 6–32); the live station maximum further caps the slider and accepted commands |
| `comfort_power_max_kw` | Upper limit for the Comfort power number in kW (1.35–22; default 4.0) |
| `poll_interval` | Poll period in seconds (5–300) |
| `mqtt_host`, `mqtt_port` | MQTT broker address |
| `mqtt_username`, `mqtt_password` | Optional broker credentials |
| `mqtt_topic_prefix` | State and availability topic prefix |
| `discovery_prefix` | Home Assistant Discovery prefix |
| `evcc_base_url` | Optional evcc REST base URL, for example `http://evcc:7070`; leave blank to disable the bridge |
| `evcc_loadpoint_id` | evcc loadpoint ID to assign a vehicle to (default `1`) |
| `uid_vehicle_map` | JSON object mapping Alfen RFID UIDs to evcc vehicle `name` values, for example `{"04AABBCCDDEEFF":"bmwx130e"}` |

The add-on publishes scalar fields returned by `alfenctl status --json` as
retained MQTT state and creates a sensor or binary sensor for each field. It
rounds floating-point sensor values to two decimals before publishing them and
sets Home Assistant's suggested display precision to two decimals. It
also creates Home Assistant MQTT numbers for socket current and Comfort charging
power (kW), a select entity for the solar charging mode (**comfort** or **green**),
switches for the selected socket and charging-profile override, and a sensor for
the latest RFID ID recorded in a charging transaction. Turning the
socket switch off may stop an active charging session.

The RFID sensor is updated when the selected socket reports an authorized card
and Alfen has recorded its transaction. Rejected card scans do not expose a card
ID through the status interface and therefore cannot be reported by this sensor.

When `evcc_base_url` and `uid_vehicle_map` are configured, the same authorized
RFID ID is normalised (separators removed, uppercase) and matched against the
map. A match assigns the mapped evcc vehicle to `evcc_loadpoint_id` through the
evcc REST API. Configure the exact vehicle `name` from evcc, not its display
title. Unknown IDs are ignored. The bridge does not clear the vehicle on
disconnect; evcc's own loadpoint behavior remains in control. Leave
`evcc_base_url` empty to keep the evcc integration disabled.

To set a socket's current limit, use the discovered number entity or publish
an integer amp value to `ace2mqtt/control/current` (replace `ace2mqtt` with
the configured topic prefix). Values outside `current_min` and `current_max` are rejected locally;
the charger still applies its own limits. The command topic is never retained.
The socket switch publishes `ON` or `OFF` to `.../control/socket`; the charging-
profile override switch uses `.../control/charging_profile_override`. Their
confirmed state is retained on the matching `.../state/` topics. Comfort power
accepts 1.35 kW up to `comfort_power_max_kw` in 0.05 kW steps and writes Alfen
property `3280_3` in watts. Set this option to the maximum supported by your
charger; it defaults to 4 kW for this installation.
The mode select publishes `comfort` or `green` to
`.../control/solar_mode`. Retained control messages are ignored. The add-on does not expose
firmware upgrades, factory reset, credential changes, network settings or
arbitrary property commands.

Direct start only overrides an installed charging profile; it does not itself
initiate a charging session. The current controls the configured per-socket
maximum and cannot exceed the configured local bounds.

## License

This add-on's wrapper is provided under EUPL-1.2. It installs `alfenctl` from
PyPI at image build time; `alfenctl` itself is separately licensed under
EUPL-1.2. See its upstream repository for its full license and notices.
