"""C-15: FakeEB300 end-to-end — handshake, requests, and every error path.

Highest-value test in the plan: exercises handshake -> keys -> encrypt ->
inner message -> response routing with no radio, deterministically.
"""

from __future__ import annotations

import asyncio
import struct
import time

import pytest
from eb300_ble.client import EB300Client
from eb300_ble.const import (
    PID,
    ErrorCode,
    KeyLock,
    Language,
    Operation,
    OuterMessageType,
    Program,
    ScreensaverType,
)
from eb300_ble.exceptions import (
    DeviceError,
    EB300ConnectionError,
    HandshakeError,
    RequestTimeoutError,
    ValidationError,
)
from eb300_ble.models import DeviceInfo, HomeProgram
from eb300_ble.protocol import HomeProgramEvent, InnerMessage, build_outer

from .fakes import FakeEB300

PSK = bytes(range(32))


async def test_handshake_and_disconnect():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    assert client.is_connected
    await client.disconnect()
    assert not client.is_connected
    assert device.connect_calls == 1
    assert device.disconnect_calls == 1


async def test_handshake_fails_when_psk_not_provisioned():
    device = FakeEB300(PSK, provisioned=False)
    client = EB300Client(device, PSK)
    with pytest.raises(HandshakeError) as exc_info:
        await client.connect()
    assert exc_info.value.step == 1
    assert exc_info.value.error_code == ErrorCode.INVALID_PARAMETER


async def test_handshake_fails_with_wrong_psk():
    device = FakeEB300(PSK)
    wrong_psk = bytes(range(1, 33))
    client = EB300Client(device, wrong_psk)
    with pytest.raises(HandshakeError) as exc_info:
        await client.connect()
    assert exc_info.value.step == 3


async def test_read_device_info():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    info = await client.read_device_info()
    assert info == DeviceInfo(model="EB300", batch="B123456", serial="SN00001234", firmware_version="1.2.0")


async def test_read_status_matches_fake_state():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    status = await client.read_status()
    assert status == device.status


async def test_read_energy_meter():
    device = FakeEB300(PSK)
    device.energy_meter_minutes = 4321
    client = EB300Client(device, PSK)
    await client.connect()
    assert await client.read_energy_meter() == 4321


async def test_set_override_temperature_updates_status():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set(PID.OVERRIDE_TEMPERATURE, struct.pack("<H", 250))
    assert device.status.current_set_temperature == 250


async def test_unauthorized_pid_raises_device_error():
    device = FakeEB300(PSK)
    device.register_pid(PID.KEY_LOCK, lambda _msg: (ErrorCode.UNAUTHORIZED, b""))
    client = EB300Client(device, PSK)
    await client.connect()
    with pytest.raises(DeviceError) as exc_info:
        await client.get(PID.KEY_LOCK)
    assert exc_info.value.error_code == ErrorCode.UNAUTHORIZED


async def test_unknown_pid_raises_device_error():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    with pytest.raises(DeviceError) as exc_info:
        await client.get(0xFFFF)
    assert exc_info.value.error_code == ErrorCode.UNKNOWN_PID


async def test_invalid_parameter_raises_device_error():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    with pytest.raises(DeviceError) as exc_info:
        await client.set(PID.OVERRIDE_TEMPERATURE, struct.pack("<H", 999))
    assert exc_info.value.error_code == ErrorCode.INVALID_PARAMETER


async def test_counter_reuse_raises_verification_failed():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    # Pre-seed counter 1 as already-seen on the device side, so the client's
    # first (counter=1) request looks like a replay.
    device.mark_counter_seen(1)
    with pytest.raises(DeviceError) as exc_info:
        await client.ping()
    assert exc_info.value.error_code == ErrorCode.VERIFICATION_FAILED


async def test_unsolicited_data_push_between_request_and_response():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    received = []
    client.set_data_callback(received.append)
    await client.connect()

    device.queue_push(PID.THERMOSTAT_STATUS, device.status.to_bytes())
    info = await client.read_device_info()

    assert info.model == "EB300"
    assert len(received) == 1
    assert received[0].operation == Operation.DATA
    assert received[0].pid == PID.THERMOSTAT_STATUS


async def test_response_never_arrives_times_out_without_deadlock():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK, request_timeout=0.05)
    await client.connect()
    device.drop_next_response = True
    with pytest.raises(RequestTimeoutError):
        await client.ping()


async def test_async_context_manager():
    device = FakeEB300(PSK)
    async with EB300Client(device, PSK) as client:
        assert client.is_connected
    assert device.disconnect_calls == 1


async def test_request_before_connect_raises():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    with pytest.raises(EB300ConnectionError):
        await client.get(PID.PING)


async def test_malformed_notification_is_dropped_not_raised():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    client._on_notify(b"")  # too short to even read an outer header
    # The client must still be usable afterwards.
    await client.ping()


async def test_unexpected_outer_message_type_is_ignored():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    client._on_notify(build_outer(OuterMessageType.CLOSE_CONNECTION))
    await client.ping()


async def test_encrypted_data_before_handshake_is_dropped():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    client._on_notify(build_outer(OuterMessageType.ENCRYPTED_DATA, b"garbage"))
    assert not client.is_connected


async def test_dispatch_unknown_operation_is_ignored():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    client._dispatch_inner(InnerMessage(error=0, operation=0x0F, counter=1, pid=1, data=b""))


async def test_dispatch_stale_counter_response_is_dropped():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    client._dispatch_inner(InnerMessage(error=0, operation=Operation.GET_RESPONSE, counter=9999, pid=1, data=b""))


async def test_disconnect_cancels_pending_requests():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK, request_timeout=5.0)
    await client.connect()
    device.drop_next_response = True
    task = asyncio.create_task(client.get(PID.PING))
    await asyncio.sleep(0)  # let the request be sent and the future get registered
    await client.disconnect()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_request_batch_returns_responses_in_order():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()

    results = await client.request_batch(
        [
            (Operation.GET, PID.MODEL_NAME, b""),
            (Operation.GET, PID.FIRMWARE_VERSION, b""),
            (Operation.GET, PID.PING, b""),
        ]
    )

    assert [r.pid for r in results] == [PID.MODEL_NAME, PID.FIRMWARE_VERSION, PID.PING]
    assert results[0].data == b"EB300"
    assert results[1].data == b"1.2.0"


async def test_request_batch_empty_returns_empty():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    assert await client.request_batch([]) == []


async def test_request_batch_propagates_device_error():
    device = FakeEB300(PSK)
    device.register_pid(PID.KEY_LOCK, lambda _msg: (ErrorCode.UNAUTHORIZED, b""))
    client = EB300Client(device, PSK)
    await client.connect()
    with pytest.raises(DeviceError) as exc_info:
        await client.request_batch([(Operation.GET, PID.PING, b""), (Operation.GET, PID.KEY_LOCK, b"")])
    assert exc_info.value.error_code == ErrorCode.UNAUTHORIZED


async def test_reconnect_gets_fresh_session_and_counter():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.ping()  # consumes counter 1
    await client.disconnect()
    await client.connect()
    # A fresh session must start its counter at 1 again, and the device's
    # per-session seen-counter set must have been cleared by the new handshake.
    await client.ping()


# ── R2 write helpers ─────────────────────────────────────────────────────


async def test_set_power_updates_status():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_power(False)
    assert device.status.power_off is True
    await client.set_power(True)
    assert device.status.power_off is False


async def test_set_manual_temp_updates_fake():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_manual_temp(180)
    assert device.manual_control_temp == 180


@pytest.mark.parametrize("decideg", [49, 351, -1, 1000])
async def test_set_manual_temp_rejects_out_of_range(decideg: int):
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    with pytest.raises(ValidationError):
        await client.set_manual_temp(decideg)
    assert device.manual_control_temp == 220  # unchanged: rejected before any BLE write


@pytest.mark.parametrize("decideg", [50, 350])
async def test_set_override_temp_accepts_boundary_values(decideg: int):
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_override_temp(decideg)
    assert device.status.current_set_temperature == decideg


async def test_set_override_temp_rejects_out_of_range():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    with pytest.raises(ValidationError):
        await client.set_override_temp(351)


async def test_set_program_updates_fake():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_program(Program.HOME)
    assert device.status.current_program == Program.HOME


async def test_set_key_lock_updates_fake():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_key_lock(True)
    assert device.key_lock == KeyLock.LOCKED
    await client.set_key_lock(False)
    assert device.key_lock == KeyLock.UNLOCKED


async def test_set_language_updates_fake():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_language(Language.FINNISH)
    assert device.language == Language.FINNISH


async def test_set_screensaver_updates_fake():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_screensaver(ScreensaverType.TIME_DATE)
    assert device.screensaver == ScreensaverType.TIME_DATE


async def test_set_calibration_updates_fake_and_forces_relay_zero():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.set_calibration(room_decideg=5, floor_decideg=-10)
    assert device.calibration == (5, -10, 0)


@pytest.mark.parametrize(("room", "floor"), [(-51, 0), (0, 51), (100, 0)])
async def test_set_calibration_rejects_out_of_range(room: int, floor: int):
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    with pytest.raises(ValidationError):
        await client.set_calibration(room_decideg=room, floor_decideg=floor)
    assert device.calibration == (0, 0, 0)  # unchanged: rejected before any BLE write


async def test_sync_clock_explicit_timestamp():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    await client.sync_clock(1_700_000_000)
    assert device.device_time == 1_700_000_000


async def test_sync_clock_defaults_to_now():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    before = int(time.time())
    await client.sync_clock()
    assert abs(device.device_time - before) <= 2


# ── R3: home program ──────────────────────────────────────────────────────


def _one_valid_day() -> list[HomeProgramEvent]:
    return [
        HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=220),
        HomeProgramEvent(active=True, hour=8, minute=0, temperature_decideg=170),
        HomeProgramEvent(active=False, hour=15, minute=0, temperature_decideg=220),
        HomeProgramEvent(active=True, hour=23, minute=0, temperature_decideg=170),
    ]


def _valid_program() -> HomeProgram:
    return HomeProgram(days=[_one_valid_day() for _ in range(7)])


async def test_read_home_program_matches_fake_default():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    program = await client.read_home_program()
    assert program.to_bytes() == device.home_program


async def test_set_home_program_round_trips_through_fake():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    program = _valid_program()

    await client.set_home_program(program)
    assert device.home_program == program.to_bytes()

    read_back = await client.read_home_program()
    assert read_back == program


async def test_set_home_program_rejects_invalid_before_any_write():
    device = FakeEB300(PSK)
    client = EB300Client(device, PSK)
    await client.connect()
    original = device.home_program
    days = _valid_program().days
    days[0][0] = HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=1000)
    invalid_program = HomeProgram(days=days)

    with pytest.raises(ValidationError):
        await client.set_home_program(invalid_program)
    assert device.home_program == original  # unchanged: rejected before any BLE write
