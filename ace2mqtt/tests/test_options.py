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
        cfg, migrated = app.normalize_options(GROUPED)
        self.assertFalse(migrated)
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
        https, _ = app.normalize_options(GROUPED)
        http, _ = app.normalize_options({**GROUPED, "charger": {**GROUPED["charger"], "http": True}})
        custom, _ = app.normalize_options({**GROUPED, "charger": {**GROUPED["charger"], "port": 8443}})
        self.assertEqual((https["charger_port"], http["charger_port"], custom["charger_port"]), (443, 80, 8443))

    def test_defaults_for_optional_values(self):
        cfg, _ = app.normalize_options({"charger": {"host": "h"}})
        self.assertEqual((cfg["mqtt_username"], cfg["evcc_base_url"], cfg["log_level"]), ("", "", "info"))
        self.assertTrue(cfg["solar_controls"])
        self.assertEqual(cfg["uid_vehicle_map"], {})

    def test_legacy_flat_options_are_migrated_and_win_over_group_defaults(self):
        legacy = {"charger_host": "192.0.2.10", "charger_port": 443, "charger_username": "admin",
                  "charger_password": "pw", "charger_http": False, "socket_number": 1,
                  "current_min": 10, "current_max": 20, "poll_interval": 30,
                  "mqtt_host": "core-mosquitto", "mqtt_topic_prefix": "ace2mqtt",
                  "evcc_base_url": "http://evcc:7070", "evcc_loadpoint_id": 3,
                  "uid_vehicle_map": '{"04AA": "car"}',
                  "limits": {"current_min": 6, "current_max": 32}}
        cfg, migrated = app.normalize_options(legacy)
        self.assertTrue(migrated)
        self.assertEqual((cfg["current_min"], cfg["current_max"], cfg["poll_interval"]), (10, 20, 30))
        self.assertEqual((cfg["charger_host"], cfg["evcc_loadpoint_id"]), ("192.0.2.10", 3))
        self.assertEqual(cfg["uid_vehicle_map"], {"04AA": "car"})
        grouped = app.grouped_options(cfg, legacy)
        self.assertEqual(grouped["charger"], {"host": "192.0.2.10", "username": "admin", "password": "pw",
                                              "http": False, "socket": 1})
        self.assertEqual(grouped["limits"]["current_min"], 10)
        self.assertEqual(grouped["evcc"]["vehicles"], [{"uid": "04AA", "vehicle": "car"}])
        self.assertNotIn("password", grouped["evcc"])
        self.assertNotIn("username", grouped["mqtt"])
        self.assertFalse(any(key in grouped for key in app.LEGACY_KEYS))

    def test_invalid_legacy_vehicle_map_is_rejected_by_validation(self):
        cfg, _ = app.normalize_options({"charger_host": "h", "uid_vehicle_map": "not json"})
        with self.assertRaises(ValueError):
            app.validate_options(cfg)

    def test_options_reads_file_and_requires_charger_host(self):
        for raw, ok in ((GROUPED, True), ({"charger": {"host": ""}}, False)):
            with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(raw))):
                if ok:
                    self.assertEqual(app.options()["charger_host"], "192.0.2.10")
                else:
                    with self.assertRaisesRegex(ValueError, r"charger\.host"):
                        app.options()

    def test_migration_is_stored_through_supervisor(self):
        raw = {"charger_host": "h", "mqtt_host": "m", "mqtt_topic_prefix": "p", "discovery_prefix": "d"}
        with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(raw))), \
                patch("app.save_supervisor_options", return_value=True) as save:
            app.options()
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0]["charger"]["host"], "h")

    def test_save_supervisor_options_needs_token_and_survives_errors(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertFalse(app.save_supervisor_options({}))
        with patch.dict("os.environ", {"SUPERVISOR_TOKEN": "t"}), patch("app.requests.post", side_effect=OSError("x")):
            self.assertFalse(app.save_supervisor_options({}))


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


@unittest.skipIf(yaml is None, "PyYAML not installed")
class AddonManifestTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load((ADDON / "config.yaml").read_text())

    def test_every_mapped_option_exists_in_options_and_schema(self):
        for key, (section, name) in app.NESTED.items():
            self.assertIn(name, self.config["schema"][section], key)
        for section, name in (("charger", "host"), ("mqtt", "host"), ("limits", "poll_interval")):
            self.assertIn(name, self.config["options"][section])

    def test_legacy_keys_are_optional_and_not_in_defaults(self):
        for key in app.LEGACY_KEYS:
            self.assertTrue(str(self.config["schema"][key]).endswith("?"), key)
            self.assertNotIn(key, self.config["options"])

    def test_defaults_normalise_to_a_valid_config_except_the_host(self):
        cfg, migrated = app.normalize_options(self.config["options"])
        self.assertFalse(migrated)
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
