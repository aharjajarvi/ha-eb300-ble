"""C-1..C-6, C-10, C-11: codec, counters, status struct, home program."""

from __future__ import annotations

import struct

import pytest
from eb300_ble.const import COUNTER_REHANDSHAKE_THRESHOLD, MAX_INNER_PAYLOAD, THERMOSTAT_STATUS_LEN
from eb300_ble.exceptions import ProtocolError, SessionExhaustedError, ValidationError
from eb300_ble.protocol import (
    Counter,
    HomeProgramEvent,
    build_inner_message,
    decideg_to_s8,
    pack_home_program,
    pack_thermostat_status,
    parse_inner_messages,
    s8_to_decideg,
    unpack_home_program,
    unpack_thermostat_status,
)

# ── C-1: golden vectors ──────────────────────────────────────────────────


def test_c1_build_inner_message_get_firmware_version():
    msg = build_inner_message(operation=0x00, pid=0x0005, counter=1)
    assert msg == bytes.fromhex("060001000500")


def test_c1_build_inner_message_set_override_temperature():
    msg = build_inner_message(operation=0x01, pid=0x10D0, counter=1, data=struct.pack("<H", 220))
    assert msg == bytes.fromhex("08010100D010DC00")


# ── C-2: batched frame round trip ───────────────────────────────────────


def test_c2_batched_frame_round_trip():
    a = build_inner_message(operation=0x00, pid=0x0001, counter=1)
    b = build_inner_message(operation=0x00, pid=0x0005, counter=2, data=b"x")
    messages = parse_inner_messages(a + b)
    assert len(messages) == 2
    assert messages[0].pid == 0x0001 and messages[0].counter == 1 and messages[0].data == b""
    assert messages[1].pid == 0x0005 and messages[1].counter == 2 and messages[1].data == b"x"


# ── C-3: OpStatus split ──────────────────────────────────────────────────


def test_c3_op_status_split():
    from eb300_ble.const import split_op_status

    error, operation = split_op_status(0x53)
    assert error == 5  # UNAUTHORIZED
    assert operation == 3  # SET_RESPONSE


# ── C-4: ThermostatStatus offsets ────────────────────────────────────────


def test_c4_thermostat_status_field_offsets():
    fields = {
        "error_flags": 0xBEEF,
        "current_set_temperature": 225,
        "limiting_temperature": -50,
        "time_to_target": 42,
        "relay_on": True,
        "in_error_state": False,
        "limited_by_limiting_sensor": True,
        "power_off": False,
        "room_temperature": 210,
        "floor_temperature": 220,
        "relay_temperature": 230,
        "room_sensor_error": 1,
        "floor_sensor_error": 2,
        "current_program": 1,
        "energy_meter": 123456,
    }
    raw = pack_thermostat_status(fields)
    assert len(raw) == THERMOSTAT_STATUS_LEN
    parsed = unpack_thermostat_status(raw)
    assert parsed == fields

    # Spot-check raw byte offsets directly against the Open API §5.7 table.
    assert struct.unpack_from("<H", raw, 0)[0] == 0xBEEF  # ErrorFlags
    assert struct.unpack_from("<h", raw, 2)[0] == 225  # CurrentSetTemperature
    assert struct.unpack_from("<h", raw, 4)[0] == -50  # LimitingTemperature
    assert struct.unpack_from("<H", raw, 8)[0] == 42  # TimeToTarget
    assert raw[12] == 1  # RelayOn
    assert raw[13] == 0  # InErrorState
    assert raw[14] == 1  # LimitedByLimitingSensor
    assert raw[16] == 0  # PowerOff
    assert struct.unpack_from("<hhh", raw, 18) == (210, 220, 230)  # SensorReadings
    assert raw[24] == 1 and raw[25] == 2  # SensorErrorCodes
    assert raw[27] == 1  # CurrentProgram
    assert struct.unpack_from("<I", raw, 28)[0] == 123456  # EnergyMeter


def test_c4_all_error_flag_bits_decoded():
    from eb300_ble.models import ThermostatStatus

    all_named_bits = 0x0002 | 0x0004 | 0x0010 | 0x0020 | 0x0040 | 0x0080 | 0x0100 | 0x0200 | 0x0400 | 0x1000 | 0x2000 | 0x4000
    status = ThermostatStatus(
        error_flags=all_named_bits,
        current_set_temperature=0,
        limiting_temperature=0,
        time_to_target=0,
        relay_on=False,
        in_error_state=False,
        limited_by_limiting_sensor=False,
        power_off=False,
        room_temperature=0,
        floor_temperature=0,
        relay_temperature=0,
        room_sensor_error=0,
        floor_sensor_error=0,
        current_program=0,
        energy_meter=0,
    )
    assert len(status.active_error_flags) == 12


# ── C-5: status length guard ─────────────────────────────────────────────


def test_c5_status_too_short_raises():
    with pytest.raises(ProtocolError):
        unpack_thermostat_status(bytes(37))


def test_c5_status_exact_length_ok():
    unpack_thermostat_status(bytes(38))  # all-zero struct, just must not raise


def test_c5_status_trailing_bytes_ignored():
    parsed = unpack_thermostat_status(bytes(40))
    assert parsed["error_flags"] == 0


# ── C-6: counter policy ──────────────────────────────────────────────────


def test_c6_counter_strictly_increasing():
    counter = Counter()
    values = [counter.next() for _ in range(5)]
    assert values == [1, 2, 3, 4, 5]


def test_c6_counter_near_wraparound_raises():
    counter = Counter(start=COUNTER_REHANDSHAKE_THRESHOLD)
    with pytest.raises(SessionExhaustedError):
        counter.next()


# ── C-10: size budget ─────────────────────────────────────────────────────


def test_c10_oversized_inner_message_raises():
    with pytest.raises(ProtocolError):
        build_inner_message(operation=0x01, pid=0x10C0, counter=1, data=bytes(MAX_INNER_PAYLOAD))


def test_c10_home_program_frame_fits_budget():
    days = _valid_home_program_days()
    program_bytes = pack_home_program(days)
    assert len(program_bytes) == 112
    msg = build_inner_message(operation=0x01, pid=0x10C0, counter=1, data=program_bytes)
    assert len(msg) == 118


# ── C-11: home program pack/unpack + constraints ────────────────────────


def test_c11_temperature_conversion():
    assert s8_to_decideg(44) == 220
    assert decideg_to_s8(220) == 44


def test_c11_home_program_round_trip():
    days = _valid_home_program_days()
    packed = pack_home_program(days)
    unpacked = unpack_home_program(packed)
    assert unpacked == days


def test_c11_wrong_day_count_rejected():
    with pytest.raises(ValidationError):
        pack_home_program(_valid_home_program_days()[:6])


def test_c11_wrong_event_count_rejected():
    days = _valid_home_program_days()
    days[0] = days[0][:3]
    with pytest.raises(ValidationError):
        pack_home_program(days)


def test_c11_temperature_out_of_range_rejected():
    days = _valid_home_program_days()
    days[0][0] = HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=1000)
    with pytest.raises(ValidationError):
        pack_home_program(days)


def test_c11_chronological_order_violation_rejected():
    days = _valid_home_program_days()
    days[0][0] = HomeProgramEvent(active=True, hour=23, minute=0, temperature_decideg=230)
    days[0][1] = HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=180)
    with pytest.raises(ValidationError):
        pack_home_program(days)


# ── The raw-clock daybreak/day-end checks were removed — they were
# mutually unsatisfiable for any day spanning a morning and an evening event,
# rejecting the real device's own schedule. Only chronological order on the
# virtual (02:00-anchored) timeline is enforced now.


def test_c11_event_before_daybreak_out_of_order_still_rejected():
    # Still rejected, but via the ordering check: 01:00 wraps to very late on
    # the virtual timeline (23:00), so putting it first violates order against
    # the later same-day event — not because 01:00 is "before 02:00" per se.
    days = _valid_home_program_days()
    days[0][0] = HomeProgramEvent(active=True, hour=1, minute=0, temperature_decideg=230)
    with pytest.raises(ValidationError):
        pack_home_program(days)


def test_c11_after_midnight_last_event_now_legal():
    # 01:00 as the day's last (latest-virtual) event is within the
    # 02:00->01:50 window and must be accepted post-fix, unlike before.
    days = _valid_home_program_days()
    days[0] = [
        HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=230),
        HomeProgramEvent(active=True, hour=22, minute=0, temperature_decideg=180),
        HomeProgramEvent(active=False, hour=0, minute=0, temperature_decideg=180),
        HomeProgramEvent(active=True, hour=1, minute=0, temperature_decideg=180),
    ]
    packed = pack_home_program(days)
    assert unpack_home_program(packed) == days


# ── Silent temperature truncation ────────────────────────────────────────


def test_c11_non_half_degree_temperature_rejected():
    # 22.3 C (223 decideg) is not representable at the device's 0.5 C
    # resolution — must be rejected, not silently stored as 22.0 C.
    days = _valid_home_program_days()
    days[0][0] = HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=223)
    with pytest.raises(ValidationError, match="not a multiple of 0.5"):
        pack_home_program(days)


# ── Inactive events are range-checked too ────────────────────────────────


def test_c11_inactive_event_out_of_range_temperature_rejected_cleanly():
    # Previously this reached struct.pack unguarded and raised a bare
    # struct.error instead of ValidationError.
    days = _valid_home_program_days()
    days[0][2] = HomeProgramEvent(active=False, hour=10, minute=0, temperature_decideg=1000)
    with pytest.raises(ValidationError):
        pack_home_program(days)


# ── Inactive-event temperatures round-trip byte-for-byte ─────────────────


def test_c11_inactive_event_temperature_preserved_through_round_trip():
    # Mirrors the real device's own weekend rows (docs/HARDWARE_NOTES.md): inactive
    # events sit mid-day, chronologically ordered, carrying a real temperature.
    days = _valid_home_program_days()
    days[0] = [
        HomeProgramEvent(active=True, hour=7, minute=0, temperature_decideg=220),
        HomeProgramEvent(active=False, hour=10, minute=0, temperature_decideg=170),
        HomeProgramEvent(active=False, hour=16, minute=30, temperature_decideg=220),
        HomeProgramEvent(active=True, hour=23, minute=0, temperature_decideg=170),
    ]
    packed = pack_home_program(days)
    unpacked = unpack_home_program(packed)
    assert unpacked[0][1] == HomeProgramEvent(active=False, hour=10, minute=0, temperature_decideg=170)


# ── Golden file: the real device's own schedule (offline equivalent of P-2) ──


# A real EB-Therm 300's factory-shipped weekly schedule, read off the device
# with `tools/session.py --snapshot`. Kept inline rather than as a snapshot file
# so the suite is self-contained and this case runs in CI on a clean checkout.
#
# Note the weekend rows: the device stores inactive slots with a real in-range
# temperature, not a zero placeholder. An earlier validator assumed zeros and
# rejected the device's own live schedule — see docs/HARDWARE_NOTES.md.
REAL_DEVICE_HOME_PROGRAM_HEX = (
    "0106002c01080022010f002c011700220106002c01080022010f002c01170022"
    "0106002c01080022010f002c011700220106002c01080022010f002c01170022"
    "0106002c01080022010f002c011700220107002c000a002200101e2c01170022"
    "0107002c000a002200101e2c01170022"
)


def test_c11_real_device_payload_round_trips_byte_identical():
    """Decoding and re-encoding a real device's own schedule must reproduce the
    exact same bytes. Before the inactive-slot validation fix, the validator
    rejected the device's own live schedule outright.
    """
    original = bytes.fromhex(REAL_DEVICE_HOME_PROGRAM_HEX)
    assert len(original) == 112

    days = unpack_home_program(original)
    repacked = pack_home_program(days)
    assert repacked == original


def _valid_home_program_days() -> list[list[HomeProgramEvent]]:
    def empty() -> HomeProgramEvent:
        # Inactive events are range-checked too, so this can't be a bare
        # decideg=0 placeholder (that shape happened to slip past the old,
        # buggy validator by accident — see the inactive-slot fix). Real devices store a real
        # in-range value here (docs/HARDWARE_NOTES.md's weekend rows), so this fixture
        # does too.
        return HomeProgramEvent(active=False, hour=0, minute=0, temperature_decideg=180)

    weekday = [
        HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=230),
        HomeProgramEvent(active=True, hour=22, minute=0, temperature_decideg=180),
        empty(),
        empty(),
    ]
    weekend = [
        HomeProgramEvent(active=True, hour=8, minute=0, temperature_decideg=230),
        HomeProgramEvent(active=True, hour=23, minute=0, temperature_decideg=180),
        empty(),
        empty(),
    ]
    return [weekday.copy() for _ in range(5)] + [weekend.copy() for _ in range(2)]
