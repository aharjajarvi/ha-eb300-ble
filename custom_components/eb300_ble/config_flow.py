"""Config flow for eb300_ble: Bluetooth discovery or manual MAC entry, PSK validated live."""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_PSK,
    CONF_USE_ROOM_SENSOR,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DEFAULT_SCAN_TIMEOUT,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
)
from .eb300_ble.client import BleakTransport, EB300Client
from .eb300_ble.const import MANUFACTURER_ID
from .eb300_ble.exceptions import EB300ConnectionError, HandshakeError
from .eb300_ble.models import DeviceInfo

_LOGGER = logging.getLogger(__name__)

CONF_ADDRESS = "address"


class CannotConnect(Exception):
    """Could not reach the device at all (out of range, powered off)."""


class InvalidAuth(Exception):
    """Handshake completed a connection but the PSK was rejected."""


async def _validate_and_fetch_device_info(address: str, psk: bytes) -> DeviceInfo:
    """Perform a real handshake + device-info read. Raises CannotConnect/InvalidAuth."""
    transport = BleakTransport(address, scan_timeout=DEFAULT_SCAN_TIMEOUT)
    client = EB300Client(transport, psk, request_timeout=DEFAULT_CONNECT_TIMEOUT)
    try:
        await client.connect()
        return await client.read_device_info()
    except HandshakeError as exc:
        raise InvalidAuth from exc
    except (EB300ConnectionError, TimeoutError) as exc:
        raise CannotConnect from exc
    finally:
        await client.disconnect()


def _decode_psk(raw: str) -> bytes:
    try:
        psk = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise vol.Invalid("psk_not_base64") from exc
    if len(psk) != 32:
        raise vol.Invalid("psk_wrong_length")
    return psk


class EB300ConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for eb300_ble."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        """Handle a discovered EB300 (manufacturer ID / service UUID match, per manifest.json)."""
        await self.async_set_unique_id(format_mac(discovery_info.address))
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or discovery_info.address
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_psk()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manual entry: pick from currently-visible EB300 devices, or type a MAC."""
        if user_input is not None:
            self._discovered_address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(format_mac(self._discovered_address), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_psk()

        # Match on manufacturer ID, not name: HA/bleak has been observed reporting this
        # device as "EBECO.EB300" rather than the "EB300" it actually broadcasts
        # (docs/HARDWARE_NOTES.md), so a name-prefix filter is not reliable here.
        current_addresses = self._async_current_ids()
        candidates = {
            info.address: f"{info.name or 'EB300'} ({info.address})"
            for info in async_discovered_service_info(self.hass, connectable=True)
            if format_mac(info.address) not in current_addresses and MANUFACTURER_ID in info.manufacturer_data
        }

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): vol.In(candidates)
                if candidates
                else str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_psk(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the PSK and validate it with a real handshake before creating the entry."""
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._discovered_address is not None
            try:
                psk = _decode_psk(user_input[CONF_PSK])
            except vol.Invalid as exc:
                errors[CONF_PSK] = str(exc.error_message or "psk_not_base64")
            else:
                try:
                    device_info = await _validate_and_fetch_device_info(self._discovered_address, psk)
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    return self.async_create_entry(
                        title=f"EB-Therm 300 ({device_info.serial})",
                        data={
                            CONF_ADDRESS: self._discovered_address,
                            CONF_PSK: user_input[CONF_PSK].strip(),
                        },
                    )

        return self.async_show_form(
            step_id="psk",
            data_schema=vol.Schema({vol.Required(CONF_PSK): str}),
            errors=errors,
            description_placeholders={"name": self._discovered_name or self._discovered_address or ""},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return EB300OptionsFlow()


class EB300OptionsFlow(OptionsFlow):
    """Adjust the poll interval without reloading credentials."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get("poll_interval", DEFAULT_POLL_INTERVAL_SECONDS)
        current_watts = self.config_entry.options.get("rated_watts", 0)
        current_use_room_sensor = self.config_entry.options.get(CONF_USE_ROOM_SENSOR, False)
        schema = vol.Schema(
            {
                vol.Required("poll_interval", default=current_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL_SECONDS, max=MAX_POLL_INTERVAL_SECONDS)
                ),
                # Optional (docs/ARCHITECTURE.md): 0 disables the derived
                # energy (kWh) sensor, since the device has no energy metering
                # of its own — see sensor.py's heating_time/energy entities.
                vol.Optional("rated_watts", default=current_watts): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=5000)
                ),
                # climate current_temperature uses the floor sensor by
                # default; this flips it to the room sensor instead.
                vol.Optional(CONF_USE_ROOM_SENSOR, default=current_use_room_sensor): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
