"""The four write-path rules, as regression tests.

docs/HARDWARE_NOTES.md#write-path-design-rules records four rules that came out
of one test case -- "write while the device is unreachable" -- across five
attended hardware cycles, three of which found a bug introduced by the previous
cycle's fix. AGENTS.md marks this code as not-to-be-casually-refactored on the
strength of that history, and until now none of it was covered: an "obvious"
simplification would have passed the whole suite.

Every test here is named for the failure mode it prevents, not for the method
it calls, because the method names are exactly what a refactor would change.

Rule 1 (one cancellable task owns both the debounce wait and the write) is
pinned here; rules 3 and 4 live at the coordinator boundary and are pinned in
test_coordinator.py. Rule 2 is not tested, deliberately -- see the comment above
`test_the_newest_value_is_shown_immediately_not_after_the_write` for why it
cannot be.

What this file does NOT establish: burst cancellation has still never been
exercised against a real thermostat. These tests pin the logic, not the radio,
and HARDWARE_NOTES.md's "known gaps" section stays accurate on that point.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from conftest import make_data, make_status
from eb300_ble import climate as climate_module
from eb300_ble.coordinator import EB300Coordinator
from eb300_ble.eb300_ble.const import Program
from eb300_ble.eb300_ble.exceptions import EB300Error
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.const import Platform
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

ENTITY_ID = "climate.eb_therm_300_123456"

# Short enough to keep the suite fast, long enough that a burst of edits made in
# one event-loop turn cannot straddle it.
DEBOUNCE = 0.15


@pytest.fixture
def platforms():
    return [Platform.CLIMATE]


@pytest.fixture(autouse=True)
def short_debounce(monkeypatch):
    """`climate.py` imports the constant by name, so that is the patch target."""
    monkeypatch.setattr(climate_module, "CLIMATE_SET_TEMPERATURE_DEBOUNCE_SECONDS", DEBOUNCE)


@pytest.fixture
def entity(hass, loaded_entry):
    return hass.data["entity_components"]["climate"].get_entity(ENTITY_ID)


@pytest.fixture
def writes(monkeypatch):
    """Record every setpoint that actually reaches the coordinator."""
    mock = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, "async_set_override_temp", mock)
    return mock


def _written(writes):
    return [call.args[0] for call in writes.await_args_list]


async def _past_the_debounce():
    await asyncio.sleep(DEBOUNCE * 3)


async def _edit(entity, celsius):
    """Drive the entity directly, in one event-loop turn.

    `async_set_temperature` has no suspension point before it creates its task,
    so a run of these is atomic with respect to the debounce timer -- which is
    the point of a burst test, and is not guaranteed if each edit goes through
    `hass.services.async_call`.
    """
    await entity.async_set_temperature(temperature=celsius)


# --- Rule 1: one cancellable task owns the wait and the write --------------


async def test_a_burst_of_edits_becomes_one_write_carrying_the_last_value(hass, entity, writes):
    """Dragging the card slider must not put every intermediate value on the air.

    This is the test docs/HARDWARE_NOTES.md claimed existed and did not.
    """
    await _edit(entity, 21.0)
    await _edit(entity, 22.0)
    await _edit(entity, 23.5)

    assert writes.await_count == 0, "wrote before the debounce elapsed"
    await _past_the_debounce()

    assert _written(writes) == [235]


async def test_a_newer_edit_preempts_a_write_already_in_flight(hass, entity, monkeypatch):
    """The stale-write bug, observed on hardware.

    A BLE write retrying against an unreachable device can take 1-2+ minutes to
    give up. Without cancellation, a newer edit does not stop it, and whichever
    write catches the device reconnecting wins -- which can silently apply a
    superseded setpoint.
    """
    started: list[int] = []
    finished: list[int] = []
    release = asyncio.Event()

    async def _hangs(self, decideg):
        started.append(decideg)
        await release.wait()
        finished.append(decideg)

    monkeypatch.setattr(EB300Coordinator, "async_set_override_temp", _hangs)

    await _edit(entity, 21.0)
    await _past_the_debounce()
    assert started == [210], "the first write should be in flight by now"

    await _edit(entity, 22.0)
    await _past_the_debounce()

    # The superseded write never got past the point where it was cancelled...
    assert started == [210, 220]
    assert finished == []

    # ...and the value that does land is the newest one, not the stale one.
    release.set()
    await asyncio.sleep(0)
    assert finished == [220]


async def test_removing_the_entity_cancels_a_pending_write(hass, loaded_entry, entity, writes):
    """`async_will_remove_from_hass` is the other caller of `_cancel_pending_write`."""
    await _edit(entity, 24.0)

    await hass.config_entries.async_unload(loaded_entry.entry_id)
    await hass.async_block_till_done()
    await _past_the_debounce()

    assert writes.await_count == 0


# --- The optimistic value ------------------------------------------------
#
# NOT rule 2. Rule 2 ("store the pending value *before* cancelling the previous
# task") is unobservable in the current design, and no test here pins it:
# `async_set_temperature` runs both statements with no await between them, and
# `_cancel_pending_write` only calls `Task.cancel()`, which schedules
# cancellation rather than running the victim. The cancelled task therefore
# cannot execute anything in between whichever order the two lines are in.
# Confirmed by mutation: swapping them makes nothing in either suite fail.
#
# The rule was real in the *earlier* split design (an `async_call_later` timer
# plus a separate write task), where the cancel path could reach a callback
# synchronously. It survives as a comment in `climate.py` describing a hazard
# the single-task rewrite removed. Left alone here rather than tested, because
# a test that passes under both orderings would claim coverage it does not have
# -- see docs/HARDWARE_NOTES.md, where this is now recorded.
#
# What *is* observable, and is tested below: the newest requested value is what
# the UI shows during the debounce window.


async def test_the_newest_value_is_shown_immediately_not_after_the_write(hass, entity, writes):
    """The card must show what the user just asked for, not the stale setpoint,
    for the whole debounce window."""
    await _edit(entity, 21.0)
    await _edit(entity, 26.5)
    # Deliberately no `async_block_till_done()`: it would wait out the debounce
    # sleep and let the write happen, which is the opposite of what is under
    # test here. `async_write_ha_state` updates the state machine synchronously.

    assert hass.states.get(ENTITY_ID).attributes["temperature"] == 26.5
    assert writes.await_count == 0  # still optimistic; nothing on the air yet


async def test_the_optimistic_value_clears_when_the_write_starts_not_when_it_fails(
    hass, entity, monkeypatch
):
    """A BLE connect can burn minutes exhausting retries. HA must not keep
    displaying an unapplied setpoint for that whole window."""
    release = asyncio.Event()

    async def _hangs(self, decideg):
        await release.wait()

    monkeypatch.setattr(EB300Coordinator, "async_set_override_temp", _hangs)

    await _edit(entity, 26.5)
    assert hass.states.get(ENTITY_ID).attributes["temperature"] == 26.5

    await _past_the_debounce()

    # The write is still hanging, but the displayed value is the device's truth.
    assert hass.states.get(ENTITY_ID).attributes["temperature"] == 20.0

    release.set()
    await asyncio.sleep(0)


# --- A failed write, as seen from the entity ------------------------------


@pytest.mark.parametrize(
    "error",
    [HomeAssistantError("unreachable"), EB300Error("device said no"), TimeoutError()],
    ids=["home_assistant_error", "eb300_error", "bare_timeout"],
)
async def test_a_failed_debounced_write_warns_instead_of_raising(hass, entity, monkeypatch, caplog, error):
    """A debounced write is detached from the service call that triggered it.

    There is nothing to raise to, so the only honest outcomes are a log line and
    the state revert. Each of the three exception types is caught deliberately:
    the coordinator translates to `HomeAssistantError` for the non-debounced
    callers, `EB300Error` can still arrive from a direct path, and a bare
    `TimeoutError` is neither.
    """
    monkeypatch.setattr(EB300Coordinator, "async_set_override_temp", AsyncMock(side_effect=error))

    await _edit(entity, 22.0)
    await _past_the_debounce()

    assert "Failed to set temperature" in caplog.text
    assert hass.states.get(ENTITY_ID).state != "unavailable"


async def test_set_temperature_without_a_temperature_is_a_no_op(hass, entity, writes):
    """`set_temperature` can arrive carrying only target_temp_high/low."""
    await entity.async_set_temperature(target_temp_high=25.0, target_temp_low=18.0)
    await _past_the_debounce()

    assert writes.await_count == 0


# --- The non-debounced writes ---------------------------------------------


@pytest.mark.parametrize(("mode", "expected"), [(HVACMode.HEAT, True), (HVACMode.OFF, False)])
async def test_hvac_mode_maps_to_the_power_flag(entity, monkeypatch, mode, expected):
    power = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, "async_set_power", power)

    await entity.async_set_hvac_mode(mode)

    power.assert_awaited_once_with(expected)


async def test_an_unsupported_hvac_mode_is_rejected(entity):
    with pytest.raises(ServiceValidationError, match="Unsupported hvac_mode"):
        await entity.async_set_hvac_mode(HVACMode.COOL)


@pytest.mark.parametrize(("preset", "expected"), [("manual", Program.MANUAL), ("home", Program.HOME)])
async def test_preset_mode_maps_to_the_program(entity, monkeypatch, preset, expected):
    program = AsyncMock()
    monkeypatch.setattr(EB300Coordinator, "async_set_program", program)

    await entity.async_set_preset_mode(preset)

    program.assert_awaited_once_with(expected)


async def test_an_unsupported_preset_mode_is_rejected(entity):
    with pytest.raises(ServiceValidationError, match="Unsupported preset_mode"):
        await entity.async_set_preset_mode("away")


# --- The read-side properties ---------------------------------------------


@pytest.mark.parametrize(
    ("power_off", "relay_on", "mode", "action"),
    [
        (False, True, HVACMode.HEAT, HVACAction.HEATING),
        (False, False, HVACMode.HEAT, HVACAction.IDLE),
        (True, False, HVACMode.OFF, HVACAction.OFF),
        # Powered off with the relay still reported closed: OFF wins. The device
        # reports a stale relay bit for a poll or two after a power-off.
        (True, True, HVACMode.OFF, HVACAction.OFF),
    ],
)
def test_mode_and_action_are_read_from_the_status(hass, power_off, relay_on, mode, action):
    coordinator = type("C", (), {"address": "AA:BB", "data": make_data(make_status(power_off=power_off, relay_on=relay_on))})()
    entity = climate_module.EB300Climate(coordinator, use_room_sensor=False)

    assert entity.hvac_mode is mode
    assert entity.hvac_action is action


@pytest.mark.parametrize(("program", "expected"), [(0, "manual"), (1, "home")])
def test_preset_mode_is_read_from_the_status(program, expected):
    coordinator = type("C", (), {"address": "AA:BB", "data": make_data(make_status(current_program=program))})()

    assert climate_module.EB300Climate(coordinator, use_room_sensor=False).preset_mode == expected
