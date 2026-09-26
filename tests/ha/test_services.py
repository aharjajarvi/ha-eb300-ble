"""The two registered service handlers, called the way HA calls them.

`test_schema.py`, `test_shorthand.py` and `test_resolve.py` already cover the
pieces -- validation, the multi-day shorthand, target resolution -- but each in
isolation. Nothing until now called `eb300_ble.set_home_program` end to end,
which is the only way to check that the pieces are wired to each other and that
`supports_response` is declared correctly on the one service that returns data.

The pre-flight temperature check is the substance here: a bad temperature is
rejected with *zero* BLE traffic, rather than after connecting, reading the
current schedule and being refused by the device.
"""
from unittest.mock import AsyncMock, patch

import pytest
from eb300_ble.const import DOMAIN, SERVICE_GET_HOME_PROGRAM, SERVICE_SET_HOME_PROGRAM, WEEKDAYS
from eb300_ble.coordinator import EB300Coordinator
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from test_coordinator import _program

ENTITY_ID = "climate.eb_therm_300_123456"


@pytest.fixture
def platforms():
    return [Platform.CLIMATE]


@pytest.fixture
def target():
    return {"entity_id": [ENTITY_ID]}


async def test_both_services_are_registered(hass, loaded_entry):
    assert hass.services.has_service(DOMAIN, SERVICE_GET_HOME_PROGRAM)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_HOME_PROGRAM)


async def test_get_returns_a_response_and_set_does_not(hass, loaded_entry):
    """`get` is `SupportsResponse.ONLY` -- a script that calls it without
    `response_variable` is a mistake HA should catch, and `set` must not be
    declared as returning anything."""
    services = hass.services.async_services_for_domain(DOMAIN)

    assert services[SERVICE_GET_HOME_PROGRAM].supports_response is SupportsResponse.ONLY
    assert services[SERVICE_SET_HOME_PROGRAM].supports_response is SupportsResponse.NONE


async def test_get_returns_all_seven_days(hass, loaded_entry, target):
    with patch.object(EB300Coordinator, "async_get_home_program", AsyncMock(return_value=_program())):
        response = await hass.services.async_call(
            DOMAIN, SERVICE_GET_HOME_PROGRAM, target, blocking=True, return_response=True
        )

    assert list(response) == list(WEEKDAYS)
    assert response["monday"][0] == {"time": "06:00", "temperature": 22.0, "active": True}


async def test_set_reaches_the_coordinator_with_the_folded_updates(hass, loaded_entry, target):
    """The shorthand is folded before the coordinator sees it, so the merge only
    ever deals with one shape."""
    write = AsyncMock()
    with patch.object(EB300Coordinator, "async_set_home_program", write):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOME_PROGRAM,
            {**target, "days": ["monday", "friday"], "events": [{"time": "06:00", "temperature": 21.0}]},
            blocking=True,
        )

    updates = write.await_args.args[0]
    assert set(updates) == {"monday", "friday"}
    assert updates["monday"] == [{"time": "06:00", "temperature": 21.0, "active": True}]


@pytest.mark.parametrize("temperature", [4.5, 35.5])
async def test_an_out_of_range_temperature_is_refused_before_any_ble_traffic(
    hass, loaded_entry, target, temperature
):
    """The common invalid-schedule case, caught without holding a connection
    slot. Constraints that depend on the device's *existing* schedule still
    cost one GET; this one costs nothing."""
    write = AsyncMock()
    with (
        patch.object(EB300Coordinator, "async_set_home_program", write),
        pytest.raises(ServiceValidationError, match="Monday"),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOME_PROGRAM,
            {**target, "monday": [{"time": "06:00", "temperature": temperature}]},
            blocking=True,
        )

    write.assert_not_awaited()


async def test_a_half_degree_step_violation_is_refused_too(hass, loaded_entry, target):
    """The device stores decidegrees but only accepts 0.5 C steps."""
    with (
        patch.object(EB300Coordinator, "async_set_home_program", AsyncMock()) as write,
        pytest.raises(ServiceValidationError),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOME_PROGRAM,
            {**target, "monday": [{"time": "06:00", "temperature": 21.3}]},
            blocking=True,
        )

    write.assert_not_awaited()


async def test_the_services_exist_without_any_thermostat_loaded(hass, registered_integration):
    """Registered in `async_setup`, not per entry: with no entry loaded an
    automation calling one gets "not loaded" instead of "action not found"."""
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})

    assert hass.services.has_service(DOMAIN, SERVICE_GET_HOME_PROGRAM)
    assert hass.services.has_service(DOMAIN, SERVICE_SET_HOME_PROGRAM)


async def test_events_out_of_order_are_refused_before_any_ble_traffic(hass, loaded_entry, target):
    """Order among the given events is the caller's; it is checked with zero
    BLE traffic, and the message names the weekday and the two events."""
    write = AsyncMock()
    with (
        patch.object(EB300Coordinator, "async_set_home_program", write),
        pytest.raises(ServiceValidationError, match=r"Monday: .*event 1 \(22:00\) comes after event 2 \(06:00\)"),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOME_PROGRAM,
            {**target, "monday": [
                {"time": "22:00", "temperature": 17.0},
                {"time": "06:00", "temperature": 22.0},
            ]},
            blocking=True,
        )

    write.assert_not_awaited()


async def test_an_event_after_midnight_is_in_order(hass, loaded_entry, target):
    """The schedule day runs 02:00 -> 01:59, so 01:00 sorts after 23:00."""
    write = AsyncMock()
    with patch.object(EB300Coordinator, "async_set_home_program", write):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_HOME_PROGRAM,
            {**target, "monday": [
                {"time": "23:00", "temperature": 17.0},
                {"time": "01:00", "temperature": 16.0},
            ]},
            blocking=True,
        )

    write.assert_awaited_once()


async def test_an_unloaded_thermostat_is_reported_as_such(hass, loaded_entry, target):
    """The entity registry outlives the config entry's `runtime_data`, so a
    service call against an unloaded entry finds an entity with no coordinator
    behind it."""
    await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError, match="not loaded"):
        await hass.services.async_call(
            DOMAIN, SERVICE_SET_HOME_PROGRAM,
            {**target, "monday": [{"time": "06:00", "temperature": 21.0}]},
            blocking=True,
        )
