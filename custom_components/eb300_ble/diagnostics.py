"""Diagnostics support. PSK must never appear unredacted (I-13); the serial is
redacted too because it is the device's MAC — as is the entry's address —
and diagnostics get pasted into public bug reports."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import EB300ConfigEntry
from .const import CONF_PSK
from .eb300_ble.const import SensorErrorCode

TO_REDACT = {CONF_PSK, "address", "serial"}


def _sensor_error(raw: int) -> str:
    """Name the per-sensor error byte, without trusting it to be a known code.

    Worth having in every bug report: a non-zero code here is why a temperature
    sensor reads `unknown` (sensor.py suppresses the device's placeholder), and
    it separates a broken sensor from one that was never wired.
    """
    try:
        return SensorErrorCode(raw).name.lower()
    except ValueError:
        return f"unknown_{raw}"


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EB300ConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    data = coordinator.data

    payload: dict[str, Any] = {
        "entry_data": dict(entry.data),
        "entry_options": dict(entry.options),
        "device_info": {
            "model": data.device_info.model,
            "batch": data.device_info.batch,
            "serial": data.device_info.serial,
            "firmware_version": data.device_info.firmware_version,
        },
        "last_status": {
            "error_flags": data.status.active_error_flags,
            "current_set_temperature_c": data.status.current_set_temperature_c,
            "room_temperature_c": data.status.room_temperature_c,
            "floor_temperature_c": data.status.floor_temperature_c,
            "room_sensor_error": _sensor_error(data.status.room_sensor_error),
            "floor_sensor_error": _sensor_error(data.status.floor_sensor_error),
            "relay_temperature_c": data.status.relay_temperature_c,
            "relay_on": data.status.relay_on,
            "power_off": data.status.power_off,
            "program": data.status.program.name,
            "time_to_target": data.status.time_to_target,
            "energy_meter_minutes": data.status.energy_meter,
        },
        "rssi": data.rssi,
        "config": {
            "key_lock": data.key_lock.name,
            "language": data.language.name,
            "screensaver": data.screensaver.name,
            "calibration_room_c": data.calibration_room_decideg / 10,
            "calibration_floor_c": data.calibration_floor_decideg / 10,
        },
    }
    return async_redact_data(payload, TO_REDACT)
