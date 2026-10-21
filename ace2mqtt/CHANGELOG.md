# Changelog

## 0.2.0-beta.1

- A failing poll no longer rebuilds the MQTT connection or resets the plug-in/RFID session state; the add-on backs off (10–60 s) and publishes `offline` until the charger answers again.
- SIGTERM/SIGINT are handled, so the add-on stops promptly and publishes `offline`.
- MQTT commands run on a worker thread instead of blocking the MQTT network thread.
- Discovery configs and sensor states are published only when they change (and again after an MQTT reconnect). Control readbacks run every third poll and directly after a write.
- RFID search retries at most every 15 s.
- MQTT client id is derived from the charger host, so multiple instances no longer collide.
- Known status fields get `device_class`/`state_class`/unit (Energy dashboard) and technical ones become diagnostic entities.
- Broker credentials are taken from the Supervisor MQTT service when left at the defaults.
- Current slider minimum and command validation use the same effective bounds.
- evcc: HTTP 4xx (except 429) is no longer retried.
- `alfen.toml` is created with mode 0600 from the start; malformed socket data no longer aborts a poll.
- CI workflow runs the unit and upstream contract tests.
