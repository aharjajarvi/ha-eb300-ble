"""Unit conversions and enum properties on the user-facing dataclasses."""

from __future__ import annotations

from eb300_ble.const import Program, SensorErrorCode
from eb300_ble.models import HomeProgram, ThermostatStatus
from eb300_ble.protocol import HomeProgramEvent

_STATUS_KWARGS = {
    "error_flags": 0,
    "current_set_temperature": 225,
    "limiting_temperature": 180,
    "time_to_target": 0,
    "relay_on": True,
    "in_error_state": False,
    "limited_by_limiting_sensor": False,
    "power_off": False,
    "room_temperature": 210,
    "floor_temperature": 230,
    "relay_temperature": 240,
    "room_sensor_error": 0,
    "floor_sensor_error": 0,
    "current_program": 1,
    "energy_meter": 0,
}


def test_temperature_celsius_conversions():
    status = ThermostatStatus(**_STATUS_KWARGS)
    assert status.current_set_temperature_c == 22.5
    assert status.limiting_temperature_c == 18.0
    assert status.room_temperature_c == 21.0
    assert status.floor_temperature_c == 23.0
    assert status.relay_temperature_c == 24.0


def test_program_and_sensor_fault_enums():
    status = ThermostatStatus(**_STATUS_KWARGS)
    assert status.program is Program.HOME
    assert status.room_sensor_fault is SensorErrorCode.OK
    assert status.floor_sensor_fault is SensorErrorCode.OK


def test_home_program_bytes_round_trip():
    def empty() -> HomeProgramEvent:
        return HomeProgramEvent(active=False, hour=0, minute=0, temperature_decideg=180)

    day = [
        HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=230),
        HomeProgramEvent(active=True, hour=22, minute=0, temperature_decideg=180),
        empty(),
        empty(),
    ]
    program = HomeProgram(days=[day.copy() for _ in range(7)])
    raw = program.to_bytes()
    assert len(raw) == 112
    assert HomeProgram.from_bytes(raw) == program
