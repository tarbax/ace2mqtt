#!/usr/bin/env python3
"""Poll and control an Alfen charger through MQTT + Home Assistant Discovery."""
import json
import logging
import math
import os
import queue
import re
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import paho.mqtt.client as mqtt
import requests

LOG = logging.getLogger("ace2mqtt")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ALFEN_LOCK = threading.RLock()
RFID_PULSE_LOCK = threading.Lock()
RFID_PULSE_TIMERS = {}
EVCC_RETRY_DELAYS = (1, 3, 5)
RFID_SEARCH_SECONDS = 180
# alfenctl 0.1.0 loadbalancing.SOLAR_MODES; --solar-mode expects an integer.
SOLAR_MODE_VALUES = {"off": "0", "comfort": "1", "green": "2"}
STOP = threading.Event()
# Control readbacks cost one alfenctl process each; external changes (for
# example from the Alfen app) are picked up every Nth poll, own writes at once.
CONTROL_POLL_EVERY = 3
RFID_RETRY_SECONDS = 15
MAX_BACKOFF_SECONDS = 60

def clean_id(value):
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "value"

def options():
    # Home Assistant add-on options are mounted at /data/options.json.
    with open("/data/options.json", encoding="utf-8") as f:
        cfg = json.load(f)
    required = ("charger_host", "mqtt_host", "mqtt_topic_prefix", "discovery_prefix")
    for key in required:
        if not str(cfg.get(key, "")).strip():
            raise ValueError(f"Instelling '{key}' is verplicht")
    return cfg

def validate_options(cfg):
    if not 1 <= int(cfg["current_min"]) <= int(cfg["current_max"]) <= 64:
        raise ValueError("Laadstroomgrenzen moeten 1 <= current_min <= current_max <= 64 A zijn")
    comfort_max = Decimal(str(cfg.get("comfort_power_max_kw", 4.0)))
    if (not comfort_max.is_finite() or not Decimal("1.35") <= comfort_max <= Decimal("11.00")
            or (comfort_max * 1000) % 50):
        raise ValueError("comfort_power_max_kw moet tussen 1,35 en 11,00 kW liggen in stappen van 0,05")
    for key in ("mqtt_topic_prefix", "discovery_prefix"):
        value = str(cfg[key]).strip("/")
        if not value or any(char in value for char in ("+", "#", " ")):
            raise ValueError(f"Instelling '{key}' bevat een ongeldig MQTT-topicdeel")
    if str(cfg.get("evcc_base_url", "")).strip():
        if not str(cfg["evcc_base_url"]).strip().startswith(("http://", "https://")):
            raise ValueError("evcc_base_url moet met http:// of https:// beginnen")
        try:
            if int(cfg.get("evcc_loadpoint_id", 1)) < 1:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("evcc_loadpoint_id moet een positief geheel getal zijn") from None
    try:
        uid_vehicle_map(cfg)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("uid_vehicle_map moet een JSON-object met UID-voertuignaamparen zijn") from exc

def toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)

def write_alfen_config(cfg):
    # This follows alfenctl's documented alfen.toml settings format and keeps
    # credentials out of command-line arguments and process listings.
    content = (
        f"username = {toml_string(cfg['charger_username'])}\n"
        f"password = {toml_string(cfg['charger_password'])}\n\n"
        "[stations.ace2mqtt]\n"
        f"host = {toml_string(cfg['charger_host'])}\n"
        f"http = {'true' if cfg.get('charger_http') else 'false'}\n"
    )
    # Create the file private from the start so the password is never readable.
    with open("/data/alfen.toml", "w", encoding="utf-8",
              opener=lambda path, flags: os.open(path, flags, 0o600)) as f:
        f.write(content)
    os.chmod("/data/alfen.toml", 0o600)

def alfen_command(cfg, *args):
    # alfenctl adds these as common options to each command parser, not to the
    # root parser. Keep them after the command/action so argparse recognizes
    # the command before parsing its connection options.
    return ["alfenctl", *args, "--config", "/data/alfen.toml",
            "--port", str(cfg["charger_port"]), "--station", "ace2mqtt"]

def run_alfen(cfg, *args, timeout=30):
    # alfenctl talks to one charger over TCP; serialize polling and control so
    # concurrent requests cannot overlap on the charger connection.
    with ALFEN_LOCK:
        result = subprocess.run(alfen_command(cfg, *args), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"alfenctl exited {result.returncode}")
    for line in result.stderr.splitlines():
        if line.lower().startswith("warning:"):
            LOG.warning("alfenctl: %s", line.removeprefix("warning:").strip())
    return result.stdout

def run_status(cfg):
    try:
        return json.loads(run_alfen(cfg, "status", "--json", timeout=20))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"alfenctl gaf geen geldige JSON-status: {exc}") from exc

class PublishCache:
    """Publish wrapper that skips payloads identical to the last one sent.

    Discovery configs and unchanged states are retained on the broker, so they
    only need to be sent again after a (re)connect, which calls reset().
    """
    def __init__(self, client):
        self.client = client
        self.sent = {}

    def publish(self, topic, payload, qos=0, retain=False):
        if self.sent.get(topic) == payload:
            return None
        self.sent[topic] = payload
        return self.client.publish(topic, payload, qos=qos, retain=retain)

    def reset(self):
        self.sent.clear()

def availability_fields(root):
    return {"availability_topic": f"{root}/availability",
            "payload_available": "online", "payload_not_available": "offline"}

def publish_config(client, cfg, component, device_id, key, config):
    topic = f"{cfg['discovery_prefix'].strip('/')}/{component}/{device_id}/{key}/config"
    client.publish(topic, json.dumps(config, ensure_ascii=False), qos=1, retain=True)

def discovery_switch(client, cfg, device_id, device, key, name):
    root = cfg["mqtt_topic_prefix"].strip("/")
    config = {
        "name": name,
        "unique_id": f"{device_id}_{key}",
        "command_topic": f"{root}/control/{key}",
        "state_topic": f"{root}/state/{key}",
        "payload_on": "ON", "payload_off": "OFF",
        "state_on": "ON", "state_off": "OFF",
        "optimistic": False, "retain": False, "device": device,
        **availability_fields(root),
    }
    publish_config(client, cfg, "switch", device_id, key, config)

def effective_current_bounds(cfg):
    """Apply the live station-wide current ceiling to the configured bounds."""
    local_min = int(cfg["current_min"])
    effective_max = min(64, int(cfg["current_max"]))
    try:
        station_limit = float(cfg.get("_station_max_current_a"))
    except (TypeError, ValueError):
        station_limit = math.nan
    if math.isfinite(station_limit) and station_limit >= 0:
        effective_max = min(effective_max, math.floor(station_limit))
    return min(local_min, effective_max), effective_max

def publish_control_discovery(client, cfg, device_id, device):
    socket = int(cfg["socket_number"])
    root = cfg["mqtt_topic_prefix"].strip("/")
    avail = availability_fields(root)
    for key, name in (("socket", f"Socket {socket}"),
                      ("charging_profile_override", "Laadprofieloverride")):
        discovery_switch(client, cfg, device_id, device, key, name)
    # Remove the previous button entities from MQTT Discovery.
    discovery_prefix = cfg["discovery_prefix"].strip("/")
    for key in ("socket_enable", "socket_disable", "direct_start_on", "direct_start_off"):
        client.publish(f"{discovery_prefix}/button/{device_id}/{key}/config", "", qos=1, retain=True)
    for key, name, icon in (("rfid_id", "Laatste RFID-ID", "mdi:card-account-details"),
                            ("rfid_id_pulse", "RFID-ID", "mdi:card-account-details-outline")):
        publish_config(client, cfg, "sensor", device_id, key, {
            "name": name, "unique_id": f"{device_id}_{key}", "state_topic": f"{root}/state/{key}",
            "icon": icon, "device": device, **avail})
    current_min, current_max = effective_current_bounds(cfg)
    publish_config(client, cfg, "number", device_id, "current_limit", {
        "name": f"Laadstroom socket {socket}", "unique_id": f"{device_id}_current_limit",
        "command_topic": f"{root}/control/current", "state_topic": f"{root}/state/current_limit",
        "min": current_min, "max": current_max, "step": 1, "unit_of_measurement": "A",
        "mode": "slider", "optimistic": False, "retain": False, "device": device, **avail})
    publish_config(client, cfg, "number", device_id, "comfort_power", {
        "name": "Comfort laadvermogen", "unique_id": f"{device_id}_comfort_power",
        "command_topic": f"{root}/control/comfort_power", "state_topic": f"{root}/state/comfort_power",
        "min": 1.35, "max": float(Decimal(str(cfg.get("comfort_power_max_kw", 4.0)))), "step": 0.05,
        "unit_of_measurement": "kW", "mode": "slider",
        "optimistic": False, "retain": False, "device": device, **avail})
    publish_config(client, cfg, "number", device_id, "green_share", {
        "name": "Green share", "unique_id": f"{device_id}_green_share",
        "command_topic": f"{root}/control/green_share", "state_topic": f"{root}/state/green_share",
        "min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "mode": "slider",
        "optimistic": False, "retain": False, "device": device, **avail})
    publish_config(client, cfg, "select", device_id, "solar_mode", {
        "name": "Laadmodus", "unique_id": f"{device_id}_solar_mode",
        "command_topic": f"{root}/control/solar_mode", "state_topic": f"{root}/state/solar_mode",
        "options": list(SOLAR_MODE_VALUES), "optimistic": False, "retain": False,
        "device": device, **avail})

def handle_command(cfg, topic, payload, retained=False):
    """Translate a small fixed command set to alfenctl; never accept shell text."""
    root = cfg["mqtt_topic_prefix"].strip("/")
    if retained:
        LOG.warning("Retained MQTT-bedieningsbericht genegeerd")
        return
    socket = str(int(cfg["socket_number"]))
    fixed = {
        f"{root}/control/socket_enable": ("socket", "enable", socket),
        # Disabling may interrupt an active session. Pressing this explicitly
        # discovered button is the confirmation; -y prevents an interactive prompt.
        f"{root}/control/socket_disable": ("socket", "disable", socket, "--yes"),
        f"{root}/control/direct_start_on": ("direct-start", "on", "--socket", socket),
        f"{root}/control/direct_start_off": ("direct-start", "off", "--socket", socket),
    }
    switch_commands = {
        f"{root}/control/socket": {
            "ON": (("socket", "enable", socket), "ON"),
            "OFF": (("socket", "disable", socket, "--yes"), "OFF"),
        },
        f"{root}/control/charging_profile_override": {
            "ON": (("direct-start", "on", "--socket", socket), "ON"),
            "OFF": (("direct-start", "off", "--socket", socket), "OFF"),
        },
    }
    if topic in switch_commands:
        command = switch_commands[topic].get(payload.strip().upper())
        if command is None:
            LOG.warning("Ongeldige schakelaarwaarde voor %s", topic)
            return
        run_alfen(cfg, *command[0])
        LOG.info("Alfen-schakelaar %s ingesteld op %s", topic.rsplit("/", 1)[-1], command[1])
        return command[1]
    if topic in fixed:
        if payload != "PRESS":
            LOG.warning("Ongeldige payload voor bedieningsknop %s", topic)
            return
        run_alfen(cfg, *fixed[topic])
        LOG.info("Alfen-bedieningscommando uitgevoerd: %s", topic.rsplit("/", 1)[-1])
        return
    if topic == f"{root}/control/current":
        try:
            numeric_amps = float(payload)
        except ValueError:
            LOG.warning("Ongeldige laadstroomwaarde ontvangen")
            return
        if not math.isfinite(numeric_amps) or not numeric_amps.is_integer():
            LOG.warning("Laadstroom moet een geheel aantal ampere zijn")
            return
        amps = int(numeric_amps)
        current_min, current_max = effective_current_bounds(cfg)
        current_min = max(1, current_min)
        if not current_min <= amps <= current_max:
            LOG.warning("Laadstroom buiten ingestelde grens (%s-%s A)", current_min, current_max)
            return
        run_alfen(cfg, "current", "set", str(amps), "--socket", socket)
        LOG.info("Laadstroominstelling voor socket %s bijgewerkt", socket)
        return str(amps)
    if topic == f"{root}/control/comfort_power":
        try:
            kw = Decimal(payload.strip())
        except (InvalidOperation, ValueError):
            LOG.warning("Ongeldig comfortvermogen ontvangen")
            return
        comfort_max = Decimal(str(cfg.get("comfort_power_max_kw", 4.0)))
        if not kw.is_finite() or kw < Decimal("1.35") or kw > comfort_max or (kw * 1000) % 50:
            LOG.warning("Comfortvermogen moet 1,35-%s kW zijn in stappen van 0,05 kW", comfort_max)
            return
        watts = int(kw * 1000)
        run_alfen(cfg, "lb", "set", "--comfort-level", str(watts))
        LOG.info("Comfortvermogen ingesteld op %s kW", kw)
        return format(kw.normalize(), "f")
    if topic == f"{root}/control/green_share":
        try:
            percent = Decimal(payload.strip())
        except (InvalidOperation, ValueError):
            LOG.warning("Ongeldig green-sharepercentage ontvangen")
            return
        if not percent.is_finite() or percent < 0 or percent > 100 or not percent == percent.to_integral_value():
            LOG.warning("Green share moet een geheel percentage van 0 tot 100 zijn")
            return
        value = int(percent)
        run_alfen(cfg, "lb", "set", "--green-share", str(value))
        LOG.info("Green share ingesteld op %s%%", value)
        return str(value)
    if topic == f"{root}/control/solar_mode":
        mode = payload.strip().lower()
        if mode not in SOLAR_MODE_VALUES:
            LOG.warning("Ongeldige laadmodus ontvangen; toegestaan: off, comfort of green")
            return
        run_alfen(cfg, "lb", "set", "--solar-mode", SOLAR_MODE_VALUES[mode])
        LOG.info("Alfen-laadmodus ingesteld op %s", mode)
        return mode
    LOG.debug("Onbekend MQTT-commando genegeerd")

def subscribe_commands(client, cfg):
    root = cfg["mqtt_topic_prefix"].strip("/")
    for name in ("socket", "charging_profile_override", "socket_enable", "socket_disable", "direct_start_on", "direct_start_off", "current", "comfort_power", "green_share", "solar_mode"):
        client.subscribe(f"{root}/control/{name}", qos=1)

def mqtt_connected(client, cfg):
    subscribe_commands(client, cfg)
    # A fresh broker session may have lost retained discovery and state.
    cache = getattr(client, "ace_cache", None)
    if cache is not None:
        cache.reset()

def on_message(client, cfg, message):
    try:
        payload = message.payload.decode("utf-8", errors="replace")
        # Keep command and actual readback ordered against polling and other writes.
        with ALFEN_LOCK:
            value = handle_command(cfg, message.topic, payload, message.retain)
            root = cfg["mqtt_topic_prefix"].strip("/")
            name = message.topic.removeprefix(f"{root}/control/")
            readers = {
                "solar_mode": publish_solar_mode_readback,
                "current": publish_current_readback,
                "comfort_power": publish_comfort_power_readback,
                "green_share": publish_green_share_readback,
                "socket": publish_socket_readback,
                "charging_profile_override": publish_override_readback,
            }
            if value is not None and name in readers:
                readers[name](client, cfg)
            elif not message.retain and payload == "PRESS":
                if name in ("socket_enable", "socket_disable"):
                    publish_socket_readback(client, cfg)
                elif name in ("direct_start_on", "direct_start_off"):
                    publish_override_readback(client, cfg)
    except Exception as exc:
        LOG.error("Bedieningscommando mislukt: %s", exc)

def flatten(obj, prefix=""):
    """Flatten status objects while retaining field names for stable entity IDs."""
    values = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            name = f"{prefix}_{key}" if prefix else str(key)
            values.update(flatten(value, name))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            values.update(flatten(value, f"{prefix}_{index}"))
    elif isinstance(obj, (str, int, float, bool)) or obj is None:
        if not isinstance(obj, float) or math.isfinite(obj):
            values[prefix] = obj
    return values

def command_worker(client, cfg, commands):
    """Run commands off the paho network thread so slow alfenctl calls cannot stall MQTT."""
    while True:
        message = commands.get()
        if message is None:
            return
        on_message(client, cfg, message)

def connect_mqtt(cfg):
    # One client id per charger so two add-on instances do not kick each other off.
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=f"ace2mqtt_{clean_id(cfg['charger_host'])}")
    username, password = cfg.get("mqtt_username"), cfg.get("mqtt_password")
    if username:
        client.username_pw_set(username, password or None)
    root = cfg["mqtt_topic_prefix"].strip("/")
    client.will_set(f"{root}/availability", "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.ace_cache = PublishCache(client)
    commands = queue.Queue()
    client.ace_commands = commands
    threading.Thread(target=command_worker, args=(client, cfg, commands), daemon=True).start()
    client.on_connect = lambda connected_client, _userdata, _flags, _reason, _properties: mqtt_connected(
        connected_client, cfg)
    client.on_message = lambda _client, _userdata, message: commands.put(message)
    client.connect(cfg["mqtt_host"], int(cfg["mqtt_port"]), keepalive=60)
    client.loop_start()
    return client

SENSOR_METADATA = (
    (r"temperature_c", {"device_class": "temperature", "unit_of_measurement": "°C",
                        "state_class": "measurement"}),
    (r"currents_a_\d+", {"device_class": "current", "unit_of_measurement": "A",
                         "state_class": "measurement"}),
    (r"voltages_v_\d+", {"device_class": "voltage", "unit_of_measurement": "V",
                         "state_class": "measurement"}),
    (r"active_power_w", {"device_class": "power", "unit_of_measurement": "W",
                         "state_class": "measurement"}),
    (r"energy_(delivered|consumed)_kwh", {"device_class": "energy", "unit_of_measurement": "kWh",
                                          "state_class": "total_increasing"}),
    (r"(max_station|max_installation|active_safe)_current_a",
     {"device_class": "current", "unit_of_measurement": "A", "entity_category": "diagnostic"}),
    (r"sockets_\d+_(mode3|led|power|error|error_code|error_severity)",
     {"entity_category": "diagnostic"}),
)

def sensor_metadata(key):
    """Home Assistant classes for known alfenctl status fields (Energy dashboard etc.)."""
    for pattern, metadata in SENSOR_METADATA:
        if re.fullmatch(pattern, key):
            return dict(metadata)
    return {}

def publish_discovery(client, cfg, state):
    root = cfg["mqtt_topic_prefix"].strip("/")
    station_limit = state.get("max_station_current_a") if isinstance(state, dict) else None
    try:
        station_limit = float(station_limit)
    except (TypeError, ValueError):
        station_limit = math.nan
    if math.isfinite(station_limit) and station_limit >= 0:
        cfg["_station_max_current_a"] = station_limit
    else:
        cfg.pop("_station_max_current_a", None)
    device_id = "ace2mqtt_" + clean_id(cfg["charger_host"])
    device = {"identifiers": [device_id], "name": "Alfen laadpaal", "manufacturer": "Alfen",
              "model": "Alfen EV charger"}
    publish_control_discovery(client, cfg, device_id, device)
    for key, value in flatten(state).items():
        entity = clean_id(key)
        state_topic = f"{root}/state/{entity}"
        component = "sensor"
        config = {"name": key.replace("_", " ").title(), "unique_id": f"{device_id}_{entity}",
                  "state_topic": state_topic, "device": device, "availability_topic": f"{root}/availability",
                  "payload_available": "online", "payload_not_available": "offline"}
        if isinstance(value, bool):
            component = "binary_sensor"
            config.update({"payload_on": "true", "payload_off": "false"})
        elif isinstance(value, (int, float)):
            config.update(sensor_metadata(key))
            if isinstance(value, float):
                config["suggested_display_precision"] = 2
        if value is None or isinstance(value, (dict, list)):
            continue
        topic = f"{cfg['discovery_prefix'].strip('/')}/{component}/{device_id}/{entity}/config"
        client.publish(topic, json.dumps(config, ensure_ascii=False), qos=1, retain=True)
        if isinstance(value, bool):
            payload = str(value).lower()
        elif isinstance(value, (int, float)):
            payload = f"{value:.2f}" if isinstance(value, float) else str(value)
        else:
            payload = str(value)
        client.publish(state_topic, payload, qos=1, retain=True)

def parse_current_limit(output, socket_number):
    """Read one socket's configured limit from alfenctl's documented current view."""
    match = re.search(
        rf"^\s*Socket\s+{int(socket_number)}\s+maximum\s+([0-9]+(?:\.[0-9]+)?)\s+A\s*$",
        output,
        re.MULTILINE,
    )
    return match.group(1) if match else None

def publish_current_readback(client, cfg):
    try:
        # Keep readback and publication together with respect to command writes,
        # so an older poll cannot overwrite a newer successful set command.
        with ALFEN_LOCK:
            output = run_alfen(cfg, "current", timeout=20)
            value = parse_current_limit(output, cfg["socket_number"])
            if value is None:
                LOG.warning("Kon de ingestelde laadstroom voor socket %s niet uit alfenctl lezen", cfg["socket_number"])
                return False
            root = cfg["mqtt_topic_prefix"].strip("/")
            client.publish(f"{root}/state/current_limit", value, qos=1, retain=True)
            return True
    except Exception as exc:
        LOG.warning("Laadstroom teruglezen mislukt: %s", exc)
        return False

def parse_solar_mode(output):
    """Read the reported solar charging mode from alfenctl lb output."""
    match = re.search(r"^\s*Solar charging\s+(off|comfort|green)\s*$", output, re.MULTILINE | re.IGNORECASE)
    return match.group(1).lower() if match else None

def publish_solar_mode_readback(client, cfg):
    try:
        with ALFEN_LOCK:
            output = run_alfen(cfg, "lb", timeout=20)
            mode = parse_solar_mode(output)
            if mode is None:
                LOG.warning("Kon de laadmodus niet uit alfenctl lezen")
                return False
            root = cfg["mqtt_topic_prefix"].strip("/")
            client.publish(f"{root}/state/solar_mode", mode, qos=1, retain=True)
            return True
    except Exception as exc:
        LOG.warning("Laadmodus teruglezen mislukt: %s", exc)
        return False

def parse_property_number(output, property_id):
    """Read only the requested property's value, never numbers in metadata."""
    for line in output.splitlines():
        if not re.match(rf"^\s*{re.escape(property_id)}\b", line):
            continue
        columns = line.split("=", 1)[1].split() if "=" in line else line.split()[2:]
        if not columns:
            continue
        try:
            value = Decimal(columns[0].rstrip("%"))
        except InvalidOperation:
            continue
        if value.is_finite():
            return value
    return None


def parse_comfort_power(output):
    """Read the comfort level property in watts from the get table."""
    watts = parse_property_number(output, "3280_3")
    if watts is None or not 1350 <= watts <= 11000:
        return None
    return format((watts / 1000).normalize(), "f")


def parse_socket_state(output, socket_number, override=False):
    labels = ({"follow profile": "OFF", "direct start (override the profile)": "ON"}
              if override else {"in service": "ON", "out of service": "OFF"})
    match = re.search(rf"^\s*Socket\s+{int(socket_number)}\s+(.+?)\s*$", output, re.MULTILINE)
    return labels.get(match.group(1)) if match else None


def publish_switch_readback(client, cfg, override=False):
    name = "charging_profile_override" if override else "socket"
    command = ("direct-start", "show") if override else ("socket", "show", str(cfg["socket_number"]))
    try:
        with ALFEN_LOCK:
            value = parse_socket_state(run_alfen(cfg, *command, timeout=20), cfg["socket_number"], override)
            if value is None:
                LOG.warning("Kon %s niet uit alfenctl lezen", name)
                return False
            client.publish(f"{cfg['mqtt_topic_prefix'].strip('/')}/state/{name}", value, qos=1, retain=True)
            return True
    except Exception as exc:
        LOG.warning("%s teruglezen mislukt: %s", name, exc)
        return False


def publish_socket_readback(client, cfg):
    return publish_switch_readback(client, cfg)


def publish_override_readback(client, cfg):
    return publish_switch_readback(client, cfg, override=True)


def socket_entries(status, socket_number):
    """Yield the status entries of the selected socket, ignoring malformed ones."""
    sockets = status.get("sockets", []) if isinstance(status, dict) else []
    for socket in sockets:
        try:
            if isinstance(socket, dict) and int(socket.get("socket", 0)) == int(socket_number):
                yield socket
        except (TypeError, ValueError):
            continue

def needs_rfid_readback(status, socket_number):
    """Whether the selected socket currently reports a card authorization."""
    for socket in socket_entries(status, socket_number):
        states = {str(socket.get(key, "")).strip().lower() for key in ("state", "display")}
        if states & {"nfc authorised", "nfc authorized", "authorised", "authorized"}:
            return True
    return False

def socket_has_vehicle(status, socket_number):
    """Recognise a new plug-in even when the short NFC state was missed."""
    for socket in socket_entries(status, socket_number):
        mode3 = str(socket.get("mode3", "")).strip().upper()
        if mode3:
            return mode3[0] in "BCD"
        states = {str(socket.get(key, "")).strip().lower() for key in ("state", "display")}
        return bool(states & {"nfc authorised", "nfc authorized", "authorised", "authorized",
                              "charging power on", "vehicle connected"})
    return False

def transaction_records(output, socket_number):
    try:
        records = json.loads(output)
    except json.JSONDecodeError:
        return []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)
            and record.get("socket") == int(socket_number)]

def latest_transaction_offset(output, socket_number):
    offsets = [record.get("offset") for record in transaction_records(output, socket_number)]
    return max((value for value in offsets if isinstance(value, int)), default=None)

def session_rfid_id(output, socket_number, connected_at, baseline_offset):
    """Select only a card recorded for this plug-in, never yesterday's latest card."""
    for record in reversed(transaction_records(output, socket_number)):
        tag = record.get("start_tag")
        if not isinstance(tag, str) or not tag.strip():
            continue
        offset = record.get("offset")
        if baseline_offset is not None and (not isinstance(offset, int) or offset <= baseline_offset):
            continue
        raw_time = record.get("start_time")
        if raw_time:
            try:
                started = datetime.fromisoformat(raw_time)
                # Alfen's local timestamps may be naive while the add-on uses UTC.
                # The transaction offset is the reliable boundary in that case.
                if started.tzinfo is not None and started < connected_at - timedelta(seconds=90):
                    continue
            except (TypeError, ValueError):
                continue
        if baseline_offset is None and (not raw_time or started.tzinfo is None):
            continue
        return tag.strip()
    return None

def parse_latest_rfid_id(output, socket_number):
    """Read the latest authorized card ID recorded for the selected socket."""
    try:
        records = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(records, list):
        return None
    target_socket = int(socket_number)
    for record in reversed(records):
        if not isinstance(record, dict) or record.get("socket") != target_socket:
            continue
        tag = record.get("start_tag")
        if isinstance(tag, str) and tag.strip():
            return tag.strip()
    return None

def _clear_rfid_id_pulse(client, state_topic, timer):
    with RFID_PULSE_LOCK:
        active = RFID_PULSE_TIMERS.get(state_topic)
        if active is None or active[0] is not timer:
            return
        del RFID_PULSE_TIMERS[state_topic]
        client.publish(state_topic, "", qos=1, retain=True)

def publish_rfid_id_pulse(client, cfg, tag):
    """Publish a short-lived RFID ID and clear it two seconds later."""
    state_topic = f"{cfg['mqtt_topic_prefix'].strip('/')}/state/rfid_id_pulse"
    with RFID_PULSE_LOCK:
        previous = RFID_PULSE_TIMERS.pop(state_topic, None)
        if previous is not None:
            previous[1].cancel()
        client.publish(state_topic, tag, qos=1, retain=True)
        token = object()
        timer = threading.Timer(2.0, _clear_rfid_id_pulse, args=(client, state_topic, token))
        timer.daemon = True
        RFID_PULSE_TIMERS[state_topic] = (token, timer)
        timer.start()

def normalise_uid(value):
    """Remove UID separators and standardise hexadecimal identifiers."""
    return re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()

def uid_vehicle_map(cfg):
    """Read the add-on's JSON UID-to-evcc-vehicle mapping."""
    raw = cfg.get("uid_vehicle_map", "{}")
    mapping = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(mapping, dict):
        raise TypeError("uid_vehicle_map must be a JSON object")
    result = {}
    for uid, vehicle in mapping.items():
        key = normalise_uid(uid)
        name = str(vehicle).strip()
        if key and name:
            result[key] = name
    return result

def assign_evcc_vehicle(cfg, tag):
    """Map one Alfen RFID UID to an EVCC vehicle and assign it to the loadpoint."""
    base_url = str(cfg.get("evcc_base_url", "")).strip().rstrip("/")
    if not base_url:
        return False
    uid = normalise_uid(tag)
    vehicle = uid_vehicle_map(cfg).get(uid)
    if not vehicle:
        LOG.info("RFID-ID heeft geen evcc-voertuigkoppeling")
        return False

    loadpoint = int(cfg.get("evcc_loadpoint_id", 1))
    path = f"/api/loadpoints/{loadpoint}/vehicle/{quote(vehicle, safe='')}"
    url = f"{base_url}{path}"
    for attempt, delay in enumerate((0,) + EVCC_RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        try:
            response = requests.post(url, timeout=10)
            response.raise_for_status()
            LOG.info("evcc-laadpunt %s ingesteld op voertuig %s", loadpoint, vehicle)
            return True
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if 400 <= status_code < 500 and status_code != 429:
                LOG.error("evcc wees de aanroep af (HTTP %s); controleer voertuignaam en laadpunt", status_code)
                return False
            if attempt < len(EVCC_RETRY_DELAYS):
                LOG.warning("evcc-aanroep mislukt: %s (opnieuw proberen)", exc)
            else:
                LOG.error("evcc-aanroep mislukt; retries opgebruikt: %s", exc)
        except Exception as exc:
            if attempt < len(EVCC_RETRY_DELAYS):
                LOG.warning("evcc-aanroep mislukt: %s (opnieuw proberen)", exc)
            else:
                LOG.error("evcc-aanroep mislukt; retries opgebruikt: %s", exc)
    return False

def publish_latest_rfid_id(client, cfg, connected_at=None, baseline_offset=None):
    """Fetch and publish the latest card recorded on the selected socket."""
    try:
        with ALFEN_LOCK:
            output = run_alfen(cfg, "transactions", "--json", "--since", "today",
                               "--socket", str(cfg["socket_number"]), timeout=60)
            tag = (session_rfid_id(output, cfg["socket_number"], connected_at, baseline_offset)
                   if connected_at else parse_latest_rfid_id(output, cfg["socket_number"]))
    except Exception as exc:
        LOG.warning("RFID-ID uitlezen mislukt: %s", exc)
        return False
    if tag is None:
        LOG.info("Geen RFID-ID gevonden in de laadtransacties")
        return False
    root = cfg["mqtt_topic_prefix"].strip("/")
    client.publish(f"{root}/state/rfid_id", tag, qos=1, retain=True)
    publish_rfid_id_pulse(client, cfg, tag)
    LOG.info("RFID-ID van de geautoriseerde laadpas gepubliceerd")
    assign_evcc_vehicle(cfg, tag)
    return True

def read_transaction_offset(cfg):
    """Remember the last log record while unplugged for firmware without timestamps."""
    try:
        with ALFEN_LOCK:
            output = run_alfen(cfg, "transactions", "--json", "--since", "today",
                               "--socket", str(cfg["socket_number"]), timeout=60)
        offset = latest_transaction_offset(output, cfg["socket_number"])
        return offset if offset is not None else -1
    except Exception as exc:
        LOG.warning("RFID-transactiebasis uitlezen mislukt: %s", exc)
        return None

def publish_comfort_power_readback(client, cfg):
    try:
        with ALFEN_LOCK:
            output = run_alfen(cfg, "get", "3280_3", timeout=20)
            value = parse_comfort_power(output)
            if value is None:
                LOG.warning("Kon het comfortvermogen niet uit alfenctl lezen")
                return False
            root = cfg["mqtt_topic_prefix"].strip("/")
            client.publish(f"{root}/state/comfort_power", value, qos=1, retain=True)
            return True
    except Exception as exc:
        LOG.warning("Comfortvermogen teruglezen mislukt: %s", exc)
        return False

def parse_green_share(output):
    """Read the Alfen green-share value from its get table or set output."""
    value = parse_property_number(output, "3280_2")
    if value is not None and value == value.to_integral_value() and 0 <= value <= 100:
        return str(int(value))
    return None


def publish_green_share_readback(client, cfg):
    try:
        with ALFEN_LOCK:
            output = run_alfen(cfg, "get", "3280_2", timeout=20)
            value = parse_green_share(output)
            if value is None:
                LOG.warning("Kon het green-sharepercentage niet uit alfenctl lezen")
                return False
            root = cfg["mqtt_topic_prefix"].strip("/")
            client.publish(f"{root}/state/green_share", value, qos=1, retain=True)
            return True
    except Exception as exc:
        LOG.warning("Green share teruglezen mislukt: %s", exc)
        return False

def supervisor_mqtt(cfg):
    """Use the Supervisor's MQTT service when no broker credentials were configured."""
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token or cfg.get("mqtt_username") or cfg["mqtt_host"] != "core-mosquitto":
        return cfg
    try:
        response = requests.get("http://supervisor/services/mqtt", timeout=10,
                                headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        data = response.json()["data"]
        LOG.info("MQTT-gegevens opgehaald via de Supervisor")
        return {**cfg, "mqtt_host": data["host"], "mqtt_port": data["port"],
                "mqtt_username": data.get("username", ""), "mqtt_password": data.get("password", "")}
    except Exception as exc:
        LOG.warning("MQTT-service van Supervisor niet beschikbaar (%s); instellingen worden gebruikt", exc)
        return cfg

class PollState:
    """Plug-in/RFID tracking that must survive transient charger or broker errors."""
    def __init__(self):
        self.session_active = False
        self.rfid_handled = False
        self.rfid_deadline = 0
        self.next_rfid_try = 0
        self.connected_at = None
        self.baseline_offset = None
        self.baseline_needed = True
        self.polls = 0

def poll_once(client, cfg, poll):
    status = run_status(cfg)
    publish_discovery(client.ace_cache, cfg, status)
    active = socket_has_vehicle(status, cfg["socket_number"])
    now = time.monotonic()
    if active and not poll.session_active:
        poll.connected_at = datetime.now().astimezone()
        poll.rfid_deadline = now + RFID_SEARCH_SECONDS
        poll.next_rfid_try = 0
        poll.rfid_handled = False
        poll.baseline_needed = True
    elif not active and poll.session_active:
        poll.baseline_needed = True
    poll.session_active = active
    if active and not poll.rfid_handled and now < poll.rfid_deadline and now >= poll.next_rfid_try:
        poll.rfid_handled = publish_latest_rfid_id(client, cfg, poll.connected_at, poll.baseline_offset)
        poll.next_rfid_try = now + RFID_RETRY_SECONDS
    elif not active and poll.baseline_needed:
        poll.baseline_offset = read_transaction_offset(cfg)
        poll.baseline_needed = False
    if poll.polls % CONTROL_POLL_EVERY == 0:
        publish_current_readback(client, cfg)
        publish_solar_mode_readback(client, cfg)
        publish_comfort_power_readback(client, cfg)
        publish_green_share_readback(client, cfg)
        publish_socket_readback(client, cfg)
        publish_override_readback(client, cfg)
    poll.polls += 1
    client.publish(f"{cfg['mqtt_topic_prefix'].strip('/')}/availability", "online", qos=1, retain=True)

def publish_offline(client, cfg):
    try:
        info = client.publish(f"{cfg['mqtt_topic_prefix'].strip('/')}/availability", "offline", qos=1, retain=True)
        info.wait_for_publish(timeout=2)
    except Exception:
        pass

def stop_handler(_signum, _frame):
    STOP.set()

def main():
    cfg = options()
    validate_options(cfg)
    cfg = supervisor_mqtt(cfg)
    write_alfen_config(cfg)
    # Home Assistant stops add-ons with SIGTERM; as PID 1 we must handle it explicitly.
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    client = None
    poll = PollState()
    failures = 0
    while not STOP.is_set():
        try:
            if client is None:
                client = connect_mqtt(cfg)
                LOG.info("Verbonden met MQTT; Alfen status wordt opgehaald van %s", cfg["charger_host"])
            poll_once(client, cfg, poll)
            if failures:
                LOG.info("Uitlezen hersteld")
            failures = 0
            delay = int(cfg["poll_interval"])
        except Exception as exc:
            failures += 1
            LOG.error("Uitlezen/publiceren mislukt: %s", exc)
            if client:
                publish_offline(client, cfg)
            # Keep MQTT and the session state; only back off from the failing side.
            delay = min(MAX_BACKOFF_SECONDS, 10 * 2 ** min(failures - 1, 3))
        STOP.wait(delay)
    if client:
        publish_offline(client, cfg)
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    main()
