"""An unread sensor reports a placeholder temperature, not a measurement.

Found on hardware on 2026-08-28: with the floor sensor disconnected and the
thermostat switched to room-sensor control -- a supported installation, and one
the Ebeco app stops flagging as an error -- `floor_temperature` snapped from
25.7 to exactly 20.0 in the same poll that first raised `floor_sensor_error`,
then stayed pinned at 20.0. Recorder happily kept it: a permanent, plausible,
entirely fictional floor temperature in history and statistics.

The per-sensor error byte is the only thing that distinguishes it from a real
reading, since 20.0 C is not otherwise suspicious.
"""
import pytest
from eb300_ble.climate import EB300Climate
from eb300_ble.eb300_ble.const import SensorErrorCode
from eb300_ble.eb300_ble.models import ThermostatStatus
from eb300_ble.sensor import SENSOR_DESCRIPTIONS

PLACEHOLDER_DECIDEG = 200  # what the device reports for a sensor it cannot read

DESCRIPTIONS = {d.key: d for d in SENSOR_DESCRIPTIONS}


def _status(*, floor_error=0, room_error=0, floor=PLACEHOLDER_DECIDEG, room=233):
    return ThermostatStatus(
        error_flags=0,
        current_set_temperature=200,
        limiting_temperature=270,
        time_to_target=0,
        relay_on=False,
        in_error_state=False,
        limited_by_limiting_sensor=False,
        power_off=False,
        room_temperature=room,
        floor_temperature=floor,
        relay_temperature=332,
        room_sensor_error=room_error,
        floor_sensor_error=floor_error,
        current_program=1,
        energy_meter=518,
    )


class _Data:
    def __init__(self, status):
        self.status = status
        self.rssi = -60


class _Coordinator:
    """Enough of a coordinator for the read-only properties under test."""

    address = "AA:BB:CC:DD:EE:FF"

    def __init__(self, status):
        self.data = _Data(status)


def _value(key, status):
    return DESCRIPTIONS[key].value_fn(_Data(status))


# --- the sensors ----------------------------------------------------------

def test_floor_temperature_reads_normally_when_the_sensor_is_healthy():
    assert _value("floor_temperature", _status(floor=257)) == 25.7


@pytest.mark.parametrize("code", [c for c in SensorErrorCode if c is not SensorErrorCode.OK])
def test_floor_temperature_is_unknown_for_every_error_code(code):
    assert _value("floor_temperature", _status(floor_error=int(code))) is None


def test_room_temperature_is_unknown_when_the_room_sensor_errors():
    assert _value("room_temperature", _status(room_error=int(SensorErrorCode.OPEN_CIRCUIT))) is None
    assert _value("room_temperature", _status()) == 23.3


def test_an_unknown_error_code_still_suppresses_the_reading():
    """Any non-zero value means "not a measurement" -- do not enumerate codes."""
    assert _value("floor_temperature", _status(floor_error=9)) is None


def test_one_faulted_sensor_does_not_suppress_the_other():
    status = _status(floor_error=int(SensorErrorCode.OPEN_CIRCUIT))
    assert _value("floor_temperature", status) is None
    assert _value("room_temperature", status) == 23.3
    assert _value("relay_temperature", status) == 33.2


# --- the climate entity ---------------------------------------------------

def _climate(status, *, use_room_sensor):
    return EB300Climate(_Coordinator(status), use_room_sensor=use_room_sensor)


def test_climate_following_the_floor_sensor_reports_unknown_when_it_faults():
    status = _status(floor_error=int(SensorErrorCode.OPEN_CIRCUIT))
    assert _climate(status, use_room_sensor=False).current_temperature is None


def test_climate_following_the_room_sensor_ignores_a_floor_fault():
    """The installation this whole case came from: room control, no floor sensor."""
    status = _status(floor_error=int(SensorErrorCode.OPEN_CIRCUIT))
    assert _climate(status, use_room_sensor=True).current_temperature == 23.3


def test_climate_reports_its_sensor_normally_when_healthy():
    assert _climate(_status(floor=257), use_room_sensor=False).current_temperature == 25.7
    assert _climate(_status(), use_room_sensor=True).current_temperature == 23.3
