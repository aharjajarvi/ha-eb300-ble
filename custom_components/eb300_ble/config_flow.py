"""Config flow for eb300_ble: Bluetooth discovery or manual MAC entry, PSK validated live."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak, async_discovered_service_info
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import format_mac

from .const import (
    CONF_POLL_INTERVAL,
    CONF_PSK,
    CONF_RATED_WATTS,
    CONF_USE_ROOM_SENSOR,
    DEFAULT_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MAX_POLL_INTERVAL_SECONDS,
    MIN_POLL_INTERVAL_SECONDS,
)
from .coordinator import async_run_once, async_with_retries
from .eb300_ble.client import EB300Client
from .eb300_ble.const import MANUFACTURER_ID, SERVICE_DATA_ACCESS
from .eb300_ble.exceptions import EB300Error, HandshakeError
from .eb300_ble.models import DeviceInfo


class CannotConnect(Exception):
    """Could not reach the device at all (out of range, powered off)."""


class InvalidAuth(Exception):
    """Handshake completed a connection but the PSK was rejected."""


async def _validate_and_fetch_device_info(hass: HomeAssistant, address: str, psk: bytes) -> DeviceInfo:
    """Perform a real handshake + device-info read. Raises CannotConnect/InvalidAuth.

    Goes over the coordinator's own connection path (`async_run_once`): routed
    through HA's Bluetooth manager, queued behind any poll already holding the
    link, capped at BLE_OPERATION_TIMEOUT and retried once. Before this it
    connected by bare address with none of that, so setting up an unreachable
    thermostat could leave the form spinning -- and a proxy slot taken -- for
    minutes.
    """

    async def _run_once(op: Callable[[EB300Client], Awaitable[DeviceInfo]]) -> DeviceInfo:
        return await async_run_once(hass, address, psk, op)

    async def _read_device_info(client: EB300Client) -> DeviceInfo:
        return await client.read_device_info()

    try:
        return await async_with_retries(_run_once, _read_device_info, address)
    except HandshakeError as exc:
        # A handshake that timed out or came back malformed is a connection
        # problem wearing an auth-shaped exception; only an outright rejection
        # means the key is wrong. Telling the two apart matters most in reauth,
        # where "your key was rejected" on a flaky link would send the user off
        # to re-request a key that is in fact fine.
        if exc.is_psk_rejection:
            raise InvalidAuth from exc
        raise CannotConnect from exc
    except (EB300Error, TimeoutError) as exc:
        raise CannotConnect from exc


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
        """Manual entry: pick from the EB300 devices HA's Bluetooth can currently see."""
        if user_input is not None:
            self._discovered_address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(format_mac(self._discovered_address), raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return await self.async_step_psk()

        # Match on what manifest.json's discovery matchers use (manufacturer ID
        # or service UUID), not the name: HA/bleak has been observed reporting
        # this device as "EBECO.EB300" rather than the "EB300" it actually
        # broadcasts (docs/HARDWARE_NOTES.md), so a name-prefix filter is not
        # reliable here.
        current_addresses = self._async_current_ids(include_ignore=False)
        candidates = {
            info.address: f"{info.name or 'EB300'} ({info.address})"
            for info in async_discovered_service_info(self.hass, connectable=True)
            if format_mac(info.address) not in current_addresses
            and (MANUFACTURER_ID in info.manufacturer_data or SERVICE_DATA_ACCESS in info.service_uuids)
        }
        # No free-text address fallback: validation connects through HA's
        # Bluetooth manager, so a thermostat no scanner can see would fail at
        # the key step anyway, after the user had already pasted the key.
        if not candidates:
            return self.async_abort(reason="no_devices_found")

        schema = vol.Schema({vol.Required(CONF_ADDRESS): vol.In(candidates)})
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
                    device_info = await _validate_and_fetch_device_info(self.hass, self._discovered_address, psk)
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

    # ── Replacing the PSK on an existing entry ───────────────────────────
    #
    # The key is a device credential that can be re-issued: disabling and
    # re-enabling local API in the Ebeco Connect app mails out a new one, which
    # makes the stored key dead. Both routes below swap it in place, so entity
    # IDs, history, names and area assignments all survive — deleting and
    # re-adding the entry loses the registry customisations.

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Started by the coordinator when the device refuses the stored PSK."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_replace_psk("reauth_confirm", self._get_reauth_entry(), user_input)

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """User-initiated: the key was rotated before a poll had a chance to fail."""
        return await self._async_replace_psk("reconfigure", self._get_reconfigure_entry(), user_input)

    async def _async_replace_psk(
        self, step_id: str, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Shared body for both routes: validate a new PSK against the entry's device.

        The address is taken from the entry and never re-asked. It is the
        unique ID, so a different address is a different thermostat and belongs
        in a new entry, not this one.
        """
        errors: dict[str, str] = {}
        address = entry.data[CONF_ADDRESS]

        if user_input is not None:
            try:
                psk = _decode_psk(user_input[CONF_PSK])
            except vol.Invalid as exc:
                errors[CONF_PSK] = str(exc.error_message or "psk_not_base64")
            else:
                try:
                    await _validate_and_fetch_device_info(self.hass, address, psk)
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                else:
                    return self._async_apply_psk(entry, user_input[CONF_PSK].strip())

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required(CONF_PSK): str}),
            errors=errors,
            description_placeholders={"name": entry.title, "address": address},
        )

    @callback
    def _async_apply_psk(self, entry: ConfigEntry, psk_b64: str) -> ConfigFlowResult:
        """Write the validated key back and get the entry reloaded onto it."""
        changed = self.hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_PSK: psk_b64}
        )
        # `async_update_entry` only fires the update listener (which reloads us,
        # see __init__._async_update_listener) when something actually changed.
        # Re-entering the key already stored changes nothing, and a reauth that
        # ended there would otherwise leave the entry sitting in its failed
        # state, so reload it explicitly. Deliberately not
        # `async_update_reload_and_abort`: that reloads on top of the listener,
        # and warns about exactly this pairing.
        if not changed:
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(
            reason="reconfigure_successful" if self.source == SOURCE_RECONFIGURE else "reauth_successful"
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return EB300OptionsFlow()


class EB300OptionsFlow(OptionsFlow):
    """Poll interval, heating element wattage, and which sensor the climate entity follows."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL_SECONDS)
        current_watts = self.config_entry.options.get(CONF_RATED_WATTS, 0)
        current_use_room_sensor = self.config_entry.options.get(CONF_USE_ROOM_SENSOR, False)
        schema = vol.Schema(
            {
                vol.Required(CONF_POLL_INTERVAL, default=current_interval): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL_SECONDS, max=MAX_POLL_INTERVAL_SECONDS)
                ),
                # Optional (docs/ARCHITECTURE.md): 0 disables both derived
                # entities — energy (kWh) and power (W) — since the device
                # measures neither. Both are relay-on time or state multiplied
                # by this number; see sensor.py, including what a change to it
                # does to the energy statistics.
                vol.Optional(CONF_RATED_WATTS, default=current_watts): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=5000)
                ),
                # climate current_temperature uses the floor sensor by
                # default; this flips it to the room sensor instead.
                vol.Optional(CONF_USE_ROOM_SENSOR, default=current_use_room_sensor): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
