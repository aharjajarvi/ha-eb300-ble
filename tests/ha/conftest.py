import base64
import json
import pathlib
from unittest.mock import AsyncMock, patch

import pytest
from eb300_ble.config_flow import CONF_ADDRESS
from eb300_ble.const import CONF_PSK, DOMAIN
from eb300_ble.coordinator import EB300Coordinator, EB300Data
from eb300_ble.eb300_ble.const import KeyLock, Language, ScreensaverType
from eb300_ble.eb300_ble.models import DeviceInfo, ThermostatStatus
from homeassistant import loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.device_registry import format_mac
from pytest_homeassistant_custom_component.common import MockConfigEntry

COMPONENT_DIR = pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "eb300_ble"

ADDRESS = "AA:BB:CC:DD:EE:FF"
PSK_B64 = base64.b64encode(bytes(range(32))).decode()

PLACEHOLDER_DECIDEG = 200  # what the device reports for a sensor it cannot read


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def registered_integration(hass):
    """Make HA able to load this component by domain (config flows, entry setup).

    HA discovers custom integrations by scanning `<config>/custom_components`,
    which in this harness is a directory inside pytest-homeassistant-custom-
    component's own package -- our component is not there, it is staged onto
    `PYTHONPATH` as a top-level `eb300_ble` package (see run.sh). Registering it
    in the loader cache directly is what keeps that staging honest: `pkg_path`
    is the same `eb300_ble` the tests import and patch, so there is exactly one
    copy of the module in play. Symlinking it under a `custom_components/`
    config dir instead would import it a second time under a second name, and
    patches applied to one would not be seen by the other.
    """
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text())
    integration = loader.Integration(
        hass,
        "eb300_ble",
        COMPONENT_DIR,
        manifest,
        {path.name for path in COMPONENT_DIR.iterdir()},
    )
    hass.data[loader.DATA_CUSTOM_COMPONENTS] = {integration.domain: integration}
    return integration


# ── A plausible device state ──────────────────────────────────────────────
#
# One definition, shared. Tests that care about a particular field pass it in;
# everything else stays at values captured from the reference device (firmware
# 1.2, batch 2603), so a test never has to invent numbers it does not care
# about.


def make_status(
    *,
    floor_error: int = 0,
    room_error: int = 0,
    floor: int = PLACEHOLDER_DECIDEG,
    room: int = 233,
    relay_on: bool = False,
    power_off: bool = False,
    current_set_temperature: int = 200,
    current_program: int = 1,
) -> ThermostatStatus:
    return ThermostatStatus(
        error_flags=0,
        current_set_temperature=current_set_temperature,
        limiting_temperature=270,
        time_to_target=0,
        relay_on=relay_on,
        in_error_state=False,
        limited_by_limiting_sensor=False,
        power_off=power_off,
        room_temperature=room,
        floor_temperature=floor,
        relay_temperature=332,
        room_sensor_error=room_error,
        floor_sensor_error=floor_error,
        current_program=current_program,
        energy_meter=518,
    )


def make_data(status: ThermostatStatus | None = None, **overrides) -> EB300Data:
    fields = {
        "status": status if status is not None else make_status(),
        "device_info": DeviceInfo(
            model="EB-Therm 300", batch="2603", serial="123456", firmware_version="1.2"
        ),
        "rssi": -60,
        "key_lock": KeyLock.UNLOCKED,
        "language": Language.ENGLISH,
        "screensaver": ScreensaverType.TIME_TEMP,
        "calibration_room_decideg": 5,
        "calibration_floor_decideg": -3,
    }
    return EB300Data(**{**fields, **overrides})


@pytest.fixture
def eb300_data() -> EB300Data:
    return make_data()


# ── A fully set-up config entry ───────────────────────────────────────────


@pytest.fixture
def platforms():
    """Override in a test module to load only the platform under test.

    Setting up all seven costs a second or so per test and pulls in entities
    the test does not look at. `@pytest.mark.parametrize("platforms", [[Platform.CLIMATE]], indirect=True)`
    -- or a module-level `platforms` fixture -- narrows it.
    """
    return


@pytest.fixture
async def loaded_entry(hass, registered_integration, eb300_data, platforms):
    """A config entry set up for real, with the BLE poll replaced by canned data.

    `_async_update_data` is the patch point on purpose, not `_with_client`: it
    leaves the coordinator's own retry loop and error translation live, so a
    test that wants to exercise those (test_coordinator.py) still can, while a
    test that just wants entities gets them without any BLE machinery.
    """
    import eb300_ble

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=format_mac(ADDRESS),
        title="EB-Therm 300 (123456)",
        data={CONF_ADDRESS: ADDRESS, CONF_PSK: PSK_B64},
    )
    entry.add_to_hass(hass)

    stack = [
        patch.object(EB300Coordinator, "_async_update_data", AsyncMock(return_value=eb300_data)),
    ]
    if platforms is not None:
        stack.append(patch.object(eb300_ble, "PLATFORMS", platforms))

    for ctx in stack:
        ctx.start()
    try:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry

        # Unload rather than just dropping the entry: it is what runs
        # `async_will_remove_from_hass`, and therefore what cancels a write
        # still sitting in its debounce window. Without it a test that leaves
        # one pending trips pytest-homeassistant's lingering-task check.
        if entry.state is ConfigEntryState.LOADED:
            await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
    finally:
        for ctx in reversed(stack):
            ctx.stop()
