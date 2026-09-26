"""Entity setup and the non-debounced write paths, one platform at a time.

These are the entities whose writes go straight at the coordinator: no
debounce, no optimistic state, nothing clever. What is worth pinning is the
*mapping* -- which option string becomes which enum, which button reaches which
method -- because every one of those is a lambda in a description tuple, where a
copy-paste slip type-checks, imports fine, and actuates the wrong thing.

`number.py` gets the most attention here: 0x10B2 is written as a single s16[3]
triplet, so each calibration entity has to send the *other* axis' current value
alongside its own. Getting that backwards silently overwrites a calibration the
user set months ago, and looks correct in every other check.
"""
from unittest.mock import AsyncMock

import pytest
from conftest import make_data, make_status
from eb300_ble import binary_sensor, number, select
from eb300_ble.coordinator import EB300Coordinator
from eb300_ble.eb300_ble.const import (
    ERROR_FLAG_FLOOR_SENSOR_OPEN,
    KeyLock,
    Language,
    ScreensaverType,
)
from homeassistant.const import Platform
from homeassistant.helpers import entity_registry as er

ALL_PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
]


@pytest.fixture
def platforms():
    return ALL_PLATFORMS


def _entity(hass, domain, key):
    """Find an entity by the description key that forms its unique_id."""
    registry = er.async_get(hass)
    from conftest import ADDRESS

    entity_id = registry.async_get_entity_id(domain, "eb300_ble", f"{ADDRESS}_{key}")
    assert entity_id is not None, f"no {domain} entity for key {key!r}"
    return hass.data["entity_components"][domain].get_entity(entity_id)


# --- setup ---------------------------------------------------------------


async def test_every_platform_is_set_up_and_no_unique_id_collides(hass, loaded_entry):
    """One device, seven platforms, and unique_ids built from a shared f-string
    in `entity.py` -- a duplicated description key would silently drop an
    entity rather than fail."""
    entries = er.async_get(hass).entities.get_entries_for_config_entry_id(loaded_entry.entry_id)
    unique_ids = [e.unique_id for e in entries]

    assert len(set(unique_ids)) == len(unique_ids)
    assert {e.domain for e in entries} == {p.value for p in ALL_PLATFORMS}


@pytest.mark.parametrize(
    ("domain", "descriptions"),
    [
        ("binary_sensor", binary_sensor.BINARY_SENSOR_DESCRIPTIONS),
        ("select", select.SELECT_DESCRIPTIONS),
        ("number", number.NUMBER_DESCRIPTIONS),
    ],
)
async def test_every_declared_description_becomes_an_entity(hass, loaded_entry, domain, descriptions):
    """Adding a description to a tuple is the whole registration mechanism;
    nothing else would notice a platform that forgot to iterate it."""
    for description in descriptions:
        assert _entity(hass, domain, description.key) is not None


# --- switch: key lock -----------------------------------------------------


@pytest.mark.parametrize(("lock", "expected"), [(KeyLock.LOCKED, True), (KeyLock.UNLOCKED, False)])
async def test_the_key_lock_switch_reads_the_lock_state(hass, loaded_entry, lock, expected):
    loaded_entry.runtime_data.data = make_data(key_lock=lock)

    assert _entity(hass, "switch", "key_lock").is_on is expected


@pytest.mark.parametrize(("method", "expected"), [("async_turn_on", True), ("async_turn_off", False)])
async def test_the_key_lock_switch_writes_the_lock_state(hass, loaded_entry, monkeypatch, method, expected):
    write = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, "async_set_key_lock", write)

    await getattr(_entity(hass, "switch", "key_lock"), method)()

    write.assert_awaited_once_with(expected)


# --- select: language and screensaver -------------------------------------


@pytest.mark.parametrize(
    ("key", "option", "setter", "expected"),
    [
        ("language", "finnish", "async_set_language", Language.FINNISH),
        ("language", "swedish", "async_set_language", Language.SWEDISH),
        ("screensaver_type", "time_date", "async_set_screensaver", ScreensaverType.TIME_DATE),
        ("screensaver_type", "off", "async_set_screensaver", ScreensaverType.OFF),
    ],
)
async def test_a_selected_option_becomes_the_right_enum(
    hass, loaded_entry, monkeypatch, key, option, setter, expected
):
    """The option strings are lowercased enum names, so the mapping back is a
    `Enum[option.upper()]` -- fine until an option is renamed for the UI."""
    write = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, setter, write)

    await _entity(hass, "select", key).async_select_option(option)

    write.assert_awaited_once_with(expected)


@pytest.mark.parametrize("description", select.SELECT_DESCRIPTIONS, ids=lambda d: d.key)
async def test_every_select_option_round_trips(hass, loaded_entry, monkeypatch, description):
    """Every option a select offers must be one the coordinator can be asked
    for, and must read back as the same string."""
    written = []
    for setter in ("async_set_language", "async_set_screensaver"):
        monkeypatch.setattr(EB300Coordinator, setter, AsyncMock(side_effect=lambda v: written.append(v)))

    entity = _entity(hass, "select", description.key)
    for option in description.options:
        await entity.async_select_option(option)

    assert [v.name.lower() for v in written] == list(description.options)


@pytest.mark.parametrize(
    ("key", "field", "value", "expected"),
    [
        ("language", "language", Language.NORWEGIAN, "norwegian"),
        ("screensaver_type", "screensaver", ScreensaverType.TEMPERATURE, "temperature"),
    ],
)
async def test_a_select_reports_the_devices_current_value(hass, loaded_entry, key, field, value, expected):
    loaded_entry.runtime_data.data = make_data(**{field: value})

    assert _entity(hass, "select", key).current_option == expected


# --- number: the calibration triplet --------------------------------------


async def test_setting_room_calibration_leaves_the_floor_value_alone(hass, loaded_entry, monkeypatch):
    """0x10B2 is one s16[3] write. Sending the room value without carrying the
    current floor value through would zero a calibration the installer set."""
    write = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, "async_set_calibration", write)

    await _entity(hass, "number", "room_calibration").async_set_native_value(1.2)

    write.assert_awaited_once_with(room_decideg=12, floor_decideg=-3)


async def test_setting_floor_calibration_leaves_the_room_value_alone(hass, loaded_entry, monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, "async_set_calibration", write)

    await _entity(hass, "number", "floor_calibration").async_set_native_value(-0.7)

    write.assert_awaited_once_with(room_decideg=5, floor_decideg=-7)


@pytest.mark.parametrize(("key", "expected"), [("room_calibration", 0.5), ("floor_calibration", -0.3)])
async def test_calibration_numbers_report_decidegrees_as_celsius(hass, loaded_entry, key, expected):
    assert _entity(hass, "number", key).native_value == expected


@pytest.mark.parametrize("key", ["room_calibration", "floor_calibration"])
async def test_calibration_limits_match_the_devices_own(hass, loaded_entry, key):
    """The device accepts -5.0..+5.0; a wider UI range would only fail after a
    BLE round trip."""
    entity = _entity(hass, "number", key)

    assert (entity.native_min_value, entity.native_max_value) == (-5.0, 5.0)
    assert entity.native_step == 0.1


# --- button: clock sync ---------------------------------------------------


async def test_pressing_sync_clock_syncs_the_clock(hass, loaded_entry, monkeypatch):
    write = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, "async_sync_clock", write)

    await _entity(hass, "button", "sync_clock").async_press()

    write.assert_awaited_once_with()


# --- binary_sensor --------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "status_kwargs", "expected"),
    [
        ("heating", {"relay_on": True}, True),
        ("heating", {"relay_on": False}, False),
        # `power` reports the unit being ON, so it inverts the device's
        # `power_off` bit -- the one place in this integration where a flag is
        # negated on the way out.
        ("power", {"power_off": True}, False),
        ("power", {"power_off": False}, True),
        ("room_sensor_fault", {"room_error": 1}, True),
        ("room_sensor_fault", {"room_error": 0}, False),
        ("floor_sensor_fault", {"floor_error": 4}, True),
        ("floor_sensor_fault", {"floor_error": 0}, False),
    ],
)
async def test_binary_sensors_read_their_own_bit(hass, loaded_entry, key, status_kwargs, expected):
    loaded_entry.runtime_data.data = make_data(make_status(**status_kwargs))

    assert _entity(hass, "binary_sensor", key).is_on is expected


async def test_the_problem_sensor_trips_on_a_flag_as_well_as_the_state_bit(hass, loaded_entry):
    """`in_error_state` and a set `error_flags` bit are separate device
    signals; either one is a problem."""
    from dataclasses import replace

    entity = _entity(hass, "binary_sensor", "problem")

    loaded_entry.runtime_data.data = make_data(make_status())
    assert entity.is_on is False

    loaded_entry.runtime_data.data = make_data(replace(make_status(), in_error_state=True))
    assert entity.is_on is True

    loaded_entry.runtime_data.data = make_data(
        replace(make_status(), error_flags=ERROR_FLAG_FLOOR_SENSOR_OPEN)
    )
    assert entity.is_on is True

    # Only *named* bits count: `active_error_flags` walks ERROR_FLAG_NAMES, so a
    # bit the protocol table does not know about reads as no problem at all.
    # True today by construction, and worth knowing if firmware ever adds one.
    loaded_entry.runtime_data.data = make_data(replace(make_status(), error_flags=0x0001))
    assert entity.is_on is False


# --- a value the device sent that has no name here -----------------------


async def test_unknown_device_values_read_unknown_and_nothing_else_breaks(hass, loaded_entry):
    """A newer firmware adding a language or a program must cost the entities
    that show it their value, not the device its availability. Written through
    the coordinator for real, so every entity's state write runs."""
    registry = er.async_get(hass)
    from conftest import ADDRESS

    def state(domain, key):
        return hass.states.get(registry.async_get_entity_id(domain, "eb300_ble", f"{ADDRESS}_{key}"))

    loaded_entry.runtime_data.async_set_updated_data(
        make_data(make_status(current_program=5), key_lock=None, language=None, screensaver=None)
    )
    await hass.async_block_till_done()

    assert state("switch", "key_lock").state == "unknown"
    assert state("select", "language").state == "unknown"
    assert state("select", "screensaver_type").state == "unknown"
    assert state("sensor", "program").state == "unknown"
    climate = state("climate", "thermostat")
    assert climate.state == "heat"
    assert climate.attributes["preset_mode"] is None
    assert state("sensor", "room_temperature").state == "23.3"
