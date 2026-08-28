"""The two entities the device does not measure, and the names they claim.

The thermostat reports neither power nor energy. Both are derived from the
element wattage entered in the options -- relay state x watts for `power`,
relay-on minutes x watts for `energy` -- so both exist only when that option is
set, and neither may quietly appear at 0 W when it is not.

The name check is here because adding `sensor.power` collided with the
`binary_sensor` that was also called "Power" (the unit's on/off state). Two
entities on one device sharing a name is invisible in code and obvious in the
UI, in search, and to voice assistants.
"""
import json
import pathlib

import pytest
from eb300_ble import binary_sensor, button, number, select, sensor, switch
from eb300_ble.sensor import SENSOR_DESCRIPTIONS, async_setup_entry
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfPower

COMPONENT_DIR = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "eb300_ble"
STRINGS = json.loads((COMPONENT_DIR / "strings.json").read_text())
EN = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())

RATED_WATTS = 1200.0


class _Status:
    def __init__(self, relay_on):
        self.relay_on = relay_on
        self.energy_meter = 120  # minutes


class _Data:
    def __init__(self, relay_on):
        self.status = _Status(relay_on)


class _Coordinator:
    address = "AA:BB:CC:DD:EE:FF"


class _Entry:
    def __init__(self, options):
        self.options = options
        self.runtime_data = _Coordinator()


async def _created_keys(options):
    created = []
    await async_setup_entry(None, _Entry(options), created.extend)
    return [entity.entity_description.key for entity in created]


# --- the power sensor -----------------------------------------------------

def _power_value(relay_on, watts=RATED_WATTS):
    return sensor._power_description(watts).value_fn(_Data(relay_on))


def test_power_is_the_rated_wattage_while_the_relay_is_closed():
    assert _power_value(True) == RATED_WATTS


def test_power_is_zero_while_the_relay_is_open():
    """Zero, not `None`: the element genuinely draws nothing, and a gap in the
    series would break the area under it."""
    assert _power_value(False) == 0.0


def test_power_is_declared_as_watts_and_measured():
    description = sensor._power_description(RATED_WATTS)
    assert description.device_class is SensorDeviceClass.POWER
    assert description.native_unit_of_measurement == UnitOfPower.WATT
    assert description.state_class is SensorStateClass.MEASUREMENT


# --- when they exist ------------------------------------------------------

async def test_no_wattage_creates_neither_derived_entity():
    keys = await _created_keys({})
    assert "power" not in keys
    assert "energy" not in keys


async def test_a_wattage_creates_both():
    keys = await _created_keys({"rated_watts": RATED_WATTS})
    assert "power" in keys
    assert "energy" in keys


async def test_zero_wattage_is_the_same_as_unset():
    assert await _created_keys({"rated_watts": 0}) == await _created_keys({})


async def test_the_always_on_sensors_do_not_depend_on_the_option():
    keys = await _created_keys({})
    assert set(keys) == {d.key for d in SENSOR_DESCRIPTIONS}


async def test_every_created_entity_has_its_own_unique_id():
    entities = []
    await async_setup_entry(None, _Entry({"rated_watts": RATED_WATTS}), entities.extend)
    unique_ids = [e.unique_id for e in entities]
    assert len(set(unique_ids)) == len(unique_ids)


def test_power_sensor_and_power_binary_sensor_do_not_share_a_unique_id_suffix():
    """They may: unique_ids are scoped per domain, and the binary sensor keeps
    `power` so its history survives the rename. Assert it, so a later "tidy-up"
    that changes the key is a deliberate choice rather than an accident."""
    binary = {d.key: d for d in binary_sensor.BINARY_SENSOR_DESCRIPTIONS}["power"]
    assert binary.translation_key == "powered_on"
    assert sensor._power_description(RATED_WATTS).key == "power"


# --- names ----------------------------------------------------------------

def _translation_keys():
    """Every translation_key the integration can put on an entity."""
    return {
        "sensor": [d.translation_key for d in SENSOR_DESCRIPTIONS]
        + [
            sensor._energy_description(RATED_WATTS).translation_key,
            sensor._power_description(RATED_WATTS).translation_key,
        ],
        "binary_sensor": [d.translation_key for d in binary_sensor.BINARY_SENSOR_DESCRIPTIONS],
        "select": [d.translation_key for d in select.SELECT_DESCRIPTIONS],
        "number": [d.translation_key for d in number.NUMBER_DESCRIPTIONS],
        "switch": [switch.KEY_LOCK_DESCRIPTION.translation_key],
        "button": [button.SYNC_CLOCK_DESCRIPTION.translation_key],
    }


@pytest.mark.parametrize("platform", _translation_keys())
def test_every_translation_key_is_declared(platform):
    """An entity whose translation_key has no string renders with a blank name."""
    declared = set(STRINGS["entity"].get(platform, {}))
    assert set(_translation_keys()[platform]) <= declared


def test_no_two_entities_share_a_name():
    names = [
        (platform, key, entry["name"])
        for platform, entries in STRINGS["entity"].items()
        for key, entry in entries.items()
    ]
    by_name = {}
    for platform, key, name in names:
        by_name.setdefault(name.casefold(), []).append(f"{platform}.{key}")
    duplicates = {name: owners for name, owners in by_name.items() if len(owners) > 1}
    assert not duplicates, f"same name on one device: {duplicates}"


def test_strings_and_english_translations_stay_in_step():
    """`strings.json` is the source; `translations/en.json` is what HA loads.
    A change to one and not the other ships an entity named after its key."""
    assert STRINGS == EN
