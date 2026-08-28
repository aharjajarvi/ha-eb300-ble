"""User-facing dataclasses: ThermostatStatus, HomeProgram, DeviceInfo.

Thin wrappers over the raw codec in protocol.py — this is where wire-format
integers become typed, documented Python values (Celsius floats, enums,
decoded flag lists).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import protocol
from .const import ERROR_FLAG_NAMES, Program, SensorErrorCode


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    model: str
    batch: str
    serial: str
    firmware_version: str


@dataclass(frozen=True, slots=True)
class ThermostatStatus:
    error_flags: int
    current_set_temperature: int  # decidegrees
    limiting_temperature: int  # decidegrees
    time_to_target: int  # minutes
    relay_on: bool
    in_error_state: bool
    limited_by_limiting_sensor: bool
    power_off: bool
    room_temperature: int  # decidegrees
    floor_temperature: int  # decidegrees
    relay_temperature: int  # decidegrees
    room_sensor_error: int
    floor_sensor_error: int
    current_program: int
    energy_meter: int  # cumulative relay-on minutes

    @classmethod
    def from_bytes(cls, data: bytes) -> ThermostatStatus:
        return cls(**protocol.unpack_thermostat_status(data))

    def to_bytes(self) -> bytes:
        return protocol.pack_thermostat_status(
            {
                "error_flags": self.error_flags,
                "current_set_temperature": self.current_set_temperature,
                "limiting_temperature": self.limiting_temperature,
                "time_to_target": self.time_to_target,
                "relay_on": self.relay_on,
                "in_error_state": self.in_error_state,
                "limited_by_limiting_sensor": self.limited_by_limiting_sensor,
                "power_off": self.power_off,
                "room_temperature": self.room_temperature,
                "floor_temperature": self.floor_temperature,
                "relay_temperature": self.relay_temperature,
                "room_sensor_error": self.room_sensor_error,
                "floor_sensor_error": self.floor_sensor_error,
                "current_program": self.current_program,
                "energy_meter": self.energy_meter,
            }
        )

    @property
    def active_error_flags(self) -> list[str]:
        """Names of every set bit in ErrorFlags, per the Open API §5.7 bitmask table."""
        return [name for mask, name in ERROR_FLAG_NAMES.items() if self.error_flags & mask]

    @property
    def current_set_temperature_c(self) -> float:
        return self.current_set_temperature / 10.0

    @property
    def limiting_temperature_c(self) -> float:
        return self.limiting_temperature / 10.0

    @property
    def room_temperature_c(self) -> float:
        return self.room_temperature / 10.0

    @property
    def floor_temperature_c(self) -> float:
        return self.floor_temperature / 10.0

    @property
    def relay_temperature_c(self) -> float:
        return self.relay_temperature / 10.0

    @property
    def program(self) -> Program:
        return Program(self.current_program)

    @property
    def room_sensor_fault(self) -> SensorErrorCode:
        return SensorErrorCode(self.room_sensor_error)

    @property
    def floor_sensor_fault(self) -> SensorErrorCode:
        return SensorErrorCode(self.floor_sensor_error)


@dataclass(frozen=True, slots=True)
class HomeProgram:
    days: list[list[protocol.HomeProgramEvent]]  # 7 days x 4 events

    @classmethod
    def from_bytes(cls, data: bytes) -> HomeProgram:
        return cls(days=protocol.unpack_home_program(data))

    def to_bytes(self) -> bytes:
        return protocol.pack_home_program(self.days)
