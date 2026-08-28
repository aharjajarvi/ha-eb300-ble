"""Sensor calibration numbers. See docs/ARCHITECTURE.md for the entity map.

0x10B2 is written as a single s16[3] triplet (room, floor, relay — relay is
always forced to 0 by the device), so each entity's set_native_value writes
back the *other* axis' last-known value alongside the one actually changed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EB300ConfigEntry
from .coordinator import EB300Data
from .entity import EB300Entity

CALIBRATION_MIN_C = -5.0
CALIBRATION_MAX_C = 5.0
CALIBRATION_STEP_C = 0.1


@dataclass(frozen=True, kw_only=True)
class EB300NumberDescription(NumberEntityDescription):
    current_fn: Callable[[EB300Data], int]  # decidegrees


NUMBER_DESCRIPTIONS: tuple[EB300NumberDescription, ...] = (
    EB300NumberDescription(
        key="room_calibration",
        translation_key="room_calibration",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=CALIBRATION_MIN_C,
        native_max_value=CALIBRATION_MAX_C,
        native_step=CALIBRATION_STEP_C,
        mode=NumberMode.BOX,
        current_fn=lambda data: data.calibration_room_decideg,
    ),
    EB300NumberDescription(
        key="floor_calibration",
        translation_key="floor_calibration",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        native_min_value=CALIBRATION_MIN_C,
        native_max_value=CALIBRATION_MAX_C,
        native_step=CALIBRATION_STEP_C,
        mode=NumberMode.BOX,
        current_fn=lambda data: data.calibration_floor_decideg,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EB300ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(EB300CalibrationNumber(coordinator, description) for description in NUMBER_DESCRIPTIONS)


class EB300CalibrationNumber(EB300Entity, NumberEntity):
    entity_description: EB300NumberDescription

    @property
    def native_value(self) -> float:
        return self.entity_description.current_fn(self.coordinator.data) / 10.0

    async def async_set_native_value(self, value: float) -> None:
        decideg = round(value * 10)
        data = self.coordinator.data
        if self.entity_description.key == "room_calibration":
            await self.coordinator.async_set_calibration(
                room_decideg=decideg, floor_decideg=data.calibration_floor_decideg
            )
        else:
            await self.coordinator.async_set_calibration(
                room_decideg=data.calibration_room_decideg, floor_decideg=decideg
            )
