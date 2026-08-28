"""Read-only sensor entities. See docs/ARCHITECTURE.md for the entity map."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfEnergy,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EB300ConfigEntry
from .coordinator import EB300Data
from .entity import EB300Entity


@dataclass(frozen=True, kw_only=True)
class EB300SensorDescription(SensorEntityDescription):
    value_fn: Callable[[EB300Data], float | int | str | None]


SENSOR_DESCRIPTIONS: tuple[EB300SensorDescription, ...] = (
    EB300SensorDescription(
        key="floor_temperature",
        translation_key="floor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.floor_temperature_c,
    ),
    EB300SensorDescription(
        key="room_temperature",
        translation_key="room_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.room_temperature_c,
    ),
    EB300SensorDescription(
        key="relay_temperature",
        translation_key="relay_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.relay_temperature_c,
    ),
    EB300SensorDescription(
        key="target_temperature",
        translation_key="target_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.status.current_set_temperature_c,
    ),
    EB300SensorDescription(
        key="program",
        translation_key="program",
        device_class=SensorDeviceClass.ENUM,
        options=["manual", "home"],
        value_fn=lambda data: data.status.program.name.lower(),
    ),
    EB300SensorDescription(
        key="heating_time",
        translation_key="heating_time",
        native_unit_of_measurement=UnitOfTime.HOURS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=1,
        # PID 0x1020 (Energy Meter) returns NOT_IMPLEMENTED on at least one
        # observed firmware (docs/HARDWARE_NOTES.md) — use the EnergyMeter
        # field embedded in the 0x1004 status struct instead, which is part
        # of every poll cycle's mandatory read and always available.
        value_fn=lambda data: data.status.energy_meter / 60,
    ),
    EB300SensorDescription(
        key="time_to_target",
        translation_key="time_to_target",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.status.time_to_target,
    ),
    EB300SensorDescription(
        key="signal_strength",
        translation_key="signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.rssi,
    ),
)


def _energy_description(rated_watts: float) -> EB300SensorDescription:
    def value_fn(data: EB300Data) -> float:
        # Derived, not read from the device: minutes of relay-on time x the
        # element wattage the user entered in options. Only registered when
        # that option is set (see async_setup_entry below) — the plan marks
        # this entity optional precisely because the device has no energy
        # metering of its own.
        return (data.status.energy_meter / 60) * rated_watts / 1000

    return EB300SensorDescription(
        key="energy",
        translation_key="energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=value_fn,
    )


async def async_setup_entry(
    hass: HomeAssistant, entry: EB300ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    entities = [EB300Sensor(coordinator, description) for description in SENSOR_DESCRIPTIONS]

    rated_watts = entry.options.get("rated_watts")
    if rated_watts:
        entities.append(EB300Sensor(coordinator, _energy_description(float(rated_watts))))

    async_add_entities(entities)


class EB300Sensor(EB300Entity, SensorEntity):
    entity_description: EB300SensorDescription

    @property
    def native_value(self) -> float | int | str | None:
        return self.entity_description.value_fn(self.coordinator.data)
