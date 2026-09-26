"""Adding a thermostat: discovery, the device picker, the key step, options.

`test_psk_rotation.py` covers replacing the key on an existing entry. This file
covers the rest of the flow, and the one thing about key validation that is not
visible from the form: it connects over the coordinator's own path, so it waits
its turn behind a poll instead of competing with it for a proxy slot.
"""
import asyncio
import base64
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eb300_ble import coordinator as coordinator_module
from eb300_ble.config_flow import CONF_ADDRESS, CannotConnect, InvalidAuth, _validate_and_fetch_device_info
from eb300_ble.const import (
    CONF_POLL_INTERVAL,
    CONF_PSK,
    CONF_RATED_WATTS,
    CONF_USE_ROOM_SENSOR,
    CONNECT_RETRY_ATTEMPTS,
    DOMAIN,
)
from eb300_ble.eb300_ble.const import MANUFACTURER_ID, SERVICE_DATA_ACCESS
from eb300_ble.eb300_ble.exceptions import EB300ConnectionError, HandshakeError
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.device_registry import format_mac
from pytest_homeassistant_custom_component.common import MockConfigEntry

ADDRESS = "AA:BB:CC:DD:EE:FF"
PSK = bytes(range(32))
PSK_B64 = base64.b64encode(PSK).decode()


@pytest.fixture(autouse=True)
def _loadable(registered_integration):
    """Every flow here is started by domain, so HA has to be able to find us."""


@pytest.fixture
def validated():
    """Stand in for the live handshake the flow does before accepting a key."""
    with patch(
        "eb300_ble.config_flow._validate_and_fetch_device_info",
        AsyncMock(return_value=MagicMock(serial="123456")),
    ) as mock:
        yield mock


@pytest.fixture
def no_setup():
    """A created entry would otherwise be set up for real and start polling."""
    with patch("eb300_ble.async_setup_entry", AsyncMock(return_value=True)) as mock:
        yield mock


def _advert(address, *, name="EB300", manufacturer_data=None, service_uuids=()):
    advert = MagicMock(
        address=address,
        manufacturer_data=manufacturer_data if manufacturer_data is not None else {},
        service_uuids=list(service_uuids),
    )
    advert.name = name  # MagicMock(name=...) names the mock, not the attribute
    return advert


def _visible(*adverts):
    return patch("eb300_ble.config_flow.async_discovered_service_info", return_value=list(adverts))


# --- the device picker ----------------------------------------------------


async def test_nothing_visible_aborts_rather_than_asking_for_an_address(hass):
    """Validation connects through HA's Bluetooth manager, so a thermostat no
    scanner can see would fail at the key step -- after the key was pasted."""
    with _visible():
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"


async def test_the_picker_matches_what_the_manifest_discovers(hass):
    """Manufacturer ID or service UUID, as in manifest.json -- not the name, and
    not a thermostat that is already configured."""
    configured = "11:22:33:44:55:66"
    MockConfigEntry(domain=DOMAIN, unique_id=format_mac(configured), data={}).add_to_hass(hass)
    adverts = [
        _advert(ADDRESS, manufacturer_data={MANUFACTURER_ID: b"\x00"}),
        _advert("AA:BB:CC:DD:EE:01", name=None, service_uuids=[SERVICE_DATA_ACCESS]),
        _advert("AA:BB:CC:DD:EE:02", name="EB300 lookalike"),
        _advert(configured, manufacturer_data={MANUFACTURER_ID: b"\x00"}),
    ]
    with _visible(*adverts):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    assert result["type"] is FlowResultType.FORM
    offered = result["data_schema"].schema[CONF_ADDRESS].container
    assert set(offered) == {ADDRESS, "AA:BB:CC:DD:EE:01"}


async def test_picking_a_device_then_a_key_creates_the_entry(hass, validated, no_setup):
    with _visible(_advert(ADDRESS, manufacturer_data={MANUFACTURER_ID: b"\x00"})):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ADDRESS: ADDRESS})

    assert result["step_id"] == "psk"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: f" {PSK_B64}\n"})

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "EB-Therm 300 (123456)"
    assert result["data"] == {CONF_ADDRESS: ADDRESS, CONF_PSK: PSK_B64}
    assert result["result"].unique_id == format_mac(ADDRESS)
    assert validated.call_args.args[1:] == (ADDRESS, PSK)


# --- Bluetooth discovery -------------------------------------------------


async def test_a_discovered_thermostat_goes_straight_to_the_key(hass, validated, no_setup):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_advert(ADDRESS, name="EB300")
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "psk"
    assert result["description_placeholders"] == {"name": "EB300"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: PSK_B64})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_configured_thermostat_is_not_offered_again(hass):
    MockConfigEntry(domain=DOMAIN, unique_id=format_mac(ADDRESS), data={}).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_advert(ADDRESS)
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(("error", "shown"), [(InvalidAuth, "invalid_auth"), (CannotConnect, "cannot_connect")])
async def test_a_failed_handshake_keeps_the_form_open(hass, error, shown):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_advert(ADDRESS)
    )
    with patch("eb300_ble.config_flow._validate_and_fetch_device_info", AsyncMock(side_effect=error)):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: PSK_B64})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": shown}


async def test_a_malformed_key_is_caught_before_any_connection(hass, validated):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_advert(ADDRESS)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PSK: "not base64!"})

    assert result["errors"] == {CONF_PSK: "psk_not_base64"}
    validated.assert_not_called()


# --- options --------------------------------------------------------------


async def test_options_are_stored(hass):
    entry = MockConfigEntry(domain=DOMAIN, unique_id=format_mac(ADDRESS), data={CONF_ADDRESS: ADDRESS})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_POLL_INTERVAL: 120, CONF_RATED_WATTS: 850, CONF_USE_ROOM_SENSOR: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_POLL_INTERVAL: 120, CONF_RATED_WATTS: 850.0, CONF_USE_ROOM_SENSOR: True}


# --- validating a key: the coordinator's connection path, not a private one ---


async def test_validation_resolves_through_home_assistants_bluetooth(hass):
    """No bare-address connect: a thermostat no scanner can see fails without
    any scan of our own, and is retried like a poll would be."""
    with (
        patch(
            "eb300_ble.coordinator.bluetooth.async_ble_device_from_address", return_value=None
        ) as resolve,
        patch("eb300_ble.coordinator.BleakTransport") as transport,
        pytest.raises(CannotConnect),
    ):
        await _validate_and_fetch_device_info(hass, ADDRESS, PSK)

    assert resolve.call_count == CONNECT_RETRY_ATTEMPTS
    assert resolve.call_args.kwargs["connectable"] is True
    transport.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [EB300ConnectionError("gone"), HandshakeError("Timed out waiting for handshake response", step=0)],
    ids=["dropped link", "handshake timeout"],
)
async def test_a_flaky_link_is_a_connection_problem_not_a_key_problem(hass, error):
    """A handshake that timed out is auth-shaped but is not a rejection: telling
    the user their key is wrong would send them to re-request a good one."""
    with (
        patch("eb300_ble.config_flow.async_run_once", AsyncMock(side_effect=error)),
        pytest.raises(CannotConnect),
    ):
        await _validate_and_fetch_device_info(hass, ADDRESS, PSK)


async def test_a_rejected_key_is_not_retried(hass):
    rejected = HandshakeError("wrong PSK?", step=3, error_code=4)
    with (
        patch("eb300_ble.config_flow.async_run_once", AsyncMock(side_effect=rejected)) as run_once,
        pytest.raises(InvalidAuth),
    ):
        await _validate_and_fetch_device_info(hass, ADDRESS, PSK)

    assert run_once.call_count == 1


async def test_validation_waits_for_a_poll_that_holds_the_link(hass):
    """The shared lock is what keeps setup from taking a second proxy slot while
    another thermostat is mid-poll."""
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.read_device_info = AsyncMock(return_value=MagicMock(serial="123456"))

    with (
        patch("eb300_ble.coordinator.bluetooth.async_ble_device_from_address", return_value=MagicMock()),
        patch("eb300_ble.coordinator.BleakTransport"),
        patch("eb300_ble.coordinator.EB300Client", return_value=client),
    ):
        async with coordinator_module._CONNECTION_SEMAPHORE:
            task = asyncio.ensure_future(_validate_and_fetch_device_info(hass, ADDRESS, PSK))
            for _ in range(5):
                await asyncio.sleep(0)
            client.connect.assert_not_awaited()

        info = await task

    assert info.serial == "123456"
    client.connect.assert_awaited_once()
    client.disconnect.assert_awaited_once()
