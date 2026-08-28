"""Read-only binary_sensor entities. See docs/ARCHITECTURE.md for the entity map."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EB300ConfigEntry
from .coordinator import EB300Data
from .entity import EB300Entity


@dataclass(frozen=True, kw_only=True)
class EB300BinarySensorDescription(BinarySensorEntityDescription):
    is_on_fn: Callable[[EB300Data], bool]


BINARY_SENSOR_DESCRIPTIONS: tuple[EB300BinarySensorDescription, ...] = (
    EB300BinarySensorDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.HEAT,
        is_on_fn=lambda data: data.status.relay_on,
    ),
    EB300BinarySensorDescription(
        # `key` is the unique_id and stays `power` deliberately: renaming it
        # would orphan the existing registry entry and throw away its history
        # for a cosmetic change. Only the *name* moves, via translation_key —
        # "Power" now belongs to the derived power (W) sensor, and two entities
        # on one device called "Power" is worse than a stale entity_id.
        key="power",
        translation_key="powered_on",
        device_class=BinarySensorDeviceClass.POWER,
        is_on_fn=lambda data: not data.status.power_off,
    ),
    EB300BinarySensorDescription(
        key="problem",
        translation_key="problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=lambda data: data.status.in_error_state or bool(data.status.active_error_flags),
    ),
    EB300BinarySensorDescription(
        key="limited_by_limiting_sensor",
        translation_key="limited_by_limiting_sensor",
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: data.status.limited_by_limiting_sensor,
    ),
    EB300BinarySensorDescription(
        key="room_sensor_fault",
        translation_key="room_sensor_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: data.status.room_sensor_error != 0,
    ),
    EB300BinarySensorDescription(
        key="floor_sensor_fault",
        translation_key="floor_sensor_fault",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=lambda data: data.status.floor_sensor_error != 0,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EB300ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        EB300BinarySensor(coordinator, description) for description in BINARY_SENSOR_DESCRIPTIONS
    )


class EB300BinarySensor(EB300Entity, BinarySensorEntity):
    entity_description: EB300BinarySensorDescription

    @property
    def is_on(self) -> bool:
        return self.entity_description.is_on_fn(self.coordinator.data)
