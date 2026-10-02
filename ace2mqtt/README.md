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
| `current_min` / `current_max` | Locally allowed current range in A (1–80; defaults 6–32) |
| `poll_interval` | Poll period in seconds (5–300) |
| `mqtt_host`, `mqtt_port` | MQTT broker address |
| `mqtt_username`, `mqtt_password` | Optional broker credentials |
| `mqtt_topic_prefix` | State and availability topic prefix |
| `discovery_prefix` | Home Assistant Discovery prefix |

The add-on publishes scalar fields returned by `alfenctl status --json` as
retained MQTT state and creates a sensor or binary sensor for each field. It
also creates Home Assistant MQTT numbers for socket current and Comfort charging
power (kW), a select entity for the solar charging mode (**comfort** or **green**), and buttons to
enable/disable the selected socket and turn the charging-profile override on
or off. The socket-disable button says that it can stop a live session;
pressing it explicitly confirms that operation.

To set a socket's current limit, use the discovered number entity or publish
an integer amp value to `ace2mqtt/control/current` (replace `ace2mqtt` with
the configured topic prefix). Values outside `current_min` and `current_max` are rejected locally;
the charger still applies its own limits. The command topic is never retained.
The other command topics are `.../control/socket_enable`,
`.../control/socket_disable`, `.../control/direct_start_on` and
`.../control/direct_start_off`; discovered buttons publish `PRESS` to these
topics. Comfort power accepts 1.35–22.00 kW in 0.05 kW steps and writes Alfen
property `3280_3` in watts; the charger's own supported range may be narrower.
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
