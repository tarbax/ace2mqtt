# Changelog

## 0.2.0-beta.6

- Remove the old flat configuration fields, their translations and automatic migration. Only the grouped configuration is supported; older installations must enter settings in the grouped layout.
- Update maintainer references to tarbax.

## 0.2.0-beta.5

- Updated to `alfenctl==0.2.0` (contract tests unchanged and passing).
- Comfort power and Green share are read with a single `alfenctl get 3280_3 3280_2` call, one charger connection less per readback round.
- The "static load balancing ... not licensed" warning is explained as a charger setting that does not affect ace2mqtt.

## 0.2.0-beta.4

- Short charger outages (TLS handshake timeouts) no longer flip every entity to unavailable: new `limits.offline_after` (default 300 s) keeps the last values; retries now back off at most 30 s.
- Each distinct `alfenctl` warning (for example "not licensed for load balancing") is logged once per hour instead of on every poll, with a hint to use `solar_controls: false`.

## 0.2.0-beta.3

- Options are grouped into `charger`, `mqtt`, `limits` and `evcc` sections. Existing flat configs are migrated automatically (legacy keys stay in the schema, empty, for now).
- RFID cards are configured as a list (`evcc.vehicles`) instead of a JSON string.
- Optional values (`mqtt.username`, `mqtt.password`, `evcc.url`, `evcc.password`, `charger.port`) are truly optional; `evcc.url` is validated as a URL and the charger port follows the HTTP setting.
- New `log_level` option (debug logs every alfenctl call) and `solar_controls` switch to hide and skip Comfort power, Green share and Charging mode.
- Option translations updated for the new layout.

## 0.2.0-beta.2

- Documentation rewritten in English; log, error and entity texts are English too.
- Entities reviewed: readable names (relative to the new "Alfen charger" device, which links to the charger UI), icons, device/state classes, diagnostic categories. Unique IDs are unchanged.
- New diagnostic sensor "Last successful poll".
- Entities the charger stops reporting are removed from Discovery after three polls.
- The RFID scan pulse is no longer retained (a leftover retained value is cleared).
- Optional evcc password (`evcc_password`) for evcc installations with authentication.
- Add-on option translations (`translations/en.yaml`); CI runs `ruff check`.

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
