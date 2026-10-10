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
    "mqtt_topic_prefix": "ace2mqtt", "discovery_prefix": "homeassistant", "comfort_power_max_kw": 11.0,
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
    def test_solar_mode_translates_known_modes_to_cli_integers(self, run):
        for payload, mode, value in ((" off ", "off", "0"), ("comfort", "comfort", "1"), ("GREEN", "green", "2")):
            with self.subTest(mode=mode):
                self.assertEqual(app.handle_command(CFG, "ace2mqtt/control/solar_mode", payload), mode)
                run.assert_called_once_with(CFG, "lb", "set", "--solar-mode", value)
                run.reset_mock()
        self.assertIsNone(app.handle_command(CFG, "ace2mqtt/control/solar_mode", "disabled"))
        run.assert_not_called()
        self.assertIsNone(app.handle_command(CFG, "ace2mqtt/control/solar_mode", "comfort", retained=True))
        run.assert_not_called()

    @patch("app.run_alfen")
    def test_comfort_power_converts_kw_to_watts_and_validates_step(self, run):
        self.assertEqual(app.handle_command(CFG, "ace2mqtt/control/comfort_power", "4.20"), "4.2")
        run.assert_called_once_with(CFG, "lb", "set", "--comfort-level", "4200")
        run.reset_mock()
        for value in ("1.34", "22.05", "4.23", "nan", "4.2 kW"):
            with self.subTest(value=value):
                self.assertIsNone(app.handle_command(CFG, "ace2mqtt/control/comfort_power", value))
        run.assert_not_called()

    def test_comfort_power_command_publishes_confirmed_state(self):
        client = Mock()
        message = types.SimpleNamespace(topic="ace2mqtt/control/comfort_power", payload=b"4.2", retain=False)
        with patch("app.run_alfen", side_effect=["", "3280_3 solarComfortW 4200 rw Comfort level"]) as run:
            app.on_message(client, CFG, message)
        self.assertEqual(run.call_args_list[0].args, (CFG, "lb", "set", "--comfort-level", "4200"))
        run.assert_called_with(CFG, "get", "3280_3", timeout=20)
        client.publish.assert_called_once_with("ace2mqtt/state/comfort_power", "4.2", qos=1, retain=True)

    def test_solar_mode_command_publishes_confirmed_state(self):
        client = Mock()
        message = types.SimpleNamespace(topic="ace2mqtt/control/solar_mode", payload=b"green", retain=False)
        with patch("app.run_alfen", side_effect=["", "Solar charging green"]):
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


class EvccBridgeTests(unittest.TestCase):
    EVCC_CFG = {
        **CFG,
        "evcc_base_url": "http://evcc.local:7070/",
        "evcc_loadpoint_id": 2,
        "uid_vehicle_map": '{"04AABBCCDDEEFF":"bmw x/130e"}',
    }

    def test_uid_normalization_matches_case_and_separators(self):
        self.assertEqual(app.normalise_uid("04:aa-bb cc:dd-ee-ff"), "04AABBCCDDEEFF")
        self.assertEqual(app.uid_vehicle_map(self.EVCC_CFG), {"04AABBCCDDEEFF": "bmw x/130e"})

    @patch("app.requests.post")
    def test_known_uid_posts_encoded_evcc_vehicle_path(self, post):
        post.return_value.raise_for_status.return_value = None
        self.assertTrue(app.assign_evcc_vehicle(self.EVCC_CFG, "04:aa:bb:cc:dd:ee:ff"))
        post.assert_called_once_with(
            "http://evcc.local:7070/api/loadpoints/2/vehicle/bmw%20x%2F130e", timeout=10
        )

    @patch("app.requests.post")
    def test_unknown_uid_does_not_call_evcc(self, post):
        self.assertFalse(app.assign_evcc_vehicle(self.EVCC_CFG, "11223344"))
        post.assert_not_called()

    @patch("app.requests.post")
    def test_evcc_is_disabled_when_base_url_is_empty(self, post):
        self.assertFalse(app.assign_evcc_vehicle({**CFG, "uid_vehicle_map": self.EVCC_CFG["uid_vehicle_map"]},
                                                 "04AABBCCDDEEFF"))
        post.assert_not_called()

    @patch("app.time.sleep")
    @patch("app.requests.post", side_effect=app.requests.RequestException("offline"))
    def test_evcc_failure_retries_with_bounded_backoff(self, post, sleep):
        self.assertFalse(app.assign_evcc_vehicle(self.EVCC_CFG, "04AABBCCDDEEFF"))
        self.assertEqual(post.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 3, 5])

    @patch("app.threading.Timer")
    @patch("app.assign_evcc_vehicle")
    @patch("app.run_alfen", return_value='[{"socket": 2, "start_tag": "04:AABBCCDDEEFF"}]')
    def test_rfid_readback_keeps_mqtt_publication_and_calls_evcc(self, run, assign, timer):
        client = Mock()
        self.assertTrue(app.publish_latest_rfid_id(client, CFG))
        client.publish.assert_any_call(
            "ace2mqtt/state/rfid_id", "04:AABBCCDDEEFF", qos=1, retain=True
        )
        assign.assert_called_once_with(CFG, "04:AABBCCDDEEFF")

    def test_invalid_evcc_map_is_rejected(self):
        with self.assertRaises(ValueError):
            app.validate_options({**CFG, "uid_vehicle_map": '["not", "an object"]'})


class DiscoveryTests(unittest.TestCase):
    def test_control_discovery_creates_buttons_and_numbers(self):
        client = Mock()
        app.publish_control_discovery(client, CFG, "ace2mqtt_192_0_2_10", {"identifiers": ["x"]})
        configs = {c.args[0]: json.loads(c.args[1]) for c in client.publish.call_args_list if c.args[1]}
        def config(component, name):
            return configs[f"homeassistant/{component}/ace2mqtt_192_0_2_10/{name}/config"]
        self.assertEqual(len(configs), 8)
        for name in ("socket", "charging_profile_override"):
            self.assertEqual(config("switch", name)["command_topic"], f"ace2mqtt/control/{name}")
        number_config = config("number", "current_limit")
        self.assertEqual(number_config["command_topic"], "ace2mqtt/control/current")
        self.assertEqual(number_config["state_topic"], "ace2mqtt/state/current_limit")
        self.assertEqual(number_config["min"], 6)
        self.assertFalse(number_config["optimistic"])
        comfort_config = config("number", "comfort_power")
        self.assertEqual(comfort_config["command_topic"], "ace2mqtt/control/comfort_power")
        self.assertEqual(comfort_config["unit_of_measurement"], "kW")
        self.assertEqual(comfort_config["step"], 0.05)
        select_config = json.loads(client.publish.call_args_list[-1].args[1])
        self.assertEqual(client.publish.call_args_list[-1].args[0],
                         "homeassistant/select/ace2mqtt_192_0_2_10/solar_mode/config")
        self.assertEqual(select_config["options"], ["off", "comfort", "green"])
        self.assertEqual(select_config["state_topic"], "ace2mqtt/state/solar_mode")

    def test_current_readback_parses_selected_socket_only(self):
        text = "Charging current limits:\n  Socket 1 maximum 16 A\n  Socket 2 maximum 8 A\n"
        self.assertEqual(app.parse_current_limit(text, 2), "8")
        self.assertIsNone(app.parse_current_limit(text, 3))

    def test_solar_mode_readback_accepts_only_known_modes(self):
        self.assertEqual(app.parse_solar_mode("  Solar charging      off\n    green share       25%\n    comfort level     2000 W\n    boost socket 1    off\n"), "off")
        self.assertEqual(app.parse_solar_mode("Load balancing:\n  Solar charging     comfort\n"), "comfort")
        self.assertEqual(app.parse_solar_mode("  Solar charging green  \n"), "green")
        self.assertIsNone(app.parse_solar_mode("Solar charging disabled"))

    def test_solar_mode_off_is_published_and_in_discovery(self):
        client = Mock()
        with patch("app.run_alfen", return_value="  Solar charging      off\n    green share       25%\n"):
            self.assertTrue(app.publish_solar_mode_readback(client, CFG))
        client.publish.assert_called_once_with("ace2mqtt/state/solar_mode", "off", qos=1, retain=True)
        client.reset_mock()
        app.publish_control_discovery(client, CFG, "test", {})
        configs = {call.args[0]: json.loads(call.args[1]) for call in client.publish.call_args_list if call.args[1]}
        self.assertEqual(configs["homeassistant/select/test/solar_mode/config"]["options"], ["off", "comfort", "green"])

    def test_solar_mode_readback_publishes_reported_value(self):
        client = Mock()
        with patch("app.run_alfen", return_value="Solar charging     comfort\n") as run:
            self.assertTrue(app.publish_solar_mode_readback(client, CFG))
        run.assert_called_once_with(CFG, "lb", timeout=20)
        client.publish.assert_called_once_with("ace2mqtt/state/solar_mode", "comfort", qos=1, retain=True)

    def test_comfort_power_readback_parses_watts_and_publishes_kw(self):
        self.assertEqual(app.parse_comfort_power("3280_3 (Solar Comfort Level) = 4200 W"), "4.2")
        self.assertEqual(app.parse_comfort_power("3280_3 = 4225 W"), "4.225")
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
