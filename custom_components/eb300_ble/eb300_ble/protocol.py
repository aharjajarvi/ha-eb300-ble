"""Pure protocol codec: outer framing, inner messages, counters, status/program structs.

No BLE, no asyncio. Bytes in, bytes out — this is what makes the wire format
testable with plain unit tests.
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TypedDict

from .const import (
    CHANNEL_OPEN_API,
    COUNTER_MAX,
    COUNTER_REHANDSHAKE_THRESHOLD,
    INNER_HEADER_LEN,
    INNER_MSG_MIN_LEN,
    MAX_INNER_PAYLOAD,
    THERMOSTAT_STATUS_LEN,
    OuterMessageType,
    op_status,
    outer_header,
    split_op_status,
    split_outer_header,
)
from .exceptions import ProtocolError, SessionExhaustedError, ValidationError

# ── Counters ─────────────────────────────────────────────────────────────


class Counter:
    """Per-session, strictly-increasing u16 request counter.

    Never a module global — the spec's own sample code gets this wrong and
    breaks with two concurrent devices/sessions. One instance per session.
    """

    __slots__ = ("_value",)

    def __init__(self, start: int = 1) -> None:
        self._value = start

    def peek(self) -> int:
        return self._value

    def next(self) -> int:
        """Return the next counter value, or raise if too close to u16 wraparound.

        "Strictly increasing" and "wraps at 65535" contradict each other, so we
        never actually wrap: once within COUNTER_REHANDSHAKE_THRESHOLD of the
        limit, force the caller to reconnect and start a fresh session instead.
        """
        if self._value >= COUNTER_REHANDSHAKE_THRESHOLD:
            raise SessionExhaustedError(
                f"Counter at {self._value}, within {COUNTER_MAX - COUNTER_REHANDSHAKE_THRESHOLD} "
                f"of u16 wraparound — reconnect and start a fresh session"
            )
        value = self._value
        self._value += 1
        return value


# ── Outer message framing ───────────────────────────────────────────────────


def build_outer(message_type: OuterMessageType, payload: bytes = b"", channel: int = CHANNEL_OPEN_API) -> bytes:
    return bytes([outer_header(message_type, channel)]) + payload


def parse_outer(data: bytes) -> tuple[int, int, bytes]:
    """Return (channel, message_type_nibble, payload)."""
    if not data:
        raise ProtocolError("Empty outer message")
    channel, msg_type = split_outer_header(data[0])
    return channel, msg_type, data[1:]


# ── Inner messages ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class InnerMessage:
    error: int
    operation: int
    counter: int
    pid: int
    data: bytes


def build_inner_message(operation: int, pid: int, counter: int, data: bytes = b"", *, error: int = 0) -> bytes:
    if not (0 <= counter <= COUNTER_MAX):
        raise ProtocolError(f"Counter {counter} out of u16 range 0..{COUNTER_MAX}")
    msg_len = INNER_HEADER_LEN + len(data)
    if msg_len > MAX_INNER_PAYLOAD:
        raise ProtocolError(
            f"Inner message length {msg_len} exceeds max inner payload {MAX_INNER_PAYLOAD} bytes"
        )
    header = struct.pack("<BBHH", msg_len, op_status(error, operation), counter, pid)
    return header + data


def parse_inner_messages(payload: bytes) -> list[InnerMessage]:
    """Parse every inner message batched into a decrypted frame."""
    messages: list[InnerMessage] = []
    offset = 0
    while offset < len(payload):
        if len(payload) - offset < INNER_HEADER_LEN:
            raise ProtocolError("Truncated inner message header")
        msg_len, op_status_byte, counter, pid = struct.unpack_from("<BBHH", payload, offset)
        if msg_len < INNER_MSG_MIN_LEN:
            raise ProtocolError(f"Inner MsgLen {msg_len} below minimum {INNER_MSG_MIN_LEN}")
        if offset + msg_len > len(payload):
            raise ProtocolError("Inner message length exceeds remaining payload")
        error, operation = split_op_status(op_status_byte)
        data = payload[offset + INNER_HEADER_LEN : offset + msg_len]
        messages.append(InnerMessage(error=error, operation=operation, counter=counter, pid=pid, data=bytes(data)))
        offset += msg_len
    return messages


# ── Thermostat status (0x1004 / 0x1003) ─────────────────────────────────────

_STATUS_FORMAT = "<HhhHHBBBBBBBBhhhBBBBIIH"


class ThermostatStatusFields(TypedDict):
    error_flags: int
    current_set_temperature: int
    limiting_temperature: int
    time_to_target: int
    relay_on: bool
    in_error_state: bool
    limited_by_limiting_sensor: bool
    power_off: bool
    room_temperature: int
    floor_temperature: int
    relay_temperature: int
    room_sensor_error: int
    floor_sensor_error: int
    current_program: int
    energy_meter: int


def unpack_thermostat_status(data: bytes) -> ThermostatStatusFields:
    """Unpack the 38-byte Thermostat Status struct into a field dict.

    Extra trailing bytes are ignored (forward-compat with future firmware);
    anything shorter than the documented length raises.
    """
    if len(data) < THERMOSTAT_STATUS_LEN:
        raise ProtocolError(
            f"Thermostat status payload too short: {len(data)} < {THERMOSTAT_STATUS_LEN} bytes"
        )
    (
        error_flags,
        current_set_temperature,
        limiting_temperature,
        _reserved6,
        time_to_target,
        _reserved10,
        _reserved11,
        relay_on,
        in_error_state,
        limited_by_limiting_sensor,
        _reserved15,
        power_off,
        _reserved17,
        room_temperature,
        floor_temperature,
        relay_temperature,
        room_sensor_error,
        floor_sensor_error,
        _reserved26,
        current_program,
        energy_meter,
        _reserved32,
        _reserved36,
    ) = struct.unpack_from(_STATUS_FORMAT, data, 0)

    return {
        "error_flags": error_flags,
        "current_set_temperature": current_set_temperature,
        "limiting_temperature": limiting_temperature,
        "time_to_target": time_to_target,
        "relay_on": bool(relay_on),
        "in_error_state": bool(in_error_state),
        "limited_by_limiting_sensor": bool(limited_by_limiting_sensor),
        "power_off": bool(power_off),
        "room_temperature": room_temperature,
        "floor_temperature": floor_temperature,
        "relay_temperature": relay_temperature,
        "room_sensor_error": room_sensor_error,
        "floor_sensor_error": floor_sensor_error,
        "current_program": current_program,
        "energy_meter": energy_meter,
    }


def pack_thermostat_status(fields: ThermostatStatusFields) -> bytes:
    """Pack a field dict back into the 38-byte wire struct (test fixtures / FakeEB300)."""
    return struct.pack(
        _STATUS_FORMAT,
        fields["error_flags"],
        fields["current_set_temperature"],
        fields["limiting_temperature"],
        0,
        fields["time_to_target"],
        0,
        0,
        int(fields["relay_on"]),
        int(fields["in_error_state"]),
        int(fields["limited_by_limiting_sensor"]),
        0,
        int(fields["power_off"]),
        0,
        fields["room_temperature"],
        fields["floor_temperature"],
        fields["relay_temperature"],
        fields["room_sensor_error"],
        fields["floor_sensor_error"],
        0,
        fields["current_program"],
        fields["energy_meter"],
        0,
        0,
    )


# ── Home program (0x10C0) ───────────────────────────────────────────────────

HOME_PROGRAM_DAYS = 7
HOME_PROGRAM_EVENTS_PER_DAY = 4
HOME_PROGRAM_EVENT_LEN = 4
HOME_PROGRAM_LEN = HOME_PROGRAM_DAYS * HOME_PROGRAM_EVENTS_PER_DAY * HOME_PROGRAM_EVENT_LEN  # 112

_EVENT_TEMP_S8_MIN = 10
_EVENT_TEMP_S8_MAX = 70
EVENT_TEMP_DECIDEG_MIN = _EVENT_TEMP_S8_MIN * 5  # 50 (5.0 C)
EVENT_TEMP_DECIDEG_MAX = _EVENT_TEMP_S8_MAX * 5  # 350 (35.0 C)


@dataclass(frozen=True, slots=True)
class HomeProgramEvent:
    active: bool
    hour: int
    minute: int
    temperature_decideg: int  # only meaningful when active=True


def decideg_to_s8(decideg: int) -> int:
    return decideg // 5


def s8_to_decideg(s8_value: int) -> int:
    return s8_value * 5


def validate_home_program_event_temperature(decideg: int) -> None:
    """Raise ValidationError unless `decideg` is a representable event temperature.

    The device's resolution is 0.5 C (one s8 unit = 5 decidegrees), so a value
    that isn't an exact multiple of 5 would otherwise be silently truncated by
    `decideg_to_s8` — reject it instead of storing something other than
    what the caller asked for. Usable standalone (e.g. to validate a single
    user-supplied event before merging it into a full program), not just from
    `_validate_home_program`.
    """
    if not (EVENT_TEMP_DECIDEG_MIN <= decideg <= EVENT_TEMP_DECIDEG_MAX):
        raise ValidationError(
            f"Temperature {decideg / 10:.1f} C out of range "
            f"{EVENT_TEMP_DECIDEG_MIN / 10:.1f}..{EVENT_TEMP_DECIDEG_MAX / 10:.1f} C"
        )
    if decideg % 5 != 0:
        low = (decideg // 5) * 5
        raise ValidationError(
            f"Temperature {decideg / 10:.1f} C is not a multiple of 0.5 C "
            f"(nearest valid values: {low / 10:.1f} C, {(low + 5) / 10:.1f} C)"
        )


def pack_home_program_event(event: HomeProgramEvent) -> bytes:
    return struct.pack(
        "<BBBb", 1 if event.active else 0, event.hour, event.minute, decideg_to_s8(event.temperature_decideg)
    )


def unpack_home_program_event(data: bytes) -> HomeProgramEvent:
    active, hour, minute, s8_temp = struct.unpack("<BBBb", data)
    return HomeProgramEvent(active=bool(active), hour=hour, minute=minute, temperature_decideg=s8_to_decideg(s8_temp))


def _validate_home_program(days: Sequence[Sequence[HomeProgramEvent]]) -> None:
    if len(days) != HOME_PROGRAM_DAYS:
        raise ValidationError(f"Home program must have {HOME_PROGRAM_DAYS} days, got {len(days)}")
    for day_idx, day in enumerate(days):
        if len(day) != HOME_PROGRAM_EVENTS_PER_DAY:
            raise ValidationError(
                f"Day {day_idx} must have exactly {HOME_PROGRAM_EVENTS_PER_DAY} events, got {len(day)}"
            )
        for event_idx, event in enumerate(day):
            if not (0 <= event.hour <= 23):
                raise ValidationError(f"Day {day_idx} event {event_idx}: hour {event.hour} out of range 0..23")
            if not (0 <= event.minute <= 59):
                raise ValidationError(f"Day {day_idx} event {event_idx}: minute {event.minute} out of range 0..59")
            # Validated unconditionally, not just for active events — an inactive
            # event's temperature still reaches struct.pack (it's preserved through
            # round-trips, since the device stores real values there), so an out-of-range
            # value must be rejected here rather than escape as a bare struct.error.
            try:
                validate_home_program_event_temperature(event.temperature_decideg)
            except ValidationError as exc:
                raise ValidationError(f"Day {day_idx} event {event_idx}: {exc}") from exc

        # Chronological order within the day, including inactive events. The device day
        # runs from 02:00 (daybreak) through 01:50 the following clock-day, so compare on
        # a virtual timeline anchored at 02:00 rather than raw hour:minute.
        #
        # Raw-clock "first >= 02:00" / "last <= 01:50" checks used to sit here too,
        # but on the virtual timeline both are tautologies (>= 0 and <= 1430) — expressed
        # in raw clock time they are actually mutually unsatisfiable for any day spanning
        # a morning and an evening event, which is every realistic schedule, including the
        # one the real device ships with. This ordering check is the only real constraint.
        virtual_minutes = [((event.hour - 2) % 24) * 60 + event.minute for event in day]
        for i in range(len(day) - 1):
            if virtual_minutes[i] > virtual_minutes[i + 1]:
                raise ValidationError(
                    f"Day {day_idx}: events must be in chronological order (event {i} follows event {i + 1})"
                )


def pack_home_program(days: Sequence[Sequence[HomeProgramEvent]]) -> bytes:
    _validate_home_program(days)
    out = bytearray()
    for day in days:
        for event in day:
            out += pack_home_program_event(event)
    return bytes(out)


def unpack_home_program(data: bytes) -> list[list[HomeProgramEvent]]:
    if len(data) != HOME_PROGRAM_LEN:
        raise ProtocolError(f"Home program payload must be {HOME_PROGRAM_LEN} bytes, got {len(data)}")
    days: list[list[HomeProgramEvent]] = []
    for day_idx in range(HOME_PROGRAM_DAYS):
        events = []
        for event_idx in range(HOME_PROGRAM_EVENTS_PER_DAY):
            offset = (day_idx * HOME_PROGRAM_EVENTS_PER_DAY + event_idx) * HOME_PROGRAM_EVENT_LEN
            events.append(unpack_home_program_event(data[offset : offset + HOME_PROGRAM_EVENT_LEN]))
        days.append(events)
    return days
