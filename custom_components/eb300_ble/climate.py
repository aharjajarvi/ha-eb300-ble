"""Climate entity. See docs/ARCHITECTURE.md for the entity map.

Writes go through PID 0x10D0 (Override Temperature) by default: it takes
immediate effect regardless of the active program, matching what a user
dragging the HA thermostat card slider expects. See the plan for why 0x1082
(Manual Control Temp) was rejected as the default — it silently does nothing
while the Home program is active.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityDescription,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EB300ConfigEntry
from .const import (
    CLIMATE_SET_TEMPERATURE_DEBOUNCE_SECONDS,
    CONF_USE_ROOM_SENSOR,
    MAX_TARGET_TEMP_C,
    MIN_TARGET_TEMP_C,
    TARGET_TEMP_STEP_C,
)
from .coordinator import EB300Coordinator
from .eb300_ble.const import Program
from .eb300_ble.exceptions import EB300Error
from .entity import EB300Entity

_LOGGER = logging.getLogger(__name__)

_PRESET_MANUAL = "manual"
_PRESET_HOME = "home"
_PRESET_TO_PROGRAM = {_PRESET_MANUAL: Program.MANUAL, _PRESET_HOME: Program.HOME}


async def async_setup_entry(
    hass: HomeAssistant, entry: EB300ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    use_room_sensor = bool(entry.options.get(CONF_USE_ROOM_SENSOR, False))
    async_add_entities([EB300Climate(coordinator, use_room_sensor=use_room_sensor)])


class EB300Climate(EB300Entity, ClimateEntity):
    _attr_name = None  # single climate entity per device: use the device name
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]  # noqa: RUF012 - ClimateEntity declares these as instance attrs
    _attr_preset_modes = [_PRESET_MANUAL, _PRESET_HOME]  # noqa: RUF012
    _attr_min_temp = MIN_TARGET_TEMP_C
    _attr_max_temp = MAX_TARGET_TEMP_C
    _attr_target_temperature_step = TARGET_TEMP_STEP_C
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, coordinator: EB300Coordinator, *, use_room_sensor: bool) -> None:
        super().__init__(coordinator, ClimateEntityDescription(key="thermostat"))
        self._use_room_sensor = use_room_sensor
        self._write_task: asyncio.Task[None] | None = None
        self._pending_temperature_c: float | None = None

    async def async_will_remove_from_hass(self) -> None:
        self._cancel_pending_write()
        await super().async_will_remove_from_hass()

    def _cancel_pending_write(self) -> None:
        # A BLE write stuck retrying against an unreachable device can take
        # 1-2+ minutes to give up. Without this, a newer edit made while an
        # older one is still retrying doesn't stop it — whichever write
        # happens to catch the device reconnecting wins, which can silently
        # apply a stale, superseded value (observed on real hardware,
        # docs/HARDWARE_NOTES.md). Cancelling here means a new edit always
        # preempts an in-flight older one, both in the UI and on the device.
        #
        # One handle covers both phases of an edit — the debounce wait and the
        # write itself — because _debounce_and_write owns both. The earlier
        # split (async_call_later timer + separate write task) needed two
        # cancel paths and put the flush callback on a worker thread; see
        # docs/HARDWARE_NOTES.md
        if self._write_task is not None and not self._write_task.done():
            self._write_task.cancel()
        self._write_task = None

    @property
    def current_temperature(self) -> float | None:
        """`None` when the device cannot read the sensor this entity follows.

        The status struct still carries a temperature for an unreadable sensor,
        and it is a placeholder (20.0 C on the tested firmware), not a
        measurement — see sensor.py, where the same rule is applied to the
        floor and room temperature sensors. A thermostat wired without a floor
        sensor is a supported installation, so the default floor-following
        climate entity would otherwise show a convincing, permanent 20.0 C.
        """
        status = self.coordinator.data.status
        if self._use_room_sensor:
            return None if status.room_sensor_error else status.room_temperature_c
        return None if status.floor_sensor_error else status.floor_temperature_c

    @property
    def target_temperature(self) -> float:
        if self._pending_temperature_c is not None:
            return self._pending_temperature_c
        return self.coordinator.data.status.current_set_temperature_c

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode.OFF if self.coordinator.data.status.power_off else HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction:
        status = self.coordinator.data.status
        if status.power_off:
            return HVACAction.OFF
        return HVACAction.HEATING if status.relay_on else HVACAction.IDLE

    @property
    def preset_mode(self) -> str:
        return self.coordinator.data.status.program.name.lower()

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return

        # Show the new value optimistically for the debounce window, then hand
        # off to a single task that owns both the wait and the write.
        #
        # Order matters: the pending value is stored *before* cancelling the
        # previous task. A task being cancelled is suspended at an await and
        # cannot run another statement in between, so it can never clear the
        # value stored on the line above.
        self._pending_temperature_c = float(temperature)
        self.async_write_ha_state()
        self._cancel_pending_write()
        self._write_task = self.hass.async_create_task(
            self._debounce_and_write(float(temperature))
        )

    async def _debounce_and_write(self, temperature_c: float) -> None:
        try:
            # Coalesce a burst of slider drags into a single BLE write, sent
            # CLIMATE_SET_TEMPERATURE_DEBOUNCE_SECONDS after the last call —
            # A newer edit cancels this task before the sleep
            # returns, so only the last value in a burst is ever written.
            await asyncio.sleep(CLIMATE_SET_TEMPERATURE_DEBOUNCE_SECONDS)

            # Stop showing the optimistic value the instant the write is
            # actually attempted, not only once it definitively fails: a BLE
            # connect can take a minute or more to exhaust its retries
            # (coordinator-level retries on top of bleak_retry_connector's own
            # internal ones), and HA shouldn't keep displaying an unapplied
            # setpoint for that whole window. If the write succeeds shortly
            # after, the coordinator's post-write refresh
            # (async_set_override_temp -> async_request_refresh) overwrites
            # this with the real, now-current value almost immediately.
            self._pending_temperature_c = None
            self.async_write_ha_state()

            try:
                await self.coordinator.async_set_override_temp(round(temperature_c * 10))
            except (HomeAssistantError, EB300Error, TimeoutError) as exc:
                # Nothing to raise to: a debounced write is detached from the
                # service call that triggered it, so a failure can only surface
                # as a log line plus the state revert already pushed above.
                # HomeAssistantError is in the tuple because the coordinator
                # translates write failures for the non-debounced callers.
                _LOGGER.warning(
                    "Failed to set temperature on %s: %s", self.coordinator.address, exc
                )
        finally:
            # Only clear the handle if it still points at this task: a newer
            # edit may already have stored its own, and an older task can
            # unwind late (its cancellation has to propagate through the
            # coordinator's `finally: await client.disconnect()`).
            if self._write_task is asyncio.current_task():
                self._write_task = None

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if hvac_mode not in self._attr_hvac_modes:
            raise ValueError(f"Unsupported hvac_mode {hvac_mode}")
        await self.coordinator.async_set_power(hvac_mode != HVACMode.OFF)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        program = _PRESET_TO_PROGRAM.get(preset_mode)
        if program is None:
            raise ValueError(f"Unsupported preset_mode {preset_mode!r}")
        await self.coordinator.async_set_program(program)
