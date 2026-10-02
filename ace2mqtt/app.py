#!/usr/bin/env python3
"""Poll and control an Alfen charger through MQTT + Home Assistant Discovery."""
import json
import logging
import math
import os
import re
import subprocess
import threading
import time
from decimal import Decimal, InvalidOperation

import paho.mqtt.client as mqtt

LOG = logging.getLogger("ace2mqtt")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
ALFEN_LOCK = threading.RLock()

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
    if int(cfg["current_min"]) > int(cfg["current_max"]):
        raise ValueError("current_min moet kleiner dan of gelijk zijn aan current_max")
    for key in ("mqtt_topic_prefix", "discovery_prefix"):
        value = str(cfg[key]).strip("/")
        if not value or any(char in value for char in ("+", "#", " ")):
            raise ValueError(f"Instelling '{key}' bevat een ongeldig MQTT-topicdeel")

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
    with open("/data/alfen.toml", "w", encoding="utf-8") as f:
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
    return result.stdout

def run_status(cfg):
    try:
        return json.loads(run_alfen(cfg, "status", "--json", timeout=20))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"alfenctl gaf geen geldige JSON-status: {exc}") from exc

def discovery_button(client, cfg, device_id, device, key, name):
    root = cfg["mqtt_topic_prefix"].strip("/")
    command_topic = f"{root}/control/{key}"
    topic = f"{cfg['discovery_prefix'].strip('/')}/button/{device_id}/{key}/config"
    config = {"name": name, "unique_id": f"{device_id}_{key}", "command_topic": command_topic,
              "payload_press": "PRESS", "device": device,
              "availability_topic": f"{root}/availability",
              "payload_available": "online", "payload_not_available": "offline"}
    client.publish(topic, json.dumps(config, ensure_ascii=False), qos=1, retain=True)

def publish_control_discovery(client, cfg, device_id, device):
    socket = int(cfg["socket_number"])
    buttons = (
        ("socket_enable", f"Socket {socket} inschakelen"),
        ("socket_disable", f"Socket {socket} uitschakelen (kan laden stoppen)"),
        ("direct_start_on", "Laadprofieloverride inschakelen"),
        ("direct_start_off", "Laadprofieloverride uitschakelen"),
    )
    for key, name in buttons:
        discovery_button(client, cfg, device_id, device, key, name)
    root = cfg["mqtt_topic_prefix"].strip("/")
    number_topic = f"{cfg['discovery_prefix'].strip('/')}/number/{device_id}/current_limit/config"
    number = {"name": f"Laadstroom socket {socket}", "unique_id": f"{device_id}_current_limit",
              "command_topic": f"{root}/control/current", "state_topic": f"{root}/state/current_limit",
              "min": int(cfg["current_min"]),
              "max": int(cfg["current_max"]), "step": 1, "unit_of_measurement": "A",
              "mode": "slider", "optimistic": False, "retain": False, "device": device,
              "availability_topic": f"{root}/availability", "payload_available": "online",
              "payload_not_available": "offline"}
    client.publish(number_topic, json.dumps(number, ensure_ascii=False), qos=1, retain=True)
    comfort_topic = f"{cfg['discovery_prefix'].strip('/')}/number/{device_id}/comfort_power/config"
    comfort_number = {
        "name": "Comfort laadvermogen",
        "unique_id": f"{device_id}_comfort_power",
        "command_topic": f"{root}/control/comfort_power",
        "state_topic": f"{root}/state/comfort_power",
        "min": 1.35, "max": 22.0, "step": 0.05,
        "unit_of_measurement": "kW", "mode": "slider",
        "optimistic": False, "retain": False, "device": device,
        "availability_topic": f"{root}/availability",
        "payload_available": "online", "payload_not_available": "offline",
    }
    client.publish(comfort_topic, json.dumps(comfort_number, ensure_ascii=False), qos=1, retain=True)
    solar_topic = f"{cfg['discovery_prefix'].strip('/')}/select/{device_id}/solar_mode/config"
    solar_select = {
        "name": "Laadmodus",
        "unique_id": f"{device_id}_solar_mode",
        "command_topic": f"{root}/control/solar_mode",
        "state_topic": f"{root}/state/solar_mode",
        "options": ["comfort", "green"],
        "optimistic": False,
        "retain": False,
        "device": device,
        "availability_topic": f"{root}/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
    }
    client.publish(solar_topic, json.dumps(solar_select, ensure_ascii=False), qos=1, retain=True)

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
        if not int(cfg["current_min"]) <= amps <= int(cfg["current_max"]):
            LOG.warning("Laadstroom buiten ingestelde grens (%s-%s A)", cfg["current_min"], cfg["current_max"])
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
        if not kw.is_finite() or kw < Decimal("1.35") or kw > Decimal("22.00") or (kw * 1000) % 50:
            LOG.warning("Comfortvermogen moet 1,35-22,00 kW zijn in stappen van 0,05 kW")
            return
        watts = int(kw * 1000)
        run_alfen(cfg, "set", "3280_3", str(watts))
        LOG.info("Comfortvermogen ingesteld op %s kW", kw)
        return format(kw.normalize(), "f")
    if topic == f"{root}/control/solar_mode":
        mode = payload.strip().lower()
        if mode not in ("comfort", "green"):
            LOG.warning("Ongeldige laadmodus ontvangen; toegestaan: comfort of green")
            return
        run_alfen(cfg, "lb", "set", "--solar-mode", mode)
        LOG.info("Alfen-laadmodus ingesteld op %s", mode)
        return mode
    LOG.debug("Onbekend MQTT-commando genegeerd")

def subscribe_commands(client, cfg):
    root = cfg["mqtt_topic_prefix"].strip("/")
    for name in ("socket_enable", "socket_disable", "direct_start_on", "direct_start_off", "current", "comfort_power", "solar_mode"):
        client.subscribe(f"{root}/control/{name}", qos=1)

def mqtt_connected(client, cfg):
    subscribe_commands(client, cfg)

def on_message(client, cfg, message):
    try:
        payload = message.payload.decode("utf-8", errors="replace")
        current_value = handle_command(cfg, message.topic, payload, message.retain)
        if current_value is not None:
            root = cfg["mqtt_topic_prefix"].strip("/")
            state_name = {
                f"{root}/control/solar_mode": "solar_mode",
                f"{root}/control/current": "current_limit",
                f"{root}/control/comfort_power": "comfort_power",
            }[message.topic]
            client.publish(f"{root}/state/{state_name}", current_value, qos=1, retain=True)
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

def connect_mqtt(cfg):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ace2mqtt")
    username, password = cfg.get("mqtt_username"), cfg.get("mqtt_password")
    if username:
        client.username_pw_set(username, password or None)
    root = cfg["mqtt_topic_prefix"].strip("/")
    client.will_set(f"{root}/availability", "offline", qos=1, retain=True)
    client.on_connect = lambda connected_client, _userdata, _flags, _reason, _properties: mqtt_connected(
        connected_client, cfg)
    client.on_message = lambda _client, _userdata, message: on_message(_client, cfg, message)
    client.connect(cfg["mqtt_host"], int(cfg["mqtt_port"]), keepalive=60)
    client.loop_start()
    return client

def publish_discovery(client, cfg, state):
    root = cfg["mqtt_topic_prefix"].strip("/")
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
            config["suggested_display_precision"] = 2
        if value is None or isinstance(value, (dict, list)):
            continue
        topic = f"{cfg['discovery_prefix'].strip('/')}/{component}/{device_id}/{entity}/config"
        client.publish(topic, json.dumps(config, ensure_ascii=False), qos=1, retain=True)
        payload = str(value).lower() if isinstance(value, bool) else str(value)
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
    match = re.search(r"^\s*Solar charging\s+(comfort|green)\s*$", output, re.MULTILINE | re.IGNORECASE)
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

def parse_comfort_power(output):
    """Read the comfort level property, reported by alfenctl in watts."""
    match = re.search(r"(?<![\w])(\d{4,5})(?![\w])", output)
    if not match:
        return None
    watts = int(match.group(1))
    if watts < 1350 or watts > 22000 or watts % 50:
        return None
    return format(Decimal(watts) / 1000, "f")

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

def main():
    cfg = options()
    validate_options(cfg)
    write_alfen_config(cfg)
    while True:
        client = None
        try:
            client = connect_mqtt(cfg)
            current_readback_published = False
            solar_mode_readback_published = False
            comfort_power_readback_published = False
            LOG.info("Verbonden met MQTT; Alfen status wordt opgehaald van %s", cfg["charger_host"])
            while True:
                status = run_status(cfg)
                publish_discovery(client, cfg, status)
                if not current_readback_published:
                    current_readback_published = publish_current_readback(client, cfg)
                if not solar_mode_readback_published:
                    solar_mode_readback_published = publish_solar_mode_readback(client, cfg)
                if not comfort_power_readback_published:
                    comfort_power_readback_published = publish_comfort_power_readback(client, cfg)
                client.publish(f"{cfg['mqtt_topic_prefix'].strip('/')}/availability", "online", qos=1, retain=True)
                time.sleep(int(cfg["poll_interval"]))
        except KeyboardInterrupt:
            if client:
                info = client.publish(f"{cfg['mqtt_topic_prefix'].strip('/')}/availability", "offline", qos=1, retain=True)
                info.wait_for_publish(timeout=2)
                client.loop_stop()
                client.disconnect()
            break
        except Exception as exc:
            LOG.error("Uitlezen/publiceren mislukt: %s", exc)
            if client:
                try:
                    info = client.publish(f"{cfg['mqtt_topic_prefix'].strip('/')}/availability", "offline", qos=1, retain=True)
                    info.wait_for_publish(timeout=2)
                    client.loop_stop()
                    client.disconnect()
                except Exception:
                    pass
            time.sleep(10)

if __name__ == "__main__":
    main()
