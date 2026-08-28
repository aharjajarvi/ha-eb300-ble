"""FakeEB300: implements the *device* side of the handshake + a PID table.

Plugs into EB300Client through the Transport protocol, so the entire client
stack — handshake, key derivation, encrypt/decrypt, request/response routing,
batching, error paths — is exercised with no radio and no real device.
"""

from __future__ import annotations

import hmac
import os
import struct
import time
from collections.abc import Callable

from eb300_ble.const import (
    NONCE_LEN,
    PID,
    ErrorCode,
    Operation,
    OuterMessageType,
)
from eb300_ble.crypto import compute_hmac, derive_keys, unwrap, wrap
from eb300_ble.exceptions import CryptoError
from eb300_ble.models import ThermostatStatus
from eb300_ble.protocol import (
    HomeProgramEvent,
    InnerMessage,
    build_inner_message,
    build_outer,
    pack_home_program,
    parse_inner_messages,
    parse_outer,
)


class FakeEB300:
    """A fake EB300 device. Implements client.Transport."""

    def __init__(self, psk: bytes, *, provisioned: bool = True) -> None:
        self._psk = psk
        self.provisioned = provisioned

        self._callback: Callable[[bytes], None] | None = None
        self._client_nonce: bytes | None = None
        self._server_nonce: bytes | None = None
        self._session_key: bytes | None = None
        self._seen_counters: set[int] = set()
        self._device_counter = 1000  # separate namespace from the client's own counter

        self._pid_table: dict[int, Callable[[InnerMessage], tuple[int, bytes]]] = {}
        self._pending_push: tuple[int, bytes] | None = None
        self.drop_next_response = False
        self.connect_calls = 0
        self.disconnect_calls = 0

        self.status = ThermostatStatus(
            error_flags=0,
            current_set_temperature=220,
            limiting_temperature=220,
            time_to_target=0,
            relay_on=False,
            in_error_state=False,
            limited_by_limiting_sensor=False,
            power_off=False,
            room_temperature=210,
            floor_temperature=220,
            relay_temperature=230,
            room_sensor_error=0,
            floor_sensor_error=0,
            current_program=0,
            energy_meter=0,
        )
        self.model = "EB300"
        self.batch = "B123456"
        self.serial = "SN00001234"
        self.firmware_version = "1.2.0"
        self.energy_meter_minutes = 0
        self.device_time = int(time.time())
        self.manual_control_temp = 220
        self.key_lock = 0
        self.language = 1
        self.screensaver = 0
        self.calibration = (0, 0, 0)
        # A plausible default schedule, not all-zero bytes: a real device never
        # reports temperature_decideg=0 for any event, active or not (docs/HARDWARE_NOTES.md
        # docs/HARDWARE_NOTES.md), so an all-zero default would make this fake unrepresentative of
        # real hardware for any test that reads the program without first
        # writing one.
        self.home_program = _default_home_program()

        self._install_default_handlers()

    # ── Transport protocol ───────────────────────────────────────────────

    async def connect(self) -> None:
        self.connect_calls += 1

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._client_nonce = None
        self._server_nonce = None
        self._seen_counters.clear()

    def set_notification_callback(self, callback: Callable[[bytes], None]) -> None:
        self._callback = callback

    async def write(self, data: bytes) -> None:
        for frame in self._handle(data):
            if self._callback is not None:
                self._callback(frame)

    # ── Test control surface ─────────────────────────────────────────────

    def register_pid(self, pid: int, handler: Callable[[InnerMessage], tuple[int, bytes]]) -> None:
        self._pid_table[pid] = handler

    def queue_push(self, pid: int, data: bytes) -> None:
        """Emit an unsolicited DATA push ahead of the next request's response."""
        self._pending_push = (pid, data)

    def mark_counter_seen(self, counter: int) -> None:
        """Pre-seed a counter as already-used, so the next request using it looks replayed."""
        self._seen_counters.add(counter)

    # ── Device-side protocol handling ────────────────────────────────────

    def _handle(self, data: bytes) -> list[bytes]:
        _, msg_type, payload = parse_outer(data)
        if msg_type == OuterMessageType.CLIENT_NONCE:
            return [self._handle_client_nonce(payload)]
        if msg_type == OuterMessageType.CLIENT_HMAC:
            return [self._handle_client_hmac(payload)]
        if msg_type == OuterMessageType.ENCRYPTED_DATA:
            return self._handle_encrypted(payload)
        return [build_outer(OuterMessageType.ERROR, bytes([ErrorCode.NOT_IMPLEMENTED]))]

    def _handle_client_nonce(self, payload: bytes) -> bytes:
        if not self.provisioned:
            return build_outer(OuterMessageType.ERROR, bytes([ErrorCode.INVALID_PARAMETER]))
        self._client_nonce = payload[:NONCE_LEN]
        self._server_nonce = os.urandom(NONCE_LEN)
        return build_outer(OuterMessageType.SERVER_NONCE, self._server_nonce)

    def _handle_client_hmac(self, payload: bytes) -> bytes:
        assert self._client_nonce is not None and self._server_nonce is not None
        keys = derive_keys(self._psk, self._client_nonce, self._server_nonce)
        expected_client_hmac = compute_hmac(keys.hmac_key, self._client_nonce, self._server_nonce, b"_CLIENT")
        if not hmac.compare_digest(expected_client_hmac, payload[:32]):
            return build_outer(OuterMessageType.ERROR, bytes([ErrorCode.VERIFICATION_FAILED]))
        self._session_key = keys.session_key
        self._seen_counters.clear()
        return build_outer(
            OuterMessageType.SERVER_HMAC,
            compute_hmac(keys.hmac_key, self._client_nonce, self._server_nonce, b"_SERVER"),
        )

    def _handle_encrypted(self, payload: bytes) -> list[bytes]:
        if self._session_key is None:
            return [build_outer(OuterMessageType.ERROR, bytes([ErrorCode.CRYPTO_ERROR]))]
        try:
            inner_payload = unwrap(self._session_key, payload)
        except CryptoError:
            return [build_outer(OuterMessageType.ERROR, bytes([ErrorCode.CRYPTO_ERROR]))]

        frames: list[bytes] = []

        if self._pending_push is not None:
            push_pid, push_data = self._pending_push
            self._pending_push = None
            self._device_counter += 1
            push_msg = build_inner_message(Operation.DATA, push_pid, self._device_counter, push_data)
            frames.append(build_outer(OuterMessageType.ENCRYPTED_DATA, wrap(self._session_key, push_msg)))

        responses = bytearray()
        for msg in parse_inner_messages(inner_payload):
            if msg.counter in self._seen_counters:
                response_op = Operation.SET_RESPONSE if msg.operation == Operation.SET else Operation.GET_RESPONSE
                responses += build_inner_message(
                    response_op, msg.pid, msg.counter, b"", error=ErrorCode.VERIFICATION_FAILED
                )
                continue
            self._seen_counters.add(msg.counter)

            if self.drop_next_response:
                self.drop_next_response = False
                continue

            handler = self._pid_table.get(msg.pid)
            if handler is None:
                error, resp_data = ErrorCode.UNKNOWN_PID, b""
            else:
                error, resp_data = handler(msg)
            response_op = Operation.SET_RESPONSE if msg.operation == Operation.SET else Operation.GET_RESPONSE
            responses += build_inner_message(response_op, msg.pid, msg.counter, resp_data, error=error)

        if responses:
            frames.append(build_outer(OuterMessageType.ENCRYPTED_DATA, wrap(self._session_key, bytes(responses))))
        return frames

    # ── Default PID table ────────────────────────────────────────────────

    def _install_default_handlers(self) -> None:
        def _string(value: str) -> Callable[[InnerMessage], tuple[int, bytes]]:
            return lambda _msg, v=value: (ErrorCode.OK, v.encode("utf-8"))

        self.register_pid(PID.MODEL_NAME, _string(self.model))
        self.register_pid(PID.BATCH_NAME, _string(self.batch))
        self.register_pid(PID.SERIAL_NUMBER, _string(self.serial))
        self.register_pid(PID.FIRMWARE_VERSION, _string(self.firmware_version))
        self.register_pid(PID.PING, lambda _msg: (ErrorCode.OK, b""))

        def _ntp_time(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                self.device_time = struct.unpack("<I", msg.data)[0]
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, struct.pack("<I", self.device_time)

        self.register_pid(PID.NTP_TIME, _ntp_time)

        def _thermostat_status(_msg: InnerMessage) -> tuple[int, bytes]:
            return ErrorCode.OK, self.status.to_bytes()

        self.register_pid(PID.THERMOSTAT_STATUS, _thermostat_status)

        def _energy_meter(_msg: InnerMessage) -> tuple[int, bytes]:
            return ErrorCode.OK, struct.pack("<I", self.energy_meter_minutes)

        self.register_pid(PID.ENERGY_METER, _energy_meter)

        def _power_on(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 1 or msg.data[0] not in (0, 1):
                    return ErrorCode.INVALID_PARAMETER, b""
                self.status = _replace(self.status, power_off=(msg.data[0] == 0))
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, bytes([0 if self.status.power_off else 1])

        self.register_pid(PID.POWER_ON, _power_on)

        def _override_temperature(msg: InnerMessage) -> tuple[int, bytes]:
            if len(msg.data) != 2:
                return ErrorCode.INVALID_LENGTH, b""
            value = struct.unpack("<H", msg.data)[0]
            from eb300_ble.const import TEMP_MAX_DECIDEG, TEMP_MIN_DECIDEG

            if not (TEMP_MIN_DECIDEG <= value <= TEMP_MAX_DECIDEG):
                return ErrorCode.INVALID_PARAMETER, b""
            self.status = _replace(self.status, current_set_temperature=value)
            return ErrorCode.OK, msg.data

        self.register_pid(PID.OVERRIDE_TEMPERATURE, _override_temperature)

        def _manual_control_temp(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 2:
                    return ErrorCode.INVALID_LENGTH, b""
                value = struct.unpack("<H", msg.data)[0]
                from eb300_ble.const import TEMP_MAX_DECIDEG, TEMP_MIN_DECIDEG

                if not (TEMP_MIN_DECIDEG <= value <= TEMP_MAX_DECIDEG):
                    return ErrorCode.INVALID_PARAMETER, b""
                self.manual_control_temp = value
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, struct.pack("<H", self.manual_control_temp)

        self.register_pid(PID.MANUAL_CONTROL_TEMP, _manual_control_temp)

        def _selected_program(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 1 or msg.data[0] not in (0, 1):
                    return ErrorCode.INVALID_PARAMETER, b""
                self.status = _replace(self.status, current_program=msg.data[0])
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, bytes([self.status.current_program])

        self.register_pid(PID.SELECTED_PROGRAM, _selected_program)

        def _key_lock(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 1 or msg.data[0] not in (0, 1):
                    return ErrorCode.INVALID_PARAMETER, b""
                self.key_lock = msg.data[0]
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, bytes([self.key_lock])

        self.register_pid(PID.KEY_LOCK, _key_lock)

        def _language(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 1 or not (0 <= msg.data[0] <= 5):
                    return ErrorCode.INVALID_PARAMETER, b""
                self.language = msg.data[0]
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, bytes([self.language])

        self.register_pid(PID.LANGUAGE, _language)

        def _screensaver(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 1 or not (0 <= msg.data[0] <= 3):
                    return ErrorCode.INVALID_PARAMETER, b""
                self.screensaver = msg.data[0]
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, bytes([self.screensaver])

        self.register_pid(PID.SCREENSAVER_TYPE, _screensaver)

        def _calibration(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 6:
                    return ErrorCode.INVALID_LENGTH, b""
                room, floor, relay = struct.unpack("<hhh", msg.data)
                from eb300_ble.const import CALIBRATION_MAX_DECIDEG, CALIBRATION_MIN_DECIDEG

                if not all(CALIBRATION_MIN_DECIDEG <= v <= CALIBRATION_MAX_DECIDEG for v in (room, floor)):
                    return ErrorCode.INVALID_PARAMETER, b""
                relay = 0  # forced to 0 by the device, per Open API §5.6
                self.calibration = (room, floor, relay)
                return ErrorCode.OK, struct.pack("<hhh", room, floor, relay)
            return ErrorCode.OK, struct.pack("<hhh", *self.calibration)

        self.register_pid(PID.CALIBRATION_USER, _calibration)

        def _home_program(msg: InnerMessage) -> tuple[int, bytes]:
            if msg.operation == Operation.SET:
                if len(msg.data) != 112:
                    return ErrorCode.INVALID_LENGTH, b""
                self.home_program = msg.data
                return ErrorCode.OK, msg.data
            return ErrorCode.OK, self.home_program

        self.register_pid(PID.HOME_PROGRAM, _home_program)


def _default_home_program() -> bytes:
    day = [
        HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=220),
        HomeProgramEvent(active=True, hour=8, minute=0, temperature_decideg=170),
        HomeProgramEvent(active=True, hour=15, minute=0, temperature_decideg=220),
        HomeProgramEvent(active=True, hour=23, minute=0, temperature_decideg=170),
    ]
    return pack_home_program([day.copy() for _ in range(7)])


def _replace(status: ThermostatStatus, **changes: object) -> ThermostatStatus:
    from dataclasses import replace

    return replace(status, **changes)
