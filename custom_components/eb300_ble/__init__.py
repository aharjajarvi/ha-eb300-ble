"""The eb300_ble integration: Ebeco EB-Therm 300 floor heating thermostat over BLE."""

from __future__ import annotations

import base64

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import CONF_POLL_INTERVAL, CONF_PSK, DEFAULT_POLL_INTERVAL_SECONDS, DOMAIN
from .coordinator import EB300Coordinator
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.BUTTON,
]

# Config entries only: nothing is read from configuration.yaml. Declared
# because defining `async_setup` makes hassfest require a schema.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type EB300ConfigEntry = ConfigEntry[EB300Coordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions once, independent of any config entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: EB300ConfigEntry) -> bool:
    address = entry.data[CONF_ADDRESS]
    psk = base64.b64decode(entry.data[CONF_PSK])
    poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS)

    coordinator = EB300Coordinator(hass, entry, address, psk, poll_interval)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EB300ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: EB300ConfigEntry) -> None:
    """Options or the stored key changed — reload so the coordinator picks them up."""
    await hass.config_entries.async_reload(entry.entry_id)
