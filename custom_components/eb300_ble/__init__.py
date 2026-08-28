"""The eb300_ble integration: Ebeco EB-Therm 300 floor heating thermostat over BLE."""

from __future__ import annotations

import base64

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .config_flow import CONF_ADDRESS
from .const import CONF_PSK, DEFAULT_POLL_INTERVAL_SECONDS
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

type EB300ConfigEntry = ConfigEntry[EB300Coordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EB300ConfigEntry) -> bool:
    address = entry.data[CONF_ADDRESS]
    psk = base64.b64decode(entry.data[CONF_PSK])
    poll_interval = entry.options.get("poll_interval", DEFAULT_POLL_INTERVAL_SECONDS)

    coordinator = EB300Coordinator(hass, entry, address, psk, poll_interval)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_setup_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EB300ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(hass: HomeAssistant, entry: EB300ConfigEntry) -> None:
    """Options (poll interval) changed — reload so the coordinator picks it up."""
    await hass.config_entries.async_reload(entry.entry_id)
