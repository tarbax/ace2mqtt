"""Contract checks against alfenctl 0.1.0, without contacting a charger."""
import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from test_app import app, CFG

try:
    from alfenctl.cli.parser import build_parser, insert_default_action
    from alfenctl.cli.output import print_rows, print_table
    from alfenctl import controls, charging_profiles, loadbalancing, status, transactions
    from alfenctl.cli.commands.status import _status_json
    from alfenctl.cli.commands.controls import cmd_socket
except ModuleNotFoundError:
    build_parser = None


def capture(fn, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        fn(*args, **kwargs)
    return out.getvalue()


@unittest.skipIf(build_parser is None, "Install pinned alfenctl==0.1.0 for upstream contract tests")
class UpstreamContractTests(unittest.TestCase):
    def test_every_command_is_accepted_by_real_cli_parser(self):
        commands = [
            ('status', '--json'), ('current',), ('lb',),
            ('get', '3280_3'), ('get', '3280_2'),
            ('socket', 'show', '2'), ('direct-start', 'show'),
            ('transactions', '--json', '--since', 'today', '--socket', '2'),
        ]
        for name, payload in [('socket', 'ON'), ('socket', 'OFF'),
                              ('charging_profile_override', 'ON'), ('charging_profile_override', 'OFF'),
                              ('current', '16'), ('comfort_power', '4.2'), ('green_share', '25'),
                              ('solar_mode', 'off'), ('solar_mode', 'comfort'), ('solar_mode', 'green'),
                              ('socket_enable', 'PRESS'), ('socket_disable', 'PRESS'),
                              ('direct_start_on', 'PRESS'), ('direct_start_off', 'PRESS')]:
            with patch('app.run_alfen') as run:
                app.handle_command(CFG, f'ace2mqtt/control/{name}', payload)
            commands.append(run.call_args.args[1:])
        parser = build_parser()
        for command in commands:
            with self.subTest(command=command):
                args = parser.parse_args(insert_default_action(app.alfen_command(CFG, *command)[1:]))
                self.assertEqual(args.station, 'ace2mqtt')
                self.assertEqual(args.port, 443)

    def test_current_from_upstream_formatter(self):
        output = capture(print_rows, 'Charging current limits', controls.format_current(
            controls.Controls(station_max_a=32, sockets={1: 16, 2: 8.5})))
        self.assertEqual(app.parse_current_limit(output, 2), '8.5')
        self.assertIsNone(app.parse_current_limit(output, 3))

    def test_all_solar_modes_from_upstream_formatter(self):
        for value, name in loadbalancing.SOLAR_MODES.items():
            state = loadbalancing.LoadBalancing(solar_mode=value, solar_green_share=25, solar_comfort_w=2000)
            output = capture(print_rows, 'Load balancing', state.rows())
            self.assertEqual(app.parse_solar_mode(output), name)
            self.assertEqual(int(app.SOLAR_MODE_VALUES[name]), value)

    def test_override_from_upstream_formatter(self):
        rows = charging_profiles.DirectStart(overrides={1: 0, 2: 1}).rows()
        output = capture(print_table, ['SETTING', 'VALUE'], [[a, b] for a, b in rows])
        self.assertEqual(app.parse_socket_state(output, 1, True), 'OFF')
        self.assertEqual(app.parse_socket_state(output, 2, True), 'ON')
        self.assertIsNone(app.parse_socket_state(output, 3, True))

    def test_socket_from_upstream_command(self):
        with patch('alfenctl.controls.read_socket_flags', return_value=(status.socket_inoperative_bit(2), 0)), \
             patch('alfenctl.controls.read', return_value=controls.Controls(sockets={1: 16, 2: 16})):
            output = capture(cmd_socket, Mock(), SimpleNamespace(action='show', socket=2))
        self.assertEqual(app.parse_socket_state(output, 2), 'OFF')

    def test_properties_from_real_table_formatter(self):
        from alfenctl.charger import LiveProperty
        from alfenctl.values import merge
        from alfenctl.eds import load_catalog
        from alfenctl.cli.output import print_properties
        catalog = load_catalog()
        for sub, value, parse, expected in [(2, 25, app.parse_green_share, '25'),
                                            (3, 2000, app.parse_comfort_power, '2')]:
            key = (0x3280, sub)
            live = LiveProperty(id=f'3280_{sub}', key=key, value=value, data_type=6, access=3)
            prop = merge(live, catalog.get(key))
            output = capture(print_properties, [prop], as_json=False)
            self.assertEqual(parse(output), expected, output)

    def test_transactions_from_real_json_serializer(self):
        records = [transactions.Record(offset=1, kind='start', raw='', socket=2, start_tag='AABB'),
                   transactions.Record(offset=2, kind='start', raw='', socket=1, start_tag='CCDD')]
        output = json.dumps([transactions.record_dict(r) for r in records])
        self.assertEqual(app.parse_latest_rfid_id(output, 2), 'AABB')

    def test_status_json_fields_and_discovery(self):
        snapshot = status.Status(sockets=[status.SocketStatus(number=2, main_state='nfc authorised', operative=True)],
                                 max_station_current_a=16, active_power_w=1234.5)
        doc = _status_json(snapshot)
        self.assertTrue(app.needs_rfid_readback(doc, 2))
        self.assertFalse(app.needs_rfid_readback(doc, 1))
        client = Mock()
        cfg = dict(CFG)
        app.publish_discovery(client, cfg, doc)
        client.publish.assert_any_call('ace2mqtt/state/sockets_0_operative', 'true', qos=1, retain=True)
        client.publish.assert_any_call('ace2mqtt/state/active_power_w', '1234.50', qos=1, retain=True)
        self.assertEqual(app.effective_current_bounds(cfg), (6, 16))


class OutputRegressionTests(unittest.TestCase):
    def test_user_charger_output_2026_10_10(self):
        current = """Charging current limits:

  Station maximum       32 A
  Installation maximum  40 A
  Socket 1 maximum      32 A
    external request    8.7 A
    now allowing        40.2 A static, 8.7 A active, 40.2 A P1
"""
        socket = """Sockets:

  Station   in service
  Socket 1  in service
"""
        override = """SETTING       VALUE
Socket 1      follow profile
Socket 2      follow profile
Random delay  0 s (below the compliant 600 s)
"""
        comfort = "\x1b[B\x1b[BID      NAME    VALUE  ACCESS  TITLE\n3280_3  3280_3  2000   rw      Solar - Comfort Level\n"
        green = "ID      NAME    VALUE  ACCESS  TITLE\n3280_2  3280_2  25     rw      Solar - Green Share\n"
        self.assertEqual(app.parse_current_limit(current, 1), '32')
        self.assertIsNone(app.parse_current_limit(current, 2))
        self.assertEqual(app.parse_socket_state(socket, 1), 'ON')
        self.assertEqual(app.parse_socket_state(override, 1, True), 'OFF')
        self.assertEqual(app.parse_comfort_power(comfort), '2')
        self.assertEqual(app.parse_green_share(green), '25')

    def test_successful_cli_warnings_are_not_lost(self):
        result = SimpleNamespace(returncode=0, stdout='result', stderr='warning: no profile installed\n')
        with patch('app.subprocess.run', return_value=result), self.assertLogs(app.LOG, level='WARNING') as logs:
            self.assertEqual(app.run_alfen(CFG, 'direct-start', 'on'), 'result')
        self.assertIn('no profile installed', logs.output[0])

    def test_all_controls_are_refreshed_on_each_poll(self):
        from contextlib import ExitStack
        readers = ['current', 'solar_mode', 'comfort_power', 'green_share', 'socket', 'override']
        with ExitStack() as stack:
            stack.enter_context(patch('app.options', return_value={**CFG, 'poll_interval': 10}))
            stack.enter_context(patch('app.write_alfen_config'))
            stack.enter_context(patch('app.connect_mqtt', return_value=Mock()))
            stack.enter_context(patch('app.run_status', return_value={'sockets': []}))
            stack.enter_context(patch('app.publish_discovery'))
            stack.enter_context(patch('app.time.sleep', side_effect=[None, KeyboardInterrupt]))
            mocks = [stack.enter_context(patch(f'app.publish_{name}_readback', return_value=True)) for name in readers]
            app.main()
            for reader in mocks:
                self.assertEqual(reader.call_count, 2)

    def test_property_values_do_not_come_from_ids_or_titles(self):
        for value in (1350, 2000, 4225, 11000):
            output = f'ID NAME VALUE ACCESS TITLE\n3280_3 solarComfortW {value} rw Solar Comfort Level 9999\n'
            self.assertEqual(float(app.parse_comfort_power(output)), value / 1000)
        for output in ('3280_2 solarGreenShare 25 rw green', '3280_3 solarComfortW unknown rw 2000',
                       '3280_3 solarComfortW NaN rw 2000', 'no property 2000'):
            self.assertIsNone(app.parse_comfort_power(output))
        for value in (0, 25, 100):
            self.assertEqual(app.parse_green_share(f'3280_2 solarGreenShare {value} rw Green share'), str(value))
        for value in ('-1', '101', '25.5', 'NaN', 'Infinity'):
            self.assertIsNone(app.parse_green_share(f'3280_2 solarGreenShare {value} rw Green share'))

    def test_commands_publish_reported_not_requested_values(self):
        cases = [('current', '16', 'Socket 2 maximum 10 A', 'current_limit', '10'),
                 ('solar_mode', 'green', 'Solar charging off', 'solar_mode', 'off'),
                 ('green_share', '25', '3280_2 solarGreenShare 30 rw Green', 'green_share', '30'),
                 ('comfort_power', '4.2', '3280_3 solarComfortW 2000 rw Comfort', 'comfort_power', '2'),
                 ('socket', 'ON', 'Socket 2 out of service', 'socket', 'OFF'),
                 ('charging_profile_override', 'ON', 'Socket 2 follow profile', 'charging_profile_override', 'OFF')]
        for name, requested, output, state, expected in cases:
            with self.subTest(name=name):
                client = Mock()
                with patch('app.run_alfen', side_effect=['', output]):
                    app.on_message(client, CFG, SimpleNamespace(topic=f'ace2mqtt/control/{name}', payload=requested.encode(), retain=False))
                client.publish.assert_called_once_with(f'ace2mqtt/state/{state}', expected, qos=1, retain=True)

    def test_failed_write_or_unrecognized_readback_never_echoes_request(self):
        for outputs in ([RuntimeError('failure')], ['', 'unrecognized']):
            client = Mock()
            with patch('app.run_alfen', side_effect=outputs):
                app.on_message(client, CFG, SimpleNamespace(topic='ace2mqtt/control/current', payload=b'16', retain=False))
            client.publish.assert_not_called()

    def test_retained_controls_and_invalid_percentages_never_write(self):
        for name, value in [('socket', 'OFF'), ('charging_profile_override', 'ON'), ('current', '16'),
                            ('comfort_power', '2'), ('solar_mode', 'off'), ('green_share', '25')]:
            with patch('app.run_alfen') as run:
                app.on_message(Mock(), CFG, SimpleNamespace(topic=f'ace2mqtt/control/{name}', payload=value.encode(), retain=True))
                run.assert_not_called()
        for value in ('-1', '101', '25.5', 'nan', 'foo'):
            with patch('app.run_alfen') as run:
                self.assertIsNone(app.handle_command(CFG, 'ace2mqtt/control/green_share', value))
                run.assert_not_called()

    def test_station_cap_and_config_limits(self):
        with patch('app.run_alfen') as run:
            app.handle_command({**CFG, '_station_max_current_a': 10}, 'ace2mqtt/control/current', '16')
            app.handle_command({**CFG, '_station_max_current_a': 0}, 'ace2mqtt/control/current', '0')
            run.assert_not_called()
        for change in ({'current_max': 80}, {'comfort_power_max_kw': 22}):
            with self.assertRaises(ValueError):
                app.validate_options({**CFG, **change})

    def test_rfid_uses_selected_socket_and_latest_nonempty_tag(self):
        records = [{'socket': 2, 'start_tag': 'old'}, {'socket': 1, 'start_tag': 'other'},
                   {'socket': 2, 'start_tag': 'new'}, {'socket': 2, 'stop_tag': 'stop'}]
        self.assertEqual(app.parse_latest_rfid_id(json.dumps(records), 2), 'new')
        for output in ('', 'null', '{}', '[]'):
            self.assertIsNone(app.parse_latest_rfid_id(output, 2))

    def test_rfid_pulse_clears_only_latest_timer(self):
        client = Mock()
        topic = 'ace2mqtt/state/rfid_id_pulse'
        with patch('app.threading.Timer') as timer:
            app.publish_rfid_id_pulse(client, CFG, 'first')
            first_args = timer.call_args.kwargs['args']
            app.publish_rfid_id_pulse(client, CFG, 'second')
            second_args = timer.call_args.kwargs['args']
            client.reset_mock()
            app._clear_rfid_id_pulse(*first_args)
            client.publish.assert_not_called()
            app._clear_rfid_id_pulse(*second_args)
            client.publish.assert_called_once_with(topic, '', qos=1, retain=True)
