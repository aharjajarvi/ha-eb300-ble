"""Language / screensaver selects. See docs/ARCHITECTURE.md for the entity map."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EB300ConfigEntry
from .coordinator import EB300Coordinator, EB300Data
from .eb300_ble.const import Language, ScreensaverType
from .entity import EB300Entity

_LANGUAGE_OPTIONS = [member.name.lower() for member in Language]
_SCREENSAVER_OPTIONS = [member.name.lower() for member in ScreensaverType]


@dataclass(frozen=True, kw_only=True)
class EB300SelectDescription(SelectEntityDescription):
    current_fn: Callable[[EB300Data], str]
    select_fn: Callable[[EB300Coordinator, str], Awaitable[None]]


SELECT_DESCRIPTIONS: tuple[EB300SelectDescription, ...] = (
    EB300SelectDescription(
        key="language",
        translation_key="language",
        entity_category=EntityCategory.CONFIG,
        options=_LANGUAGE_OPTIONS,
        current_fn=lambda data: data.language.name.lower(),
        select_fn=lambda coordinator, option: coordinator.async_set_language(Language[option.upper()]),
    ),
    EB300SelectDescription(
        key="screensaver_type",
        translation_key="screensaver_type",
        entity_category=EntityCategory.CONFIG,
        options=_SCREENSAVER_OPTIONS,
        current_fn=lambda data: data.screensaver.name.lower(),
        select_fn=lambda coordinator, option: coordinator.async_set_screensaver(ScreensaverType[option.upper()]),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: EB300ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(EB300Select(coordinator, description) for description in SELECT_DESCRIPTIONS)


class EB300Select(EB300Entity, SelectEntity):
    entity_description: EB300SelectDescription

    @property
    def current_option(self) -> str:
        return self.entity_description.current_fn(self.coordinator.data)

    async def async_select_option(self, option: str) -> None:
        await self.entity_description.select_fn(self.coordinator, option)
