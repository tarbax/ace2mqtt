import json
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import Mock, patch

from test_app import app, CFG

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

ADDON = Path(__file__).resolve().parents[1]
GROUPED = {
    "charger": {"host": "192.0.2.10", "username": "admin", "password": "pw", "http": False, "socket": 2},
    "mqtt": {"host": "broker", "port": 1883, "topic_prefix": "alfen", "discovery_prefix": "homeassistant"},
    "limits": {"current_min": 6, "current_max": 16, "comfort_power_max_kw": 4.0, "poll_interval": 15},
    "evcc": {"url": "http://evcc:7070", "loadpoint": 2, "vehicles": [{"uid": "04:AA", "vehicle": "car"}]},
    "solar_controls": False, "log_level": "debug",
}


class OptionNormalisationTests(unittest.TestCase):
    def test_grouped_options_become_flat_internal_config(self):
        cfg = app.normalize_options(GROUPED)
        self.assertEqual((cfg["charger_host"], cfg["socket_number"], cfg["mqtt_topic_prefix"]),
                         ("192.0.2.10", 2, "alfen"))
        self.assertEqual((cfg["current_max"], cfg["poll_interval"]), (16, 15))
        self.assertEqual((cfg["evcc_base_url"], cfg["evcc_loadpoint_id"]), ("http://evcc:7070", 2))
        self.assertEqual(cfg["uid_vehicle_map"], {"04:AA": "car"})
        self.assertEqual(app.uid_vehicle_map(cfg), {"04AA": "car"})
        self.assertFalse(cfg["solar_controls"])
        self.assertEqual(cfg["log_level"], "debug")
        app.validate_options(cfg)

    def test_port_follows_http_choice_unless_set(self):
        https = app.normalize_options(GROUPED)
        http = app.normalize_options({**GROUPED, "charger": {**GROUPED["charger"], "http": True}})
        custom = app.normalize_options({**GROUPED, "charger": {**GROUPED["charger"], "port": 8443}})
        self.assertEqual((https["charger_port"], http["charger_port"], custom["charger_port"]), (443, 80, 8443))

    def test_defaults_for_optional_values(self):
        cfg = app.normalize_options({"charger": {"host": "h"}})
        self.assertEqual((cfg["mqtt_username"], cfg["evcc_base_url"], cfg["log_level"]), ("", "", "info"))
        self.assertTrue(cfg["solar_controls"])
        self.assertEqual(cfg["uid_vehicle_map"], {})

    def test_flat_options_do_not_override_grouped_values(self):
        cfg = app.normalize_options({**GROUPED, "charger_host": "old-host",
                                     "current_max": 64, "uid_vehicle_map": '{"OLD": "old-car"}'})
        self.assertEqual(cfg["charger_host"], "192.0.2.10")
        self.assertEqual(cfg["current_max"], 16)
        self.assertEqual(cfg["uid_vehicle_map"], {"04:AA": "car"})

    def test_options_reads_file_and_requires_charger_host(self):
        for raw, ok in ((GROUPED, True), ({"charger": {"host": ""}}, False)):
            with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(raw))):
                if ok:
                    self.assertEqual(app.options()["charger_host"], "192.0.2.10")
                else:
                    with self.assertRaisesRegex(ValueError, r"charger\.host"):
                        app.options()

    def test_flat_only_options_require_grouped_charger_host(self):
        raw = {"charger_host": "old-host"}
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(raw))), \
                self.assertRaisesRegex(ValueError, r"charger\.host"):
            app.options()


class SolarControlTests(unittest.TestCase):
    def test_disabled_solar_controls_are_removed_and_ignored(self):
        cfg = {**CFG, "solar_controls": False}
        client = Mock()
        app.publish_control_discovery(client, cfg, "dev", {})
        published = {c.args[0]: c.args[1] for c in client.publish.call_args_list}
        for path in ("number/dev/comfort_power", "number/dev/green_share", "select/dev/solar_mode"):
            self.assertEqual(published[f"homeassistant/{path}/config"], "")
        self.assertTrue(published["homeassistant/number/dev/current_limit/config"])
        with patch("app.run_alfen") as run:
            for name, value in (("comfort_power", "4"), ("green_share", "25"), ("solar_mode", "green")):
                self.assertIsNone(app.handle_command(cfg, f"ace2mqtt/control/{name}", value))
            run.assert_not_called()

    def test_enabled_by_default(self):
        client = Mock()
        app.publish_control_discovery(client, CFG, "dev", {})
        published = {c.args[0]: c.args[1] for c in client.publish.call_args_list}
        self.assertTrue(published["homeassistant/select/dev/solar_mode/config"])


class SolarReadbackTests(unittest.TestCase):
    def test_comfort_and_green_share_use_one_alfenctl_call(self):
        output = ("ID NAME VALUE ACCESS TITLE\n3280_2 solarGreenShare 25 rw Green share\n"
                  "3280_3 solarComfortW 4200 rw Comfort level\n")
        client = Mock()
        with patch("app.run_alfen", return_value=output) as run:
            self.assertTrue(app.publish_solar_properties_readback(client, CFG))
        run.assert_called_once_with(CFG, "get", "3280_3", "3280_2", timeout=20)
        client.publish.assert_any_call("ace2mqtt/state/comfort_power", "4.2", qos=1, retain=True)
        client.publish.assert_any_call("ace2mqtt/state/green_share", "25", qos=1, retain=True)

    def test_one_unreadable_property_does_not_block_the_other(self):
        client = Mock()
        with patch("app.run_alfen", return_value="3280_2 solarGreenShare 30 rw Green share\n"), \
                self.assertLogs(app.LOG, level="WARNING"):
            self.assertFalse(app.publish_solar_properties_readback(client, CFG))
        client.publish.assert_called_once_with("ace2mqtt/state/green_share", "30", qos=1, retain=True)


class OutageToleranceTests(unittest.TestCase):
    def test_repeated_alfenctl_warning_is_logged_once(self):
        app.WARNING_LAST_LOGGED.clear()
        msg = "static load balancing is on, but this station is not licensed for it"
        with self.assertLogs(app.LOG, level="WARNING") as logs:
            for _ in range(5):
                app.log_alfen_warning(msg)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("without a licence", logs.output[0])
        with patch("app.time.monotonic", return_value=app.time.monotonic() + app.WARNING_REPEAT_SECONDS + 1), \
                self.assertLogs(app.LOG, level="WARNING") as logs:
            app.log_alfen_warning(msg)
        self.assertEqual(len(logs.output), 1)

    def _run(self, offline_after, failures):
        calls = iter(failures)
        def status(_cfg):
            value = next(calls)
            if isinstance(value, Exception):
                raise value
            return value
        clock = iter(range(0, 10000, 10))
        client = Mock()
        app.STOP.clear()
        remaining = [len(failures)]
        def counted_wait(_delay):
            remaining[0] -= 1
            if remaining[0] <= 0:
                app.STOP.set()
            return app.STOP.is_set()
        with patch("app.options", return_value={**CFG, "poll_interval": 10, "offline_after": offline_after}), \
                patch("app.write_alfen_config"), patch("app.supervisor_mqtt", side_effect=lambda c: c), \
                patch("app.signal.signal"), patch("app.connect_mqtt", return_value=client), \
                patch("app.poll_once", side_effect=status), \
                patch("app.time.monotonic", side_effect=lambda: next(clock)), \
                patch.object(app.STOP, "wait", side_effect=counted_wait), \
                self.assertLogs(app.LOG, level="ERROR"):
            app.main()
        app.STOP.clear()
        return [c for c in client.publish.call_args_list if c.args[1] == "offline"]

    def test_short_outage_keeps_entities_available(self):
        offline = self._run(300, [RuntimeError("tls timeout")] * 3)
        # the fake clock advances 20 s per failure, far below 300 s: only shutdown says offline
        self.assertEqual(len(offline), 1)

    def test_long_outage_marks_offline_once_then_shutdown(self):
        offline = self._run(15, [RuntimeError("tls timeout")] * 4)
        self.assertEqual(len(offline), 2)  # once after the grace period, once at shutdown

    def test_offline_after_zero_is_immediate(self):
        self.assertEqual(len(self._run(0, [RuntimeError("x")] * 2)), 2)

    def test_negative_offline_after_is_rejected(self):
        with self.assertRaises(ValueError):
            app.validate_options({**CFG, "offline_after": -1})


@unittest.skipIf(yaml is None, "PyYAML not installed")
class AddonManifestTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((ADDON / "config.yaml").read_text())

    def test_every_mapped_option_exists_in_options_and_schema(self):
        for key, (section, name) in app.OPTION_MAP.items():
            self.assertIn(name, self.config["schema"][section], key)
        for section, name in (("charger", "host"), ("mqtt", "host"), ("limits", "poll_interval")):
            self.assertIn(name, self.config["options"][section])

    def test_only_current_groups_and_general_options_are_exposed(self):
        expected = {"charger", "mqtt", "limits", "evcc", "solar_controls", "log_level"}
        self.assertEqual(set(self.config["schema"]), expected)
        self.assertEqual(set(self.config["options"]), expected)
        translations = yaml.safe_load((ADDON / "translations" / "en.yaml").read_text())["configuration"]
        self.assertEqual(set(translations), expected)

    def test_defaults_normalise_to_a_valid_config_except_the_host(self):
        cfg = app.normalize_options(self.config["options"])
        self.assertEqual(cfg["charger_host"], "")
        cfg["charger_host"] = "192.0.2.10"
        app.validate_options(cfg)

    def test_translations_cover_all_groups(self):
        translations = yaml.safe_load((ADDON / "translations" / "en.yaml").read_text())["configuration"]
        for section in ("charger", "mqtt", "limits", "evcc"):
            for name in self.config["schema"][section]:
                self.assertIn(name, translations[section]["fields"], f"{section}.{name}")
        for key in ("solar_controls", "log_level"):
            self.assertIn(key, translations)


if __name__ == "__main__":
    unittest.main()
