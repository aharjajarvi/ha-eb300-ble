"""`active` support: disabling a slot, and get -> edit -> set round-tripping.

The device always stores 4 slots per weekday and enforces chronological order
across inactive slots too, so a slot cannot be turned off by blanking its time
-- it keeps time and temperature and only stops firing. Before this,
`merge_home_program` hardcoded active=True, so there was no way to express that,
and `get_home_program` emitted an `active` key that `set_home_program` refused.
"""
import pathlib

import pytest
import yaml
from eb300_ble.const import WEEKDAYS
from eb300_ble.coordinator import merge_home_program
from eb300_ble.eb300_ble.exceptions import ValidationError
from eb300_ble.eb300_ble.models import HomeProgram
from eb300_ble.eb300_ble.protocol import HomeProgramEvent
from eb300_ble.services import SET_HOME_PROGRAM_SCHEMA, _program_to_response
from homeassistant.helpers.selector import selector as make_selector

COMPONENT_DIR = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "eb300_ble"
DAY_FIELDS = yaml.safe_load(
    (COMPONENT_DIR / "services.yaml").read_text()
)["set_home_program"]["fields"]["per_day"]["fields"]


def _event(hour, minute, temp, active=True):
    return HomeProgramEvent(
        active=active, hour=hour, minute=minute, temperature_decideg=int(temp * 10)
    )


def _program():
    """A schedule captured from a real device (see docs/HARDWARE_NOTES.md)."""
    weekday = [_event(6, 0, 22), _event(8, 0, 17), _event(15, 0, 22), _event(23, 0, 17)]
    weekend = [
        _event(7, 0, 22),
        _event(10, 0, 17, active=False),
        _event(16, 30, 22, active=False),
        _event(23, 0, 17),
    ]
    # list-of-lists, matching unpack_home_program(): HomeProgram is a frozen
    # dataclass whose __eq__ compares containers by type, and the verify-readback
    # in async_set_home_program relies on that equality holding.
    return HomeProgram(days=[list(weekday) for _ in range(5)] + [list(weekend) for _ in range(2)])


# --- the disable case the user hit ---------------------------------------

def test_schema_accepts_active_false():
    out = SET_HOME_PROGRAM_SCHEMA(
        {"entity_id": ["climate.x"], "monday": [{"time": "06:00", "temperature": 22.0, "active": False}]}
    )
    assert out["monday"][0]["active"] is False


def test_active_defaults_to_true_when_omitted():
    out = SET_HOME_PROGRAM_SCHEMA(
        {"entity_id": ["climate.x"], "monday": [{"time": "06:00", "temperature": 22.0}]}
    )
    assert out["monday"][0]["active"] is True


def test_merge_honours_active_false():
    merged = merge_home_program(
        _program(),
        {"monday": [
            {"time": "06:00", "temperature": 22.0, "active": True},
            {"time": "08:00", "temperature": 17.0, "active": False},
            {"time": "15:00", "temperature": 22.0, "active": False},
            {"time": "23:00", "temperature": 17.0, "active": True},
        ]},
    )
    assert [e.active for e in merged.days[0]] == [True, False, False, True]
    # a disabled slot keeps its time and temperature
    assert (merged.days[0][1].hour, merged.days[0][1].temperature_decideg) == (8, 170)


def test_disabled_slot_still_serialises():
    """Ordering is checked across inactive events, so a disabled slot must
    still carry an in-order time -- to_bytes() is where that is enforced."""
    merged = merge_home_program(
        _program(),
        {"monday": [
            {"time": "06:00", "temperature": 22.0, "active": False},
            {"time": "08:00", "temperature": 17.0, "active": False},
            {"time": "15:00", "temperature": 22.0, "active": False},
            {"time": "23:00", "temperature": 17.0, "active": False},
        ]},
    )
    assert merged.to_bytes()


def test_out_of_order_disabled_slot_is_still_rejected():
    with pytest.raises(ValidationError):
        merge_home_program(
            _program(),
            {"monday": [
                {"time": "23:00", "temperature": 17.0, "active": False},
                {"time": "06:00", "temperature": 22.0, "active": True},
            ]},
        ).to_bytes()


# --- round-trip: get output must be valid set input -----------------------

def test_get_output_is_valid_set_input():
    response = _program_to_response(_program())
    validated = SET_HOME_PROGRAM_SCHEMA({"entity_id": ["climate.x"], **response})
    for day in WEEKDAYS:
        assert validated[day] == response[day], day


def test_get_output_survives_the_ui_selector():
    """Pasting get output into the UI must not trip 'Field ... is not allowed'."""
    response = _program_to_response(_program())
    for day in WEEKDAYS:
        assert make_selector(DAY_FIELDS[day]["selector"])(response[day])


def test_full_round_trip_is_identity():
    """get -> set with no edits must leave the program byte-identical."""
    program = _program()
    response = _program_to_response(program)
    validated = SET_HOME_PROGRAM_SCHEMA({"entity_id": ["climate.x"], **response})
    merged = merge_home_program(program, {d: validated[d] for d in WEEKDAYS})
    assert merged.to_bytes() == program.to_bytes()


def test_round_trip_with_one_slot_disabled():
    program = _program()
    response = _program_to_response(program)
    response["monday"][2]["active"] = False        # the edit
    validated = SET_HOME_PROGRAM_SCHEMA({"entity_id": ["climate.x"], **response})
    merged = merge_home_program(program, {d: validated[d] for d in WEEKDAYS})
    assert [e.active for e in merged.days[0]] == [True, True, False, True]
    assert merged.days[1] == program.days[1]       # tuesday untouched


# --- the selector must declare `active` ----------------------------------

@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_declares_active(day):
    fields = DAY_FIELDS[day]["selector"]["object"]["fields"]
    assert set(fields) == {"time", "temperature", "active"}
    assert fields["active"]["required"] is False


@pytest.mark.parametrize("day", WEEKDAYS)
def test_selector_accepts_active_flag(day):
    sel = make_selector(DAY_FIELDS[day]["selector"])
    assert sel([{"time": "06:00:00", "temperature": 22.0, "active": False}])
