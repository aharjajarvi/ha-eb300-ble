"""EB300Client: handshake, request/response routing, and response dispatch.

Talks to a `Transport` — an abstraction over "write bytes to RX, get bytes back
via a TX notification callback". `BleakTransport` implements it over real BLE;
tests plug in `FakeEB300` (see tests/fakes.py) instead, so the entire client can
be exercised with no radio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol, Self

if TYPE_CHECKING:
    from bleak import BleakClient
    from bleak.backends.device import BLEDevice

from .const import (
    CALIBRATION_MAX_DECIDEG,
    CALIBRATION_MIN_DECIDEG,
    CHAR_DATA_STREAM_RX,
    CHAR_DATA_STREAM_TX,
    MAX_INNER_PAYLOAD,
    NONCE_LEN,
    PID,
    TEMP_MAX_DECIDEG,
    TEMP_MIN_DECIDEG,
    KeyLock,
    Language,
    Operation,
    OuterMessageType,
    Program,
    ScreensaverType,
)
from .crypto import compute_hmac, derive_keys, unwrap, verify_hmac, wrap
from .exceptions import (
    CryptoError,
    DeviceError,
    EB300ConnectionError,
    HandshakeError,
    ProtocolError,
    RequestTimeoutError,
    ValidationError,
)
from .models import DeviceInfo, HomeProgram, ThermostatStatus
from .protocol import (
    Counter,
    InnerMessage,
    build_inner_message,
    build_outer,
    parse_inner_messages,
    parse_outer,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_REQUEST_TIMEOUT = 5.0


class Transport(Protocol):
    """What EB300Client needs from a BLE (or fake) transport."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    async def write(self, data: bytes) -> None: ...
    def set_notification_callback(self, callback: Callable[[bytes], None]) -> None: ...


class BleakTransport:
    """Transport implementation over `bleak`, used on real hardware.

    Accepts either a bare MAC string (standalone/CLI use — this transport
    resolves it itself via `BleakScanner`) or a pre-resolved `BLEDevice`
    (HA use — the coordinator resolves it via HA's own Bluetooth manager,
    which already knows about every proxy and picks the best one; passing a
    bare string from inside HA would bypass all of that and do its own
    uncoordinated scan instead).
    """

    def __init__(
        self, address_or_device: str | BLEDevice, *, scan_timeout: float = 10.0, **bleak_kwargs: Any
    ) -> None:
        self._address_or_device = address_or_device
        self._scan_timeout = scan_timeout
        self._bleak_kwargs = bleak_kwargs
        self._client: BleakClient | None = None
        self._callback: Callable[[bytes], None] | None = None

    async def connect(self) -> None:
        from bleak import BleakClient  # imported lazily: not needed off-hardware
        from bleak.exc import BleakError
        from bleak_retry_connector import establish_connection

        device = self._address_or_device
        if isinstance(device, str):
            from bleak import BleakScanner

            # Bare address (no HA Bluetooth manager available): resolve it
            # ourselves. Connecting straight off a bare address string relies
            # on BlueZ's device object cache, which is short-lived once
            # nothing is actively scanning (observed on real hardware:
            # BleakDeviceNotFoundError a couple of minutes after the last
            # scan, even though the device was advertising continuously the
            # whole time).
            resolved = await BleakScanner.find_device_by_address(device, timeout=self._scan_timeout)
            if resolved is None:
                raise EB300ConnectionError(
                    f"No BLE advertisement seen from {device} within {self._scan_timeout}s "
                    "(device powered off, out of range, or BLE disabled on it?)"
                )
            device = resolved

        # establish_connection, not a bare BleakClient().connect(): it retries
        # internally (4 attempts by default) and is what HA's own Bluetooth
        # stack expects third-party integrations to use — a raw connect() was
        # flagged by habluetooth as unreliable when multiple scanners/proxies
        # are involved (confirmed in docs/HARDWARE_NOTES.md against real HA logs).
        #
        # Every bleak/bleak_retry_connector failure mode (BleakNotFoundError,
        # BleakConnectionError, BleakOutOfConnectionSlotsError, ...) derives
        # from bleak.exc.BleakError, not from anything in this library's own
        # exception hierarchy. Left unwrapped, callers that only catch
        # EB300Error/TimeoutError (coordinator retry logic, HA entity write
        # paths) never see it — confirmed on real hardware: a device going
        # offline mid-session raised a raw BleakError that skipped the
        # climate entity's failed-write state revert entirely, leaving HA
        # showing an unapplied setpoint.
        try:
            self._client = await establish_connection(
                BleakClient, device, device.name or device.address, **self._bleak_kwargs
            )
        except BleakError as exc:
            raise EB300ConnectionError(f"Could not connect to {device}: {exc}") from exc
        # start_notify needs the same guard: a device that drops the link
        # between connect and notify-subscribe raises a raw BleakError here,
        # which would escape every caller for the same reason.
        try:
            await self._client.start_notify(CHAR_DATA_STREAM_TX, self._on_notify)
        except BleakError as exc:
            raise EB300ConnectionError(f"Could not subscribe to notifications on {device}: {exc}") from exc

    def _on_notify(self, _characteristic: object, data: bytearray) -> None:
        if self._callback is not None:
            self._callback(bytes(data))

    def set_notification_callback(self, callback: Callable[[bytes], None]) -> None:
        self._callback = callback

    async def write(self, data: bytes) -> None:
        from bleak.exc import BleakError

        if self._client is None:
            raise EB300ConnectionError("Transport not connected")
        # Data Stream RX only advertises the "write" (with response) GATT property,
        # not "write-without-response" (confirmed via H-2 GATT discovery on real
        # hardware) — forcing response=False here silently drops writes.
        try:
            await self._client.write_gatt_char(CHAR_DATA_STREAM_RX, data, response=True)
        except BleakError as exc:
            raise EB300ConnectionError(f"BLE write failed: {exc}") from exc

    async def disconnect(self) -> None:
        from bleak.exc import BleakError

        # Swallowed, not normalized and re-raised: callers invoke disconnect
        # from a `finally`, so anything raised here *replaces* the exception
        # already propagating — a failed teardown on an already-broken link
        # would mask the real error and escape callers that only catch
        # EB300Error/TimeoutError. Clearing _client matters more than the
        # teardown succeeding; the link is going away either way.
        if self._client is not None and self._client.is_connected:
            try:
                await self._client.disconnect()
            except BleakError as exc:
                _LOGGER.debug("Ignoring BLE disconnect failure: %s", exc)
        self._client = None


class EB300Client:
    """One handshake session with one EB300 thermostat over the Open API channel."""

    def __init__(self, transport: Transport, psk: bytes, *, request_timeout: float = DEFAULT_REQUEST_TIMEOUT) -> None:
        self._transport = transport
        self._psk = psk
        self._request_timeout = request_timeout

        self._session_key: bytes | None = None
        self._counter = Counter()
        self._handshake_future: asyncio.Future[bytes] | None = None
        self._pending: dict[int, asyncio.Future[InnerMessage]] = {}
        self._data_callback: Callable[[InnerMessage], None] | None = None

    @property
    def is_connected(self) -> bool:
        return self._session_key is not None

    def set_data_callback(self, callback: Callable[[InnerMessage], None] | None) -> None:
        """Register a callback for unsolicited DATA (op 0x04) pushes."""
        self._data_callback = callback

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    # ── Connection lifecycle ─────────────────────────────────────────────

    async def connect(self) -> None:
        await self._transport.connect()
        self._transport.set_notification_callback(self._on_notify)
        await self._handshake()

    async def disconnect(self) -> None:
        await self._transport.disconnect()
        self._session_key = None
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending.clear()

    async def _handshake(self) -> None:
        client_nonce = os.urandom(NONCE_LEN)
        resp = await self._handshake_step(OuterMessageType.CLIENT_NONCE, client_nonce)
        _, msg_type, payload = parse_outer(resp)
        if msg_type == OuterMessageType.ERROR:
            code = payload[0] if payload else -1
            raise HandshakeError(
                f"Handshake failed at step 1 (server nonce): device error {code} — "
                "PSK likely not provisioned for the Open API channel",
                step=1,
                error_code=code,
            )
        if msg_type != OuterMessageType.SERVER_NONCE or len(payload) < NONCE_LEN:
            raise HandshakeError(f"Unexpected or malformed response at step 1 (type 0x{msg_type:X})", step=1)
        server_nonce = payload[:NONCE_LEN]

        keys = derive_keys(self._psk, client_nonce, server_nonce)
        client_hmac = compute_hmac(keys.hmac_key, client_nonce, server_nonce, b"_CLIENT")

        resp = await self._handshake_step(OuterMessageType.CLIENT_HMAC, client_hmac)
        _, msg_type, payload = parse_outer(resp)
        if msg_type == OuterMessageType.ERROR:
            code = payload[0] if payload else -1
            raise HandshakeError(
                f"Handshake failed at step 3 (server hmac): device error {code} — wrong PSK?",
                step=3,
                error_code=code,
            )
        if msg_type != OuterMessageType.SERVER_HMAC or len(payload) < 32:
            raise HandshakeError(f"Unexpected or malformed response at step 3 (type 0x{msg_type:X})", step=3)
        server_hmac = payload[:32]
        if not verify_hmac(keys.hmac_key, client_nonce, server_nonce, b"_SERVER", server_hmac):
            raise HandshakeError("Server HMAC mismatch (wrong PSK?)", step=4)

        self._session_key = keys.session_key
        self._counter = Counter()

    async def _handshake_step(self, message_type: OuterMessageType, payload: bytes) -> bytes:
        loop = asyncio.get_running_loop()
        self._handshake_future = loop.create_future()
        try:
            await self._transport.write(build_outer(message_type, payload))
            try:
                return await asyncio.wait_for(self._handshake_future, timeout=self._request_timeout)
            except TimeoutError as exc:
                raise HandshakeError("Timed out waiting for handshake response", step=0) from exc
        finally:
            self._handshake_future = None

    # ── Notification routing ─────────────────────────────────────────────

    def _on_notify(self, data: bytes) -> None:
        if self._handshake_future is not None and not self._handshake_future.done():
            self._handshake_future.set_result(data)
            return

        try:
            _, msg_type, payload = parse_outer(data)
        except Exception:
            _LOGGER.debug("Dropping malformed notification", exc_info=True)
            return

        if msg_type == OuterMessageType.ERROR:
            _LOGGER.warning("Unexpected outer error after handshake: code %s", payload[0] if payload else None)
            return
        if msg_type != OuterMessageType.ENCRYPTED_DATA:
            _LOGGER.debug("Ignoring unexpected outer message type 0x%X", msg_type)
            return
        if self._session_key is None:
            _LOGGER.debug("Dropping encrypted data received before handshake completed")
            return

        try:
            inner_payload = unwrap(self._session_key, payload)
        except CryptoError:
            _LOGGER.warning("Failed to decrypt data message, dropping")
            return

        for msg in parse_inner_messages(inner_payload):
            self._dispatch_inner(msg)

    def _dispatch_inner(self, msg: InnerMessage) -> None:
        if msg.operation in (Operation.GET_RESPONSE, Operation.SET_RESPONSE):
            future = self._pending.pop(msg.counter, None)
            if future is not None and not future.done():
                future.set_result(msg)
            else:
                _LOGGER.debug("Dropping response for unknown/stale counter %s", msg.counter)
        elif msg.operation == Operation.DATA:
            if self._data_callback is not None:
                self._data_callback(msg)
        else:
            _LOGGER.debug("Ignoring inner message with unexpected operation %s", msg.operation)

    # ── Requests ─────────────────────────────────────────────────────────

    async def request(self, operation: int, pid: int, data: bytes = b"") -> InnerMessage:
        if self._session_key is None:
            raise EB300ConnectionError("Not connected: handshake not complete")

        counter = self._counter.next()
        inner = build_inner_message(operation, pid, counter, data)
        frame = build_outer(OuterMessageType.ENCRYPTED_DATA, wrap(self._session_key, inner))

        loop = asyncio.get_running_loop()
        future: asyncio.Future[InnerMessage] = loop.create_future()
        self._pending[counter] = future

        await self._transport.write(frame)
        try:
            msg = await asyncio.wait_for(future, timeout=self._request_timeout)
        except TimeoutError as exc:
            self._pending.pop(counter, None)
            raise RequestTimeoutError(pid, counter, self._request_timeout) from exc

        if msg.error != 0:
            raise DeviceError(msg.error, pid=pid)
        return msg

    async def request_batch(self, requests: Sequence[tuple[int, int, bytes]]) -> list[InnerMessage]:
        """Send several GET/SET requests in a single encrypted frame.

        Cuts BLE round trips (and therefore airtime/connection-slot hold time)
        for a poll cycle that reads several PIDs at once. Responses are
        returned in the same order as `requests`; a single device error aborts
        the whole batch (matches `request()`'s per-call semantics).
        """
        if self._session_key is None:
            raise EB300ConnectionError("Not connected: handshake not complete")
        if not requests:
            return []

        counters: list[int] = []
        inner_frame = bytearray()
        loop = asyncio.get_running_loop()
        futures: dict[int, asyncio.Future[InnerMessage]] = {}
        for operation, pid, data in requests:
            counter = self._counter.next()
            counters.append(counter)
            inner_frame += build_inner_message(operation, pid, counter, data)
            future: asyncio.Future[InnerMessage] = loop.create_future()
            futures[counter] = future
            self._pending[counter] = future

        if len(inner_frame) > MAX_INNER_PAYLOAD:
            for counter in counters:
                self._pending.pop(counter, None)
            raise ProtocolError(
                f"Batched request frame {len(inner_frame)} bytes exceeds max inner payload {MAX_INNER_PAYLOAD}"
            )

        frame = build_outer(OuterMessageType.ENCRYPTED_DATA, wrap(self._session_key, bytes(inner_frame)))
        await self._transport.write(frame)

        results: list[InnerMessage] = []
        for (_, pid, _), counter in zip(requests, counters, strict=True):
            try:
                msg = await asyncio.wait_for(futures[counter], timeout=self._request_timeout)
            except TimeoutError as exc:
                self._pending.pop(counter, None)
                raise RequestTimeoutError(pid, counter, self._request_timeout) from exc
            if msg.error != 0:
                raise DeviceError(msg.error, pid=pid)
            results.append(msg)
        return results

    async def get(self, pid: int) -> InnerMessage:
        return await self.request(Operation.GET, pid)

    async def set(self, pid: int, data: bytes) -> InnerMessage:
        return await self.request(Operation.SET, pid, data)

    # ── High-level helpers ───────────────────────────────────────────────

    async def read_device_info(self) -> DeviceInfo:
        model = (await self.get(PID.MODEL_NAME)).data.decode("utf-8")
        batch = (await self.get(PID.BATCH_NAME)).data.decode("utf-8")
        serial = (await self.get(PID.SERIAL_NUMBER)).data.decode("utf-8")
        firmware_version = (await self.get(PID.FIRMWARE_VERSION)).data.decode("utf-8")
        return DeviceInfo(model=model, batch=batch, serial=serial, firmware_version=firmware_version)

    async def read_status(self) -> ThermostatStatus:
        msg = await self.get(PID.THERMOSTAT_STATUS)
        return ThermostatStatus.from_bytes(msg.data)

    async def read_energy_meter(self) -> int:
        import struct

        msg = await self.get(PID.ENERGY_METER)
        value: int = struct.unpack("<I", msg.data)[0]
        return value

    async def ping(self) -> None:
        await self.get(PID.PING)

    async def read_home_program(self) -> HomeProgram:
        msg = await self.get(PID.HOME_PROGRAM)
        return HomeProgram.from_bytes(msg.data)

    # ── Write helpers ────────────────────────────────────────────────────
    #
    # Every setter validates client-side (Open API §5.3/5.5/5.6/5.2) and raises
    # ValidationError *before* any BLE write — cheaper than a round trip to
    # discover the device would have rejected it, and the error message is
    # ours to write instead of a bare INVALID_PARAMETER device error.

    async def set_power(self, on: bool) -> None:
        await self.set(PID.POWER_ON, bytes([1 if on else 0]))

    async def set_manual_temp(self, decideg: int) -> None:
        """Update Manual Control Temp (0x1082). Sticks only while Program is Manual."""
        _validate_temp_decideg(decideg)
        await self.set(PID.MANUAL_CONTROL_TEMP, struct.pack("<H", decideg))

    async def set_override_temp(self, decideg: int) -> None:
        """Momentary override (0x10D0) — the default write path. See docs/PROTOCOL.md."""
        _validate_temp_decideg(decideg)
        await self.set(PID.OVERRIDE_TEMPERATURE, struct.pack("<H", decideg))

    async def set_program(self, program: Program) -> None:
        await self.set(PID.SELECTED_PROGRAM, bytes([int(program)]))

    async def set_key_lock(self, locked: bool) -> None:
        await self.set(PID.KEY_LOCK, bytes([KeyLock.LOCKED if locked else KeyLock.UNLOCKED]))

    async def set_language(self, language: Language) -> None:
        await self.set(PID.LANGUAGE, bytes([int(language)]))

    async def set_screensaver(self, screensaver: ScreensaverType) -> None:
        await self.set(PID.SCREENSAVER_TYPE, bytes([int(screensaver)]))

    async def set_calibration(self, *, room_decideg: int, floor_decideg: int) -> None:
        """User calibration offsets (0x10B2). Relay index is always forced to 0."""
        for name, value in (("room", room_decideg), ("floor", floor_decideg)):
            if not (CALIBRATION_MIN_DECIDEG <= value <= CALIBRATION_MAX_DECIDEG):
                raise ValidationError(
                    f"{name} calibration {value} outside [{CALIBRATION_MIN_DECIDEG}, {CALIBRATION_MAX_DECIDEG}]"
                    " decidegrees"
                )
        await self.set(PID.CALIBRATION_USER, struct.pack("<hhh", room_decideg, floor_decideg, 0))

    async def sync_clock(self, timestamp: int | None = None) -> None:
        await self.set(PID.NTP_TIME, struct.pack("<I", int(timestamp if timestamp is not None else time.time())))

    async def set_home_program(self, program: HomeProgram) -> None:
        """Write the full 112-byte weekly schedule.

        `program.to_bytes()` validates client-side and raises `ValidationError`
        before any BLE write.
        """
        await self.set(PID.HOME_PROGRAM, program.to_bytes())


def _validate_temp_decideg(decideg: int) -> None:
    if not (TEMP_MIN_DECIDEG <= decideg <= TEMP_MAX_DECIDEG):
        raise ValidationError(f"Temperature {decideg} decideg outside [{TEMP_MIN_DECIDEG}, {TEMP_MAX_DECIDEG}]")
