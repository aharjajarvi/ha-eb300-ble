"""Key lock switch. See docs/ARCHITECTURE.md for the entity map."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EB300ConfigEntry
from .eb300_ble.const import KeyLock
from .entity import EB300Entity

KEY_LOCK_DESCRIPTION = SwitchEntityDescription(
    key="key_lock",
    translation_key="key_lock",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EB300ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([EB300KeyLockSwitch(coordinator, KEY_LOCK_DESCRIPTION)])


class EB300KeyLockSwitch(EB300Entity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        key_lock = self.coordinator.data.key_lock
        return key_lock is KeyLock.LOCKED if key_lock is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_key_lock(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_key_lock(False)
