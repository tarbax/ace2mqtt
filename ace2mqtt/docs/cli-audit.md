# alfenctl I/O audit — 2026-10-10

Reference: the published `alfenctl==0.1.0` wheel, matching requirements.txt.
Tests run against its CLI parser and output formatters with synthetic data;
no charger, broker, Home Assistant instance or evcc server was contacted.

| Interface | Source contract | Result |
| --- | --- | --- |
| Connection arguments | `cli/parser.py`: common options on action parser, implicit show insertion | Config, station and port accepted for every exposed command |
| Status | `cli/commands/status.py:_status_json` | Sockets list, operative booleans, station current and measurements preserved by flattening/Discovery |
| Socket | `cli/commands/controls.py:cmd_socket` | enable/disable syntax correct; added readback of selected socket's in-service flag |
| Profile override | `charging_profiles.py:DirectStart.rows`, `cli/commands/meter.py` | on/off syntax correct; added follow-profile/direct-start readback |
| Current | `controls.py:format_current`, `cli/commands/controls.py` | Selected socket maximum parsed correctly; config capped at CLI maximum 64 A; local minimum and station maximum enforced |
| Solar mode | `loadbalancing.py:SOLAR_MODES` | off/comfort/green map to integer arguments 0/1/2; off added to readback and Discovery |
| Comfort power | `cli/output.py:print_properties`, `loadbalancing.py` | Read exact 3280_3 value; use bounded `lb set --comfort-level`; CLI range 1350–11000 W; local configured cap and 50 W write steps retained |
| Green share | Same property formatter, `loadbalancing.py` | 3280_2 value parsed; integer 0–100 accepted by `lb set --green-share` |
| Legacy buttons | CLI parser for socket and direct-start | All four commands valid; now refresh associated switch after execution |
| RFID records | `transactions.py:record_dict`, `cli/commands/logs.py` | JSON list/start_tag matches; selection filters socket locally (CLI JSON export does not apply socket filter); upstream download sorts chronologically |
| RFID pulse | Local timer/publication tests | Superseded timer cannot clear latest pulse; current timer clears retained value after two seconds |
| evcc assignment | Existing local request tests | Encoded vehicle path, UID mapping, unknown-ID suppression and bounded retries pass; remote API behavior not independently verified |
| MQTT controls | Local publication tests | Retained commands ignored; failed writes/readbacks do not echo requested value; actual clamped values published |

All six controls refresh on every poll and after their write command. CLI warnings
on successful commands are surfaced instead of discarded. State names, unique
IDs and topic paths remain stable. The old button Discovery entries are still
removed, while their command topics remain accepted for compatibility.

## Validation

44 tests pass, including eight upstream contract tests. The four pre-existing
failures were stale test expectations: comfort tests lacked a cap allowing their
4.2 kW input, Discovery expected the old buttons/layout, and RFID expected only
one publication before the pulse sensor was added. The expectations now match
the current functionality and the tests cover the repaired behavior.

Command: `PYTHONPATH=ace2mqtt:<unpacked pinned wheel> python3 -m unittest discover -s ace2mqtt/tests`.
The upstream tests skip explicitly when alfenctl is not available.

## Remaining runtime boundaries

- No physical charger or live Home Assistant/MQTT verification, deployment or publication.
- Every poll now executes more serialized CLI reads. The configured interval is
  a delay after completion, not a guaranteed sampling period.
- Failed readback retains the previous known control state and logs a warning.
  Unsupported firmware properties can therefore produce recurring warnings.
- RFID remains transaction-derived; an older transaction can be the newest
  available while a fresh authorization is not yet recorded. It is not proof
  of the identity of every physical scan, and short authorization states can
  be missed between polls. This existing limitation is not fixed by parser tests.
- Generic status sensors retain their existing field-based naming and units
  behavior; missing fields are not a fresh measurement or automatic deletion.
- Existing configurations above 64 A or above 11 kW comfort cap must be adjusted
  before startup; the standard 6–32 A and 4 kW defaults remain valid.

## User-supplied charger output verified

On 2026-10-10 the user supplied current, socket, direct-start and both property
readouts from their charger. These are preserved in a regression test, including
the terminal escape sequences before the comfort table header. They parse as
32 A socket maximum, socket ON, profile override OFF, 2 kW comfort and 25% green
share. The external request of 8.7 A is distinct from the configured socket
maximum and is not used as the current-limit control state. All 44 tests pass.
This verifies parsing of actual supplied output; live MQTT/HA and writes remain
unverified.
