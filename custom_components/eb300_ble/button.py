"""Sync-clock button. See docs/ARCHITECTURE.md for the entity map."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EB300ConfigEntry
from .entity import EB300Entity

SYNC_CLOCK_DESCRIPTION = ButtonEntityDescription(
    key="sync_clock",
    translation_key="sync_clock",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EB300ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([EB300SyncClockButton(coordinator, SYNC_CLOCK_DESCRIPTION)])


class EB300SyncClockButton(EB300Entity, ButtonEntity):
    async def async_press(self) -> None:
        await self.coordinator.async_sync_clock()
