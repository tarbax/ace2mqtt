import json
import sys
import types
import tomllib
import unittest
from unittest.mock import Mock, mock_open, patch

# The runtime dependency is pinned in requirements.txt; keep unit tests runnable
# in a lightweight checkout where paho-mqtt has not been installed.
try:
    import paho.mqtt.client  # noqa: F401
except ModuleNotFoundError:
    paho = types.ModuleType("paho")
    mqtt = types.ModuleType("paho.mqtt")
    client_module = types.ModuleType("paho.mqtt.client")
    client_module.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    client_module.Client = object
    mqtt.client = client_module
    paho.mqtt = mqtt
    sys.modules.update({"paho": paho, "paho.mqtt": mqtt, "paho.mqtt.client": client_module})

import app


CFG = {
    "charger_host": "192.0.2.10", "charger_port": 443,
    "socket_number": 2, "current_min": 6, "current_max": 32,
    "mqtt_topic_prefix": "ace2mqtt", "discovery_prefix": "homeassistant",
}


class AlfenCommandTests(unittest.TestCase):
    def test_builds_station_command_with_port_and_no_shell(self):
        command = app.alfen_command(CFG, "current", "set", "16", "--socket", "2")
        self.assertEqual(command, ["alfenctl", "current", "set", "16", "--socket", "2",
                                   "--config", "/data/alfen.toml", "--port", "443",
                                   "--station", "ace2mqtt"])
        self.assertNotIsInstance(command, str)

    @patch("app.run_alfen")
    def test_fixed_controls_map_to_documented_actions(self, run):
        actions = {
            "socket_enable": ("socket", "enable", "2"),
            "socket_disable": ("socket", "disable", "2", "--yes"),
            "direct_start_on": ("direct-start", "on", "--socket", "2"),
            "direct_start_off": ("direct-start", "off", "--socket", "2"),
        }
        for name, command in actions.items():
            with self.subTest(name=name):
                app.handle_command(CFG, f"ace2mqtt/control/{name}", "PRESS")
                run.assert_called_with(CFG, *command)
                run.reset_mock()

    @patch("app.run_alfen")
    def test_current_value_is_bounded_and_numeric(self, run):
        app.handle_command(CFG, "ace2mqtt/control/current", "16")
        run.assert_called_once_with(CFG, "current", "set", "16", "--socket", "2")
        run.reset_mock()
        for value in ("5", "33", "nan", "16.5", "current set 16"):
            with self.subTest(value=value):
                app.handle_command(CFG, "ace2mqtt/control/current", value)
                run.assert_not_called()

    @patch("app.run_alfen")
    def test_retained_or_invalid_button_commands_are_ignored(self, run):
        app.handle_command(CFG, "ace2mqtt/control/socket_disable", "PRESS", retained=True)
        app.handle_command(CFG, "ace2mqtt/control/socket_disable", "anything")
        app.handle_command(CFG, "ace2mqtt/control/unknown", "PRESS")
        run.assert_not_called()

    def test_current_accepts_integral_float_payload_from_number_widgets(self):
        with patch("app.run_alfen") as run:
            app.handle_command(CFG, "ace2mqtt/control/current", "16.0")
        run.assert_called_once_with(CFG, "current", "set", "16", "--socket", "2")

    @patch("app.run_alfen")
    def test_solar_mode_only_allows_comfort_or_green(self, run):
        self.assertEqual(app.handle_command(CFG, "ace2mqtt/control/solar_mode", "GREEN"), "green")
        run.assert_called_once_with(CFG, "lb", "set", "--solar-mode", "green")
        run.reset_mock()
        self.assertEqual(app.handle_command(CFG, "ace2mqtt/control/solar_mode", "off"), None)
        run.assert_not_called()
        self.assertIsNone(app.handle_command(CFG, "ace2mqtt/control/solar_mode", "comfort", retained=True))
        run.assert_not_called()

    @patch("app.run_alfen")
    def test_comfort_power_converts_kw_to_watts_and_validates_step(self, run):
        self.assertEqual(app.handle_command(CFG, "ace2mqtt/control/comfort_power", "4.20"), "4.2")
        run.assert_called_once_with(CFG, "set", "3280_3", "4200")
        run.reset_mock()
        for value in ("1.34", "22.05", "4.23", "nan", "4.2 kW"):
            with self.subTest(value=value):
                self.assertIsNone(app.handle_command(CFG, "ace2mqtt/control/comfort_power", value))
        run.assert_not_called()

    def test_comfort_power_command_publishes_confirmed_state(self):
        client = Mock()
        message = types.SimpleNamespace(topic="ace2mqtt/control/comfort_power", payload=b"4.2", retain=False)
        with patch("app.run_alfen") as run:
            app.on_message(client, CFG, message)
        run.assert_called_once_with(CFG, "set", "3280_3", "4200")
        client.publish.assert_called_once_with("ace2mqtt/state/comfort_power", "4.2", qos=1, retain=True)

    def test_solar_mode_command_publishes_confirmed_state(self):
        client = Mock()
        message = types.SimpleNamespace(topic="ace2mqtt/control/solar_mode", payload=b"green", retain=False)
        with patch("app.run_alfen"):
            app.on_message(client, CFG, message)
        client.publish.assert_called_once_with("ace2mqtt/state/solar_mode", "green", qos=1, retain=True)

    def test_invalid_topic_prefix_is_rejected(self):
        bad_cfg = {**CFG, "mqtt_topic_prefix": "bad/+"}
        with self.assertRaises(ValueError):
            app.validate_options(bad_cfg)

    def test_status_json_is_decoded_from_cli_result(self):
        with patch("app.run_alfen", return_value='{"socket1": {"state": "available"}}') as run:
            result = app.run_status(CFG)
        self.assertEqual(result, {"socket1": {"state": "available"}})
        run.assert_called_once_with(CFG, "status", "--json", timeout=20)

    def test_charger_toml_escapes_credentials_and_uses_station_table(self):
        writer = mock_open()
        cfg = {**CFG, "charger_username": "user", "charger_password": 'a"b\\c', "charger_http": False}
        with patch("builtins.open", writer), patch("app.os.chmod") as chmod:
            app.write_alfen_config(cfg)
        text = "".join(call.args[0] for call in writer().write.call_args_list)
        parsed = tomllib.loads(text)
        self.assertEqual(parsed["password"], 'a"b\\c')
        self.assertEqual(parsed["stations"]["ace2mqtt"]["host"], CFG["charger_host"])
        self.assertFalse(parsed["stations"]["ace2mqtt"]["http"])
        chmod.assert_called_once_with("/data/alfen.toml", 0o600)


class DiscoveryTests(unittest.TestCase):
    def test_control_discovery_creates_buttons_and_numbers(self):
        client = Mock()
        app.publish_control_discovery(client, CFG, "ace2mqtt_192_0_2_10", {"identifiers": ["x"]})
        self.assertEqual(client.publish.call_count, 7)
        first = client.publish.call_args_list[0].args
        data = json.loads(first[1])
        self.assertEqual(first[0], "homeassistant/button/ace2mqtt_192_0_2_10/socket_enable/config")
        self.assertEqual(data["command_topic"], "ace2mqtt/control/socket_enable")
        self.assertEqual(data["payload_press"], "PRESS")
        number_config = json.loads(client.publish.call_args_list[-3].args[1])
        self.assertEqual(number_config["command_topic"], "ace2mqtt/control/current")
        self.assertEqual(number_config["state_topic"], "ace2mqtt/state/current_limit")
        self.assertEqual(number_config["min"], 6)
        self.assertFalse(number_config["optimistic"])
        comfort_config = json.loads(client.publish.call_args_list[-2].args[1])
        self.assertEqual(comfort_config["command_topic"], "ace2mqtt/control/comfort_power")
        self.assertEqual(comfort_config["unit_of_measurement"], "kW")
        self.assertEqual(comfort_config["step"], 0.05)
        select_config = json.loads(client.publish.call_args_list[-1].args[1])
        self.assertEqual(client.publish.call_args_list[-1].args[0],
                         "homeassistant/select/ace2mqtt_192_0_2_10/solar_mode/config")
        self.assertEqual(select_config["options"], ["comfort", "green"])
        self.assertEqual(select_config["state_topic"], "ace2mqtt/state/solar_mode")

    def test_current_readback_parses_selected_socket_only(self):
        text = "Charging current limits:\n  Socket 1 maximum 16 A\n  Socket 2 maximum 8 A\n"
        self.assertEqual(app.parse_current_limit(text, 2), "8")
        self.assertIsNone(app.parse_current_limit(text, 3))

    def test_solar_mode_readback_accepts_only_known_modes(self):
        self.assertEqual(app.parse_solar_mode("Load balancing:\n  Solar charging     comfort\n"), "comfort")
        self.assertEqual(app.parse_solar_mode("  Solar charging green  \n"), "green")
        self.assertIsNone(app.parse_solar_mode("Solar charging disabled"))

    def test_solar_mode_readback_publishes_reported_value(self):
        client = Mock()
        with patch("app.run_alfen", return_value="Solar charging     comfort\n") as run:
            self.assertTrue(app.publish_solar_mode_readback(client, CFG))
        run.assert_called_once_with(CFG, "lb", timeout=20)
        client.publish.assert_called_once_with("ace2mqtt/state/solar_mode", "comfort", qos=1, retain=True)

    def test_comfort_power_readback_parses_watts_and_publishes_kw(self):
        self.assertEqual(app.parse_comfort_power("3280_3 (Solar Comfort Level) = 4200 W"), "4.2")
        self.assertIsNone(app.parse_comfort_power("3280_3 = 4225 W"))
        client = Mock()
        with patch("app.run_alfen", return_value="3280_3 = 4200 W") as run:
            self.assertTrue(app.publish_comfort_power_readback(client, CFG))
        run.assert_called_once_with(CFG, "get", "3280_3", timeout=20)
        client.publish.assert_called_once_with("ace2mqtt/state/comfort_power", "4.2", qos=1, retain=True)

    def test_flatten_skips_non_finite_numbers(self):
        self.assertEqual(app.flatten({"socket": {"amps": 12.5, "unknown": float("nan")}, "active": True}),
                         {"socket_amps": 12.5, "active": True})


if __name__ == "__main__":
    unittest.main()
