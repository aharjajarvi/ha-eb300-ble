"""The coordinator's write boundary, poll assembly, and program merge.

Two of the four write-path rules in docs/HARDWARE_NOTES.md live here rather
than in climate.py:

  Rule 3 -- raise `HomeAssistantError` at the coordinator write boundary. HA
  treats any *other* exception escaping a service call as an integration bug:
  full traceback at ERROR, and no readable message for the user. Doing the
  translation once here beats doing it in each of the seven entity write
  methods, and is invisible until someone adds an eighth.

  Rule 4 -- normalise a bare `TimeoutError`. `str(TimeoutError())` is the empty
  string, so any message built from it trails off after the colon.

`_poll` is covered here too: it is the only place the four batched config GETs
are matched up with their responses positionally, which is the kind of thing
that breaks silently and reads fine.
"""
import struct
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from conftest import ADDRESS, PSK_B64, make_status
from eb300_ble.config_flow import CONF_ADDRESS
from eb300_ble.const import CONF_PSK, CONNECT_RETRY_ATTEMPTS, DOMAIN, POST_WRITE_SETTLE_SECONDS, WEEKDAYS
from eb300_ble.coordinator import EB300Coordinator, merge_home_program
from eb300_ble.eb300_ble.const import (
    PID,
    KeyLock,
    Language,
    Operation,
    Program,
    ScreensaverType,
)
from eb300_ble.eb300_ble.exceptions import (
    DeviceError,
    EB300ConnectionError,
    EB300Error,
    ProtocolError,
    RequestTimeoutError,
    ValidationError,
)
from eb300_ble.eb300_ble.models import DeviceInfo, HomeProgram
from eb300_ble.eb300_ble.protocol import HomeProgramEvent
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed


def _coordinator(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS, CONF_PSK: PSK_B64}
    )
    entry.add_to_hass(hass)
    return EB300Coordinator(hass, entry, ADDRESS, b"\x00" * 32, 60)


# --- Rule 3: nothing but HomeAssistantError leaves a write ----------------

WRITE_ERRORS = [
    EB300Error("generic"),
    EB300ConnectionError("out of range"),
    DeviceError(5, pid=0x10D0),
    RequestTimeoutError(0x10D0, 7, 10.0),
    ProtocolError("garbage on the wire"),
    ValidationError("42.0 C is out of range"),
]


@pytest.mark.parametrize("error", WRITE_ERRORS, ids=lambda e: type(e).__name__)
async def test_every_library_error_leaves_a_write_as_home_assistant_error(hass, error):
    """Anything else gets HA's "this integration is broken" traceback treatment."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", side_effect=error),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await coordinator.async_set_power(True)

    assert not isinstance(caught.value, ServiceValidationError)
    assert str(error) in str(caught.value)
    assert caught.value.__cause__ is error


async def test_a_bare_timeout_still_produces_a_readable_message(hass):
    """Rule 4. `str(TimeoutError())` is empty -- a message built from it ends
    in a bare colon and tells the user nothing."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", side_effect=TimeoutError()),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await coordinator.async_set_power(True)

    message = str(caught.value)
    assert not message.rstrip().endswith(":")
    assert ADDRESS in message
    assert f"{CONNECT_RETRY_ATTEMPTS} attempt" in message


@pytest.mark.parametrize(
    "method, args",
    [
        ("async_set_power", (True,)),
        ("async_set_manual_temp", (220,)),
        ("async_set_override_temp", (220,)),
        ("async_set_key_lock", (True,)),
        ("async_set_program", (Program.HOME,)),
        ("async_set_language", (Language.FINNISH,)),
        ("async_set_screensaver", (ScreensaverType.OFF,)),
        ("async_sync_clock", ()),
    ],
)
async def test_every_setter_goes_through_the_same_boundary(hass, method, args):
    """A new setter that forgets `_write` would raise a raw library error."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", side_effect=EB300Error("nope")),
        pytest.raises(HomeAssistantError),
    ):
        await getattr(coordinator, method)(*args)


async def test_calibration_goes_through_the_boundary_too(hass):
    """Keyword-only, so it does not fit the table above."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", side_effect=EB300Error("nope")),
        pytest.raises(HomeAssistantError),
    ):
        await coordinator.async_set_calibration(room_decideg=10, floor_decideg=0)


def _writing_client():
    client = MagicMock()
    for setter in ("set_power", "set_program", "set_override_temp", "set_manual_temp", "set_key_lock"):
        setattr(client, setter, AsyncMock())
    return client


def _after_settle(hass):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=POST_WRITE_SETTLE_SECONDS + 1))


@pytest.mark.parametrize(
    "method, args",
    [
        ("async_set_program", (Program.HOME,)),
        ("async_set_power", (True,)),
        ("async_set_override_temp", (215,)),
        ("async_set_manual_temp", (215,)),
        ("async_set_key_lock", (True,)),
    ],
)
async def test_a_successful_write_polls_once_after_the_device_settles(hass, method, args):
    """Without the poll the UI shows the old value until the next scheduled one.

    Not immediately: a status read right after a SET returned the pre-write
    setpoint or program 5 times out of 5 on hardware (2026-09-26), and the
    entity then showed that stale value for a whole poll interval."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(_writing_client())),
        patch.object(EB300Coordinator, "async_refresh", AsyncMock()) as refresh,
        patch.object(EB300Coordinator, "async_request_refresh", AsyncMock()) as debounced,
    ):
        await getattr(coordinator, method)(*args)
        await hass.async_block_till_done()
        refresh.assert_not_awaited()

        _after_settle(hass)
        await hass.async_block_till_done()

    refresh.assert_awaited_once()
    # The debounced path's 10 s cooldown deferred a second edit's refresh.
    debounced.assert_not_awaited()
    await coordinator.async_shutdown()


async def test_a_failed_write_does_not_ask_for_a_poll(hass):
    """The refresh would connect a second time to a device that just refused one."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", side_effect=EB300Error("nope")),
        patch.object(EB300Coordinator, "async_refresh", AsyncMock()) as refresh,
        pytest.raises(HomeAssistantError),
    ):
        await coordinator.async_set_power(True)
    _after_settle(hass)
    await hass.async_block_till_done()

    refresh.assert_not_awaited()


async def test_back_to_back_writes_share_one_settle_poll(hass):
    """Each settle poll is a BLE connection; a second write restarts the wait
    rather than queueing a second one behind the first."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(_writing_client())),
        patch.object(EB300Coordinator, "async_refresh", AsyncMock()) as refresh,
    ):
        await coordinator.async_set_program(Program.MANUAL)
        await coordinator.async_set_override_temp(215)
        _after_settle(hass)
        await hass.async_block_till_done()

    refresh.assert_awaited_once()


async def test_shutdown_cancels_a_pending_settle_poll(hass):
    """Otherwise an unloaded entry connects to the device one last time."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(_writing_client())),
        patch.object(EB300Coordinator, "async_refresh", AsyncMock()) as refresh,
    ):
        await coordinator.async_set_program(Program.MANUAL)
        await coordinator.async_shutdown()
        _after_settle(hass)
        await hass.async_block_till_done()

    refresh.assert_not_awaited()


# --- The home-program path, which is deliberately NOT `_write` ------------


def _program(temp=220):
    day = [
        HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=temp),
        HomeProgramEvent(active=True, hour=8, minute=0, temperature_decideg=170),
        HomeProgramEvent(active=True, hour=15, minute=0, temperature_decideg=temp),
        HomeProgramEvent(active=True, hour=23, minute=0, temperature_decideg=170),
    ]
    return HomeProgram(days=[list(day) for _ in range(7)])


def _client_for(program, *, readback=None):
    """A stand-in client whose set() is a no-op and whose reads are scripted."""
    client = MagicMock()
    reads = [program, readback if readback is not None else program]
    client.read_home_program = AsyncMock(side_effect=reads)
    client.set_home_program = AsyncMock()
    return client


def _with_client_running(client):
    """Run the operation the coordinator hands to `_with_client` against `client`.

    Patched onto the class, so it arrives as an unbound function and takes
    `self`. Everything above the transport -- the merge, the readback check,
    the error translation -- stays live; only the radio is gone.
    """

    async def _run(self, op):
        return await op(client)

    return _run


async def test_a_bad_schedule_is_a_user_error_not_a_connectivity_one(hass):
    """`ServiceValidationError` renders as a message; `HomeAssistantError`
    renders as an integration failure. A typo'd temperature is the former.

    This is why `async_set_home_program` does not reuse `_write`: folding
    `ValidationError` into `_write`'s catch-all would change the behaviour for
    every other write method too.
    """
    coordinator = _coordinator(hass)
    client = _client_for(_program())
    client.set_home_program = AsyncMock(side_effect=ValidationError("42.0 C out of range"))

    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(client)),
        pytest.raises(ServiceValidationError, match="42.0 C out of range"),
    ):
        await coordinator.async_set_home_program({"monday": [{"time": "06:00", "temperature": 42.0}]})


@pytest.mark.parametrize(
    "error", [EB300ConnectionError("gone"), TimeoutError()], ids=["connection", "bare_timeout"]
)
async def test_a_connectivity_failure_on_the_program_path_is_not_a_user_error(hass, error):
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", side_effect=error),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await coordinator.async_set_home_program({"monday": [{"time": "06:00", "temperature": 22.0}]})

    assert not isinstance(caught.value, ServiceValidationError)
    assert not str(caught.value).rstrip().endswith(":")  # rule 4, on this path too


async def test_a_readback_that_does_not_match_is_reported(hass):
    """The device acknowledges a schedule write it did not fully apply.

    Without the verify-readback the service returns success and the user finds
    out days later, from the heating.
    """
    coordinator = _coordinator(hass)
    client = _client_for(_program(), readback=_program(temp=999))

    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(client)),
        pytest.raises(HomeAssistantError, match="did not verify"),
    ):
        await coordinator.async_set_home_program({"monday": [{"time": "06:00", "temperature": 22.0}]})


async def test_a_verified_program_write_polls_after_the_settle_delay(hass):
    """Not an immediate refresh: in the Home program a new schedule can change
    the setpoint in force, and a status read straight after a SET still returns
    the old one -- the same reason every other write waits
    POST_WRITE_SETTLE_SECONDS (docs/HARDWARE_NOTES.md)."""
    coordinator = _coordinator(hass)
    updates = {"monday": [{"time": "06:00", "temperature": 22.0}]}
    merged = merge_home_program(_program(), updates)
    client = _client_for(_program(), readback=merged)

    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(client)),
        patch.object(EB300Coordinator, "async_request_refresh", AsyncMock()) as immediate,
        patch.object(EB300Coordinator, "_schedule_settle_refresh") as settle,
    ):
        await coordinator.async_set_home_program(updates)

    client.set_home_program.assert_awaited_once()
    settle.assert_called_once_with()
    immediate.assert_not_awaited()


@pytest.mark.parametrize(
    "error", [TimeoutError(), EB300ConnectionError("out of range")], ids=["timeout", "connection"]
)
async def test_reading_the_program_translates_errors_too(hass, error):
    """`get_home_program` is a service with a response; a raw library error here
    reaches the user as an integration traceback, same as on the write side."""
    coordinator = _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", side_effect=error),
        pytest.raises(HomeAssistantError) as caught,
    ):
        await coordinator.async_get_home_program()

    assert not str(caught.value).rstrip().endswith(":")
    assert ADDRESS in str(caught.value)


async def test_reading_the_program_returns_what_the_device_has(hass):
    coordinator = _coordinator(hass)
    client = _client_for(_program())

    with patch.object(EB300Coordinator, "_with_client", _with_client_running(client)):
        assert await coordinator.async_get_home_program() == _program()


# --- `_poll`: four batched GETs matched to their responses positionally ---


def _response(payload: bytes):
    return MagicMock(data=payload)


_CALIBRATION = struct.pack("<hhh", 5, -3, 0)


def _polling_client(
    *,
    key_lock=bytes([KeyLock.LOCKED]),
    language=bytes([Language.FINNISH]),
    screensaver=bytes([ScreensaverType.TEMPERATURE]),
    calibration=_CALIBRATION,
    status=None,
):
    client = MagicMock()
    client.read_device_info = AsyncMock(
        return_value=DeviceInfo(model="EB-Therm 300", batch="2603", serial="123456", firmware_version="1.2")
    )
    client.read_status = AsyncMock(return_value=status if status is not None else make_status())
    client.request_batch = AsyncMock(
        return_value=[_response(key_lock), _response(language), _response(screensaver), _response(calibration)]
    )
    return client


async def _poll_once(hass, client, coordinator=None):
    coordinator = coordinator or _coordinator(hass)
    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(client)),
        patch("eb300_ble.coordinator.bluetooth.async_last_service_info", return_value=None),
    ):
        return await coordinator._async_update_data()


# --- `_poll`: a value with no name here costs one entity, not the device ----


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("key_lock", {"key_lock": b"\x07"}),
        ("language", {"language": b"\x09"}),
        ("screensaver", {"screensaver": b"\x0a"}),
    ],
)
async def test_an_unknown_setting_value_reads_as_none_and_the_poll_succeeds(hass, field, kwargs):
    """A firmware that adds a display language must not take every entity of the
    device unavailable: before this, `Language(9)` raised a bare `ValueError` out
    of `_poll`, which HA logs as an unexpected error and fails the whole update."""
    data = await _poll_once(hass, _polling_client(**kwargs))

    assert getattr(data, field) is None
    # the rest of the poll is intact
    assert data.status.current_set_temperature == 200
    assert (data.calibration_room_decideg, data.calibration_floor_decideg) == (5, -3)


async def test_an_unknown_program_reads_as_none(hass):
    data = await _poll_once(hass, _polling_client(status=make_status(current_program=5)))

    assert data.program is None
    assert data.status.current_program == 5


async def test_an_unknown_value_is_logged_once_not_every_poll(hass, caplog):
    coordinator = _coordinator(hass)
    client = _polling_client(language=b"\x09")

    await _poll_once(hass, client, coordinator)
    await _poll_once(hass, client, coordinator)

    warnings = [r for r in caplog.records if "does not recognise" in r.getMessage()]
    assert len(warnings) == 1
    assert "language 9" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "kwargs",
    [{"language": b""}, {"calibration": b"\x05\x00"}],
    ids=["empty setting", "short calibration"],
)
async def test_a_malformed_config_payload_fails_the_poll_cleanly(hass, kwargs):
    """Malformed is different from unknown: it is a protocol error, and it fails
    the poll as `UpdateFailed` like any other -- not as a bare IndexError or
    struct.error that HA reports as an integration bug."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    with pytest.raises(UpdateFailed):
        await _poll_once(hass, _polling_client(**kwargs))


async def test_a_poll_assembles_every_field_from_its_own_response(hass):
    """The four config GETs are matched to their responses by position. Reorder
    either list and the language becomes the key lock, silently."""
    coordinator = _coordinator(hass)
    client = _polling_client()

    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(client)),
        patch("eb300_ble.coordinator.bluetooth.async_last_service_info", return_value=MagicMock(rssi=-71)),
    ):
        data = await coordinator._async_update_data()

    assert data.key_lock is KeyLock.LOCKED
    assert data.language is Language.FINNISH
    assert data.screensaver is ScreensaverType.TEMPERATURE
    assert (data.calibration_room_decideg, data.calibration_floor_decideg) == (5, -3)
    assert data.rssi == -71
    assert data.status.current_set_temperature == 200

    # The batch is one frame, in the declared order -- four separate round trips
    # per poll cycle is what this replaced.
    requested = [(op, pid) for op, pid, _ in client.request_batch.await_args.args[0]]
    assert requested == [
        (Operation.GET, PID.KEY_LOCK),
        (Operation.GET, PID.LANGUAGE),
        (Operation.GET, PID.SCREENSAVER_TYPE),
        (Operation.GET, PID.CALIBRATION_USER),
    ]


async def test_a_poll_with_no_advertisement_reports_no_rssi(hass):
    """`None`, not a stale number: HA renders it as unknown rather than as a
    signal strength that has not been seen for hours."""
    coordinator = _coordinator(hass)
    client = _polling_client()

    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(client)),
        patch("eb300_ble.coordinator.bluetooth.async_last_service_info", return_value=None),
    ):
        data = await coordinator._async_update_data()

    assert data.rssi is None


async def test_device_info_is_read_once_and_cached(hass):
    """Model/serial/firmware never change post-pairing; re-reading them every
    cycle is a round trip per poll for nothing."""
    coordinator = _coordinator(hass)
    client = _polling_client()

    with (
        patch.object(EB300Coordinator, "_with_client", _with_client_running(client)),
        patch("eb300_ble.coordinator.bluetooth.async_last_service_info", return_value=None),
    ):
        first = await coordinator._async_update_data()
        second = await coordinator._async_update_data()

    assert client.read_device_info.await_count == 1
    assert client.read_status.await_count == 2
    assert first.device_info == second.device_info


# --- `merge_home_program`: the branches the service schema hides ----------


def test_more_than_four_events_is_rejected_at_the_merge_too(hass):
    """The service schema catches this first (`vol.Length(max=4)`), so this
    branch is only reachable by a caller that bypasses it -- which is exactly
    why it must not be removed as unreachable."""
    with pytest.raises(ValidationError, match="at most 4 events"):
        merge_home_program(
            _program(),
            {"monday": [{"time": f"0{i}:00", "temperature": 20.0} for i in range(5)]},
        )


def test_a_short_day_keeps_the_devices_own_values_for_the_rest():
    """The device always stores 4 slots. A 2-event edit must not zero the other
    two -- it disables them while keeping the times and temperatures that are
    already there."""
    current = _program()
    merged = merge_home_program(
        current, {"monday": [{"time": "07:00", "temperature": 21.0}, {"time": "09:00", "temperature": 18.0}]}
    )

    assert [e.active for e in merged.days[0]] == [True, True, False, False]
    assert merged.days[0][2] == HomeProgramEvent(
        active=False, hour=15, minute=0, temperature_decideg=220
    )
    assert merged.days[1] == current.days[1]  # tuesday untouched


def test_a_short_day_never_puts_the_padded_slots_out_of_order():
    """The README's own example, against a normal 4-event day. The device day
    here is 06:00 / 08:00 / 15:00 / 23:00; the edit gives 08:00 and 23:30. Slot
    3's existing 15:00 now falls before the given 23:30 -- padding it with the
    existing time put the day out of order, and the device refused an edit whose
    every given event was valid, naming slots the user never wrote."""
    merged = merge_home_program(
        _program(),
        {"saturday": [{"time": "08:00", "temperature": 22.0}, {"time": "23:30", "temperature": 17.0}]},
    )

    saturday = merged.days[5]
    assert [(e.active, e.hour, e.minute) for e in saturday] == [
        (True, 8, 0),
        (True, 23, 30),
        (False, 23, 30),  # was 15:00: clamped up to the slot ahead of it
        (False, 23, 30),  # was 23:00: likewise
    ]
    # the device's own temperatures are kept, only the times move
    assert [e.temperature_decideg for e in saturday[2:]] == [220, 170]
    assert merged.to_bytes()


def test_a_padded_slot_that_is_still_in_order_keeps_its_own_time():
    """Clamping is only for slots that would otherwise break the order."""
    merged = merge_home_program(_program(), {"monday": [{"time": "05:00", "temperature": 21.0}]})

    assert [(e.hour, e.minute) for e in merged.days[0]] == [(5, 0), (8, 0), (15, 0), (23, 0)]
    assert merged.to_bytes()


def test_padding_respects_the_0200_daybreak():
    """01:00 is the *end* of the schedule day, so an existing 23:00 slot after a
    given 01:00 is out of order and gets clamped -- to 01:00, not left at 23:00."""
    merged = merge_home_program(
        _program(),
        {"monday": [
            {"time": "06:00", "temperature": 22.0},
            {"time": "22:00", "temperature": 17.0},
            {"time": "01:00", "temperature": 16.0},
        ]},
    )

    assert (merged.days[0][3].hour, merged.days[0][3].minute) == (1, 0)
    assert merged.to_bytes()


def test_a_short_day_over_a_short_existing_day_repeats_the_last_given_event():
    """The fallback for a device day that is itself under-length -- not expected
    from real firmware, but the merge must still emit exactly 4 in-order slots
    rather than an implicit 00:00 that would break the ordering check."""
    stunted = HomeProgram(days=[[HomeProgramEvent(active=True, hour=6, minute=0, temperature_decideg=220)]] * 7)

    merged = merge_home_program(stunted, {"monday": [{"time": "07:00", "temperature": 21.0}]})

    assert len(merged.days[0]) == 4
    assert [e.active for e in merged.days[0]] == [True, False, False, False]
    assert all(e.hour == 7 and e.temperature_decideg == 210 for e in merged.days[0][1:])


def test_a_day_not_mentioned_is_left_byte_for_byte_alone():
    current = _program()
    merged = merge_home_program(current, {"wednesday": [{"time": "05:00", "temperature": 23.0}]})

    for index, day in enumerate(WEEKDAYS):
        if day != "wednesday":
            assert merged.days[index] == current.days[index], day


def test_a_day_given_no_events_at_all_still_emits_four_slots():
    """Neither the schema nor the UI can produce this -- `cv.ensure_list` of an
    empty list is an empty list, and `_collect_updates` would still pass it
    through. Four in-order slots is the only shape `to_bytes()` accepts, so the
    merge has to invent them rather than emit a short day."""
    stunted = HomeProgram(days=[[] for _ in range(7)])

    merged = merge_home_program(stunted, {"monday": []})

    assert len(merged.days[0]) == 4
    assert all(not e.active and e.hour == 0 and e.temperature_decideg == 0 for e in merged.days[0])


# --- `_run_once`: routing the connection through HA, not around it -------


async def test_a_device_no_scanner_can_see_fails_before_any_connect(hass):
    """Resolution goes through HA's Bluetooth manager, which already tracks
    every proxy and their signal quality. Handing a bare address to
    `BleakTransport` instead would make it run its own uncoordinated
    `find_device_by_address` -- the pattern habluetooth warns about
    (docs/HARDWARE_NOTES.md)."""
    coordinator = _coordinator(hass)

    with patch(
        "eb300_ble.coordinator.bluetooth.async_ble_device_from_address", return_value=None
    ) as resolve, pytest.raises(HomeAssistantError, match="not currently visible"):
        await coordinator.async_set_power(True)

    assert resolve.call_args.args[1] == ADDRESS
    assert resolve.call_args.kwargs["connectable"] is True


async def test_an_unreachable_device_is_retried_then_reported(hass):
    """`CONNECT_RETRY_ATTEMPTS` applies to the whole connect-and-operate cycle,
    on top of bleak-retry-connector's own internal retries."""
    coordinator = _coordinator(hass)

    with patch.object(
        EB300Coordinator, "_run_once", side_effect=EB300ConnectionError("gone")
    ) as run_once, pytest.raises(HomeAssistantError):
        await coordinator.async_set_power(True)

    assert run_once.call_count == CONNECT_RETRY_ATTEMPTS


async def test_the_client_is_disconnected_even_when_the_operation_raises(hass):
    """Teardown sits in a `finally` outside the timeout scope. A leaked
    connection holds one of a proxy's ~3 slots until something times it out --
    an unreachable device starving a whole proxy is a failure mode this
    integration has already caused (docs/HARDWARE_NOTES.md)."""
    coordinator = _coordinator(hass)
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()

    async def _boom(_client):
        raise EB300ConnectionError("dropped mid-operation")

    with (
        patch("eb300_ble.coordinator.bluetooth.async_ble_device_from_address", return_value=MagicMock()),
        patch("eb300_ble.coordinator.BleakTransport"),
        patch("eb300_ble.coordinator.EB300Client", return_value=client),
        pytest.raises(EB300ConnectionError),
    ):
        await coordinator._run_once(_boom)

    client.disconnect.assert_awaited_once()
