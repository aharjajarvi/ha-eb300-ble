"""DataUpdateCoordinator for eb300_ble: connect, handshake, read, disconnect — every cycle."""

from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, TypeVar

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    BLE_CONNECT_MAX_ATTEMPTS,
    BLE_OPERATION_TIMEOUT,
    CONNECT_RETRY_ATTEMPTS,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_SCAN_TIMEOUT,
    WEEKDAYS,
)
from .eb300_ble.client import BleakTransport, EB300Client
from .eb300_ble.const import PID, KeyLock, Language, Operation, Program, ScreensaverType
from .eb300_ble.exceptions import EB300ConnectionError, EB300Error, ValidationError
from .eb300_ble.models import DeviceInfo as EB300DeviceInfo
from .eb300_ble.models import HomeProgram, ThermostatStatus
from .eb300_ble.protocol import HOME_PROGRAM_EVENTS_PER_DAY, HomeProgramEvent

_T = TypeVar("_T")

_LOGGER = logging.getLogger(__name__)

# Global across every eb300_ble config entry in this HA process, not per-device.
# BLE proxy connection slots (~3 per proxy) are shared with every other
# Bluetooth integration in the house — this only guarantees eb300_ble never
# uses more than one at a time itself. With the limit at 1, a second device's
# poll simply queues behind the first rather than racing it, which is a
# stronger guarantee than a separate stagger-offset scheme would give and
# makes one unnecessary.
_CONNECTION_SEMAPHORE = asyncio.Semaphore(1)


@dataclass(slots=True)
class EB300Data:
    status: ThermostatStatus
    device_info: EB300DeviceInfo
    rssi: int | None
    key_lock: KeyLock
    language: Language
    screensaver: ScreensaverType
    calibration_room_decideg: int
    calibration_floor_decideg: int


class EB300Coordinator(DataUpdateCoordinator[EB300Data]):
    """Polls one EB-Therm 300 thermostat."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        address: str,
        psk: bytes,
        update_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=f"eb300_ble ({address})",
            update_interval=timedelta(seconds=update_interval_seconds),
        )
        self.address = address
        self._psk = psk
        # Device info (model/serial/firmware) never changes post-pairing —
        # read once on the first successful poll and cached, not re-fetched
        # every cycle.
        self._device_info: EB300DeviceInfo | None = None

    async def _async_update_data(self) -> EB300Data:
        try:
            return await self._with_client(self._poll)
        except (EB300Error, TimeoutError) as exc:
            # `str(TimeoutError())` is empty; fall back to the type name so the
            # log line never trails off after the colon.
            raise UpdateFailed(
                f"Could not read {self.address} after {CONNECT_RETRY_ATTEMPTS} attempt(s): "
                f"{str(exc) or type(exc).__name__}"
            ) from exc

    async def _poll(self, client: EB300Client) -> EB300Data:
        if self._device_info is None:
            self._device_info = await client.read_device_info()
        status = await client.read_status()

        # Config-category values (key lock, language, screensaver, calibration)
        # aren't part of the 0x1004 status struct, so they need their own GETs
        # — batched into one encrypted frame to keep this to a single extra
        # round trip per poll cycle rather than four.
        key_lock_resp, language_resp, screensaver_resp, calibration_resp = await client.request_batch(
            [
                (Operation.GET, PID.KEY_LOCK, b""),
                (Operation.GET, PID.LANGUAGE, b""),
                (Operation.GET, PID.SCREENSAVER_TYPE, b""),
                (Operation.GET, PID.CALIBRATION_USER, b""),
            ]
        )
        room_decideg, floor_decideg, _relay_decideg = struct.unpack("<hhh", calibration_resp.data)

        service_info = bluetooth.async_last_service_info(self.hass, self.address, connectable=True)
        rssi = service_info.rssi if service_info else None

        assert self._device_info is not None
        return EB300Data(
            status=status,
            device_info=self._device_info,
            rssi=rssi,
            key_lock=KeyLock(key_lock_resp.data[0]),
            language=Language(language_resp.data[0]),
            screensaver=ScreensaverType(screensaver_resp.data[0]),
            calibration_room_decideg=room_decideg,
            calibration_floor_decideg=floor_decideg,
        )

    # ── Writes ────────────────────────────────────────────────────────────
    #
    # Every setter connects, performs the SET, disconnects, then requests a
    # fresh poll — so entities reflect the change within one connection cycle
    # instead of waiting up to `poll_interval` for the next scheduled one.

    async def async_set_power(self, on: bool) -> None:
        await self._write(lambda client: client.set_power(on))

    async def async_set_manual_temp(self, decideg: int) -> None:
        await self._write(lambda client: client.set_manual_temp(decideg))

    async def async_set_override_temp(self, decideg: int) -> None:
        await self._write(lambda client: client.set_override_temp(decideg))

    async def async_set_program(self, program: Program) -> None:
        await self._write(lambda client: client.set_program(program))

    async def async_set_key_lock(self, locked: bool) -> None:
        await self._write(lambda client: client.set_key_lock(locked))

    async def async_set_language(self, language: Language) -> None:
        await self._write(lambda client: client.set_language(language))

    async def async_set_screensaver(self, screensaver: ScreensaverType) -> None:
        await self._write(lambda client: client.set_screensaver(screensaver))

    async def async_set_calibration(self, *, room_decideg: int, floor_decideg: int) -> None:
        await self._write(lambda client: client.set_calibration(room_decideg=room_decideg, floor_decideg=floor_decideg))

    async def async_sync_clock(self) -> None:
        await self._write(lambda client: client.sync_clock())

    # ── Home program (exposed as services, not entities) ────────────────────

    async def async_get_home_program(self) -> HomeProgram:
        try:
            return await self._with_client(lambda client: client.read_home_program())
        except TimeoutError as exc:
            raise HomeAssistantError(
                f"Could not read {self.address}: timed out after "
                f"{CONNECT_RETRY_ATTEMPTS} attempt(s) of {BLE_OPERATION_TIMEOUT:.0f}s"
            ) from exc
        except EB300Error as exc:
            raise HomeAssistantError(f"Could not read {self.address}: {exc}") from exc

    async def async_set_home_program(self, updates: Mapping[str, Sequence[Mapping[str, Any]]]) -> None:
        """GET -> merge -> SET -> verify-readback, all inside one connection.

        Kept separate from `_write` (rather than reusing it) because a bad
        schedule must surface as `ServiceValidationError` — a user-input
        error HA renders without a traceback — not the generic
        `HomeAssistantError` every other write path uses for connectivity
        failures. Merging `ValidationError` into `_write`'s
        existing catch-all would change that behaviour for every other write
        method too.
        """

        async def _op(client: EB300Client) -> None:
            current = await client.read_home_program()
            merged = merge_home_program(current, updates)
            await client.set_home_program(merged)
            readback = await client.read_home_program()
            if readback != merged:
                raise EB300ConnectionError(
                    f"Home program write to {self.address} did not verify: device readback "
                    "does not match what was written"
                )

        try:
            await self._with_client(_op)
        except ValidationError as exc:
            raise ServiceValidationError(str(exc)) from exc
        except TimeoutError as exc:
            raise HomeAssistantError(
                f"Could not write to {self.address}: timed out after "
                f"{CONNECT_RETRY_ATTEMPTS} attempt(s) of {BLE_OPERATION_TIMEOUT:.0f}s"
            ) from exc
        except EB300Error as exc:
            raise HomeAssistantError(f"Could not write to {self.address}: {exc}") from exc
        await self.async_request_refresh()

    async def _write(self, op: Callable[[EB300Client], Awaitable[None]]) -> None:
        # Translate library errors to HomeAssistantError here, once, rather
        # than in each of the seven entity write methods. HA treats any other
        # exception escaping a service call as an integration bug and logs a
        # full traceback at ERROR instead of showing the user a readable
        # message. See docs/HARDWARE_NOTES.md.
        try:
            await self._with_client(op)
        except TimeoutError as exc:
            # Carries no message of its own, and BLE_OPERATION_TIMEOUT makes it
            # the usual outcome for an unreachable device — spell it out rather
            # than showing the user a message ending in a bare colon.
            raise HomeAssistantError(
                f"Could not write to {self.address}: timed out after "
                f"{CONNECT_RETRY_ATTEMPTS} attempt(s) of {BLE_OPERATION_TIMEOUT:.0f}s"
            ) from exc
        except EB300Error as exc:
            raise HomeAssistantError(f"Could not write to {self.address}: {exc}") from exc
        await self.async_request_refresh()

    # ── Shared connect/retry plumbing ────────────────────────────────────

    async def _with_client(self, op: Callable[[EB300Client], Awaitable[_T]]) -> _T:
        last_error: Exception | None = None
        for attempt in range(1, CONNECT_RETRY_ATTEMPTS + 1):
            try:
                return await self._run_once(op)
            except (EB300Error, TimeoutError) as exc:
                last_error = exc
                _LOGGER.debug(
                    "Attempt %d/%d for %s failed: %s", attempt, CONNECT_RETRY_ATTEMPTS, self.address, exc
                )
        assert last_error is not None
        raise last_error

    async def _run_once(self, op: Callable[[EB300Client], Awaitable[_T]]) -> _T:
        # Resolve through HA's own Bluetooth manager, not our own scan: it
        # already tracks every proxy (both of them, in this house) and their
        # signal quality, and routes to the best one. Passing a bare address
        # to BleakTransport would make it do its own uncoordinated
        # BleakScanner.find_device_by_address() instead — the exact pattern
        # habluetooth's "connect() called without bleak-retry-connector"-style
        # warnings exist to catch (docs/HARDWARE_NOTES.md).
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None:
            raise EB300ConnectionError(f"{self.address} not currently visible to any Bluetooth scanner")

        async with _CONNECTION_SEMAPHORE:
            transport = BleakTransport(
                ble_device, scan_timeout=DEFAULT_SCAN_TIMEOUT, max_attempts=BLE_CONNECT_MAX_ATTEMPTS
            )
            client = EB300Client(transport, self._psk, request_timeout=DEFAULT_CONNECT_TIMEOUT)
            try:
                # Timeout inside the semaphore, not around it: an operation
                # queued behind another one must not be charged for the time it
                # spent waiting its turn. Disconnect stays outside the scope so
                # teardown always runs. TimeoutError is already retryable in
                # _with_client, so a timed-out attempt still gets its second
                # chance. See BLE_OPERATION_TIMEOUT in const.py for why this is
                # the only lever available here.
                async with asyncio.timeout(BLE_OPERATION_TIMEOUT):
                    await client.connect()
                    return await op(client)
            finally:
                await client.disconnect()


def merge_home_program(current: HomeProgram, updates: Mapping[str, Sequence[Mapping[str, Any]]]) -> HomeProgram:
    """Merge partial per-weekday event lists onto a full 7-day program.

    - Days absent from `updates` are left byte-for-byte unchanged.
    - Given events default to `active=True`; pass `active: false` to keep a slot's
      time and temperature but stop it firing. An inactive event still needs a
      valid, in-order time, since the ordering check spans inactive events too.
    - A day given fewer than 4 events keeps the device's own existing time and
      temperature for the remaining slots (marked inactive), falling back to
      repeating the last given event's values if the existing day is
      (unexpectedly) shorter than 4 events.

    Raises `eb300_ble.exceptions.ValidationError` — via `HomeProgram.to_bytes()`
    inside `EB300Client.set_home_program`, not here — if the merged result
    violates a device constraint (e.g. chronological order); this function
    itself only validates that a day was not given more than 4 events.
    """
    new_days = list(current.days)
    for day_idx, day_name in enumerate(WEEKDAYS):
        if day_name not in updates:
            continue
        given = updates[day_name]
        if len(given) > HOME_PROGRAM_EVENTS_PER_DAY:
            raise ValidationError(
                f"{day_name}: at most {HOME_PROGRAM_EVENTS_PER_DAY} events allowed, got {len(given)}"
            )
        existing_day = current.days[day_idx]
        new_events: list[HomeProgramEvent] = []
        for item in given:
            hour, minute = _parse_hh_mm(str(item["time"]))
            temperature_decideg = round(float(item["temperature"]) * 10)
            new_events.append(
                HomeProgramEvent(
                    active=bool(item.get("active", True)),
                    hour=hour,
                    minute=minute,
                    temperature_decideg=temperature_decideg,
                )
            )
        for i in range(len(given), HOME_PROGRAM_EVENTS_PER_DAY):
            if i < len(existing_day):
                source = existing_day[i]
            elif new_events:
                source = new_events[-1]
            else:
                source = HomeProgramEvent(active=False, hour=0, minute=0, temperature_decideg=0)
            new_events.append(
                HomeProgramEvent(
                    active=False,
                    hour=source.hour,
                    minute=source.minute,
                    temperature_decideg=source.temperature_decideg,
                )
            )
        new_days[day_idx] = new_events
    return HomeProgram(days=new_days)


def _parse_hh_mm(value: str) -> tuple[int, int]:
    """Parse "HH:MM" — format already enforced by the service schema (services.py)."""
    hour_str, _, minute_str = value.partition(":")
    return int(hour_str), int(minute_str)
