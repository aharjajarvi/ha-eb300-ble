"""eb300-ble: standalone protocol library for the Ebeco EB-Therm 300 Open API."""

from .client import BleakTransport, EB300Client, Transport
from .const import PID, ErrorCode, Language, Operation, Program, ScreensaverType
from .exceptions import (
    CryptoError,
    DeviceError,
    EB300ConnectionError,
    EB300Error,
    HandshakeError,
    ProtocolError,
    RequestTimeoutError,
    SessionExhaustedError,
    ValidationError,
)
from .models import DeviceInfo, HomeProgram, ThermostatStatus
from .protocol import HomeProgramEvent

__all__ = [
    "PID",
    "BleakTransport",
    "CryptoError",
    "DeviceError",
    "DeviceInfo",
    "EB300Client",
    "EB300ConnectionError",
    "EB300Error",
    "ErrorCode",
    "HandshakeError",
    "HomeProgram",
    "HomeProgramEvent",
    "Language",
    "Operation",
    "Program",
    "ProtocolError",
    "RequestTimeoutError",
    "ScreensaverType",
    "SessionExhaustedError",
    "ThermostatStatus",
    "Transport",
    "ValidationError",
]
