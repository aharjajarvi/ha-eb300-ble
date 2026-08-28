"""Shared base entity: device registration grouping every platform under one HA device."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EB300Coordinator


class EB300Entity(CoordinatorEntity[EB300Coordinator]):
    """Base for every eb300_ble entity — handles unique_id and device grouping."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EB300Coordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.address}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        info = self.coordinator.data.device_info
        return DeviceInfo(
            identifiers={(DOMAIN, info.serial)},
            connections={(CONNECTION_BLUETOOTH, self.coordinator.address)},
            name=f"EB-Therm 300 ({info.serial})",
            manufacturer="Ebeco",
            model=info.model,
            sw_version=info.firmware_version,
        )
