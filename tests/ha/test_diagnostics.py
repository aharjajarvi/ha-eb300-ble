"""Diagnostics must never carry the PSK.

AGENTS.md rule 5: "A PSK is a device credential; `diagnostics.py` redacts it and
must keep doing so." Diagnostics downloads get pasted verbatim into public
GitHub issues, so this is a credential-disclosure guarantee that until now
rested entirely on nobody editing a set literal.

The address and the serial are redacted for the same reason -- the serial *is*
the device MAC (docs/HARDWARE_NOTES.md, "Device info is not what you would
guess").
"""
import json

import pytest
from conftest import ADDRESS, PSK_B64, make_data, make_status
from eb300_ble.diagnostics import _sensor_error, async_get_config_entry_diagnostics
from eb300_ble.eb300_ble.const import SensorErrorCode
from homeassistant.const import Platform

REDACTED = "**REDACTED**"


@pytest.fixture
def platforms():
    return [Platform.SENSOR]


@pytest.fixture
async def payload(hass, loaded_entry):
    return await async_get_config_entry_diagnostics(hass, loaded_entry)


# --- the credential ------------------------------------------------------


async def test_the_psk_appears_nowhere_in_the_payload(payload):
    """Asserted against the whole serialised document, not just the field it is
    known to live in: a new field that happens to carry the key would pass a
    field-level check and still leak it."""
    assert PSK_B64 not in json.dumps(payload)


async def test_the_psk_field_is_redacted_rather_than_dropped(payload):
    """Present-but-redacted, so a bug report still shows a key *was* configured."""
    assert payload["entry_data"]["psk"] == REDACTED


async def test_the_mac_appears_nowhere_either(payload):
    """Both spellings of it: the entry address and the device serial."""
    serialised = json.dumps(payload)
    assert ADDRESS not in serialised
    assert "123456" not in serialised
    assert payload["entry_data"]["address"] == REDACTED
    assert payload["device_info"]["serial"] == REDACTED


async def test_the_fields_that_make_a_report_useful_survive(payload):
    """Redaction that took the diagnostics with it would be its own bug."""
    assert payload["device_info"]["firmware_version"] == "1.2"
    assert payload["device_info"]["batch"] == "2603"
    assert payload["last_status"]["current_set_temperature_c"] == 20.0
    assert payload["config"]["language"] == "ENGLISH"
    assert payload["config"]["calibration_room_c"] == 0.5
    assert payload["rssi"] == -60


async def test_the_payload_is_json_serialisable(payload):
    """HA serialises this to send it to the browser. An enum or a dataclass
    left in place fails there, where the user sees a download that never
    arrives rather than an error."""
    assert json.loads(json.dumps(payload)) == payload


# --- the sensor error byte ------------------------------------------------


@pytest.mark.parametrize("code", list(SensorErrorCode))
def test_every_known_sensor_error_is_named(code):
    """A non-zero code here is *why* a temperature entity reads `unknown`
    (sensor.py suppresses the device's 20.0 C placeholder). Without the name in
    diagnostics, a bug report shows an unexplained gap in history."""
    assert _sensor_error(int(code)) == code.name.lower()


def test_an_unknown_sensor_error_is_reported_not_swallowed():
    """Firmware may add codes. `SensorErrorCode(9)` would raise and take the
    whole diagnostics download with it."""
    assert _sensor_error(9) == "unknown_9"


async def test_a_faulted_sensor_shows_up_in_the_payload(hass, loaded_entry):
    """The end this exists for: the report says which sensor is faulted."""
    loaded_entry.runtime_data.data = make_data(
        make_status(floor_error=int(SensorErrorCode.OPEN_CIRCUIT))
    )
    payload = await async_get_config_entry_diagnostics(hass, loaded_entry)

    assert payload["last_status"]["floor_sensor_error"] == "open_circuit"
    assert payload["last_status"]["room_sensor_error"] == "ok"
