"""Protocol constants: PIDs, UUIDs, enums, error codes.

Source: Ebeco EB300 Open Local API, Rev 1.0 (2026-03-27), published by Ebeco AB:
https://www.ebeco.com/en/solutions/underfloor-heating/ebeco-open-api
"""

from __future__ import annotations

from enum import IntEnum

# ── BLE identity ──────────────────────────────────────────────────────────

DEVICE_LOCAL_NAME = "EB300"
MANUFACTURER_ID = 0x0F93  # Ebeco AB

SERVICE_DATA_ACCESS = "ebec0ebe-2000-478c-b320-d88b98a9ba80"
CHAR_DATA_STREAM_RX = "ebec0ebe-2001-478c-b320-d88b98a9ba80"  # client -> device (write)
CHAR_DATA_STREAM_TX = "ebec0ebe-2002-478c-b320-d88b98a9ba80"  # device -> client (notify)

MAX_BLE_PAYLOAD = 182
OUTER_OVERHEAD = 1 + 12 + 16  # header + nonce + GCM tag
MAX_INNER_PAYLOAD = MAX_BLE_PAYLOAD - OUTER_OVERHEAD  # 153

# ── Handshake / crypto ────────────────────────────────────────────────────

CHANNEL_OPEN_API = 4
NONCE_LEN = 16
GCM_NONCE_LEN = 12
GCM_TAG_LEN = 16
HKDF_SALT = b"bg22-psk-salt-ebeco"
PSK_LEN = 32

COUNTER_MAX = 0xFFFF
COUNTER_REHANDSHAKE_THRESHOLD = 0xFFF0  # force reconnect when this close to wraparound


class OuterMessageType(IntEnum):
    """Low nibble of the outer header byte: (channel << 4) | message_type."""

    CLIENT_NONCE = 0x00
    SERVER_NONCE = 0x01
    CLIENT_HMAC = 0x02
    SERVER_HMAC = 0x03
    ENCRYPTED_DATA = 0x04
    UNENCRYPTED_DATA = 0x05
    CLOSE_CONNECTION = 0x06
    ERROR = 0x0F


def outer_header(message_type: OuterMessageType, channel: int = CHANNEL_OPEN_API) -> int:
    return (channel << 4) | int(message_type)


def split_outer_header(header: int) -> tuple[int, int]:
    """Return (channel, message_type_nibble)."""
    return (header >> 4) & 0x0F, header & 0x0F


# ── Inner message ─────────────────────────────────────────────────────────

INNER_HEADER_LEN = 6  # MsgLen(1) + OpStatus(1) + Counter(2) + PID(2)
INNER_MSG_MIN_LEN = INNER_HEADER_LEN


class Operation(IntEnum):
    """Lower 4 bits of OpStatus."""

    GET = 0x00
    SET = 0x01
    GET_RESPONSE = 0x02
    SET_RESPONSE = 0x03
    DATA = 0x04


class ErrorCode(IntEnum):
    OK = 0
    NOK = 1
    VALIDATION_FAILED = 2
    INVALID_LENGTH = 3
    UNKNOWN_PID = 4
    UNAUTHORIZED = 5
    NOT_FOUND = 6
    INVALID_PARAMETER = 7
    BUFFER_OVERFLOW = 8
    NOT_IMPLEMENTED = 9
    CRYPTO_ERROR = 10
    VERIFICATION_FAILED = 11


def op_status(error_code: int, operation: int) -> int:
    return ((error_code & 0x0F) << 4) | (operation & 0x0F)


def split_op_status(op_status_byte: int) -> tuple[int, int]:
    """Return (error_code, operation)."""
    return (op_status_byte >> 4) & 0x0F, op_status_byte & 0x0F


# ── PIDs ───────────────────────────────────────────────────────────────────


class PID(IntEnum):
    # 5.1 Device Information (Read Only)
    MODEL_NAME = 0x0001
    BATCH_NAME = 0x0002
    SERIAL_NUMBER = 0x0003
    FIRMWARE_VERSION = 0x0005
    PING = 0x000A

    # 5.2 Time Synchronization
    NTP_TIME = 0x0230

    # 5.8 Device Popups
    SHOW_POPUP = 0x0A03

    # 5.9 Status Advertisement Setup
    STATUS_SETUP_ADVERTS = 0x0C01

    # 5.7 Status & Monitoring
    THERMOSTAT_STATUS = 0x1004
    THERMOSTAT_STATUS_PERIODIC = 0x1003  # advert-only variant, same payload shape
    ENERGY_METER = 0x1020

    # 5.3 Thermostat Control
    POWER_ON = 0x1081
    MANUAL_CONTROL_TEMP = 0x1082
    SELECTED_PROGRAM = 0x1083

    # 5.5 Display & UI Settings
    LANGUAGE = 0x1090
    SCREENSAVER_TYPE = 0x1091

    # 5.6 Sensor Calibration
    KEY_LOCK = 0x10A2
    CALIBRATION_USER = 0x10B2

    # 5.4 Program Scheduling
    HOME_PROGRAM = 0x10C0

    # 5.3 Thermostat Control
    OVERRIDE_TEMPERATURE = 0x10D0


class Program(IntEnum):
    MANUAL = 0
    HOME = 1


class Language(IntEnum):
    SWEDISH = 0
    ENGLISH = 1
    NORWEGIAN = 2
    FINNISH = 3
    GERMAN = 4
    DANISH = 5


class ScreensaverType(IntEnum):
    OFF = 0
    TIME_TEMP = 1
    TIME_DATE = 2
    TEMPERATURE = 3


class KeyLock(IntEnum):
    UNLOCKED = 0
    LOCKED = 1


# ── Temperature limits (decidegrees) ────────────────────────────────────────

TEMP_MIN_DECIDEG = 50  # 5.0 C
TEMP_MAX_DECIDEG = 350  # 35.0 C
CALIBRATION_MIN_DECIDEG = -50
CALIBRATION_MAX_DECIDEG = 50

# ── ErrorFlags bitmask (ThermostatStatus offset 0) ──────────────────────────

ERROR_FLAG_AT_MIN_LIMIT = 0x0002
ERROR_FLAG_AT_MAX_LIMIT = 0x0004
ERROR_FLAG_ROOM_SENSOR_OPEN = 0x0010
ERROR_FLAG_ROOM_SENSOR_SHORT = 0x0020
ERROR_FLAG_FLOOR_SENSOR_OPEN = 0x0040
ERROR_FLAG_FLOOR_SENSOR_SHORT = 0x0080
ERROR_FLAG_RELAY_SENSOR_OPEN = 0x0100
ERROR_FLAG_RELAY_SENSOR_SHORT = 0x0200
ERROR_FLAG_RELAY_OVERTEMP = 0x0400
ERROR_FLAG_STARTUP_MODE = 0x1000
ERROR_FLAG_POWER_CONTROL_ACTIVE = 0x2000
ERROR_FLAG_POWER_OFF = 0x4000

ERROR_FLAG_NAMES: dict[int, str] = {
    ERROR_FLAG_AT_MIN_LIMIT: "at_minimum_temperature_limit",
    ERROR_FLAG_AT_MAX_LIMIT: "at_maximum_temperature_limit",
    ERROR_FLAG_ROOM_SENSOR_OPEN: "room_sensor_open_circuit",
    ERROR_FLAG_ROOM_SENSOR_SHORT: "room_sensor_short_circuit",
    ERROR_FLAG_FLOOR_SENSOR_OPEN: "floor_sensor_open_circuit",
    ERROR_FLAG_FLOOR_SENSOR_SHORT: "floor_sensor_short_circuit",
    ERROR_FLAG_RELAY_SENSOR_OPEN: "relay_sensor_open_circuit",
    ERROR_FLAG_RELAY_SENSOR_SHORT: "relay_sensor_short_circuit",
    ERROR_FLAG_RELAY_OVERTEMP: "relay_over_temperature",
    ERROR_FLAG_STARTUP_MODE: "startup_mode_active",
    ERROR_FLAG_POWER_CONTROL_ACTIVE: "power_control_active",
    ERROR_FLAG_POWER_OFF: "power_off",
}


class SensorErrorCode(IntEnum):
    OK = 0
    OPEN_CIRCUIT = 1
    SHORT_CIRCUIT = 2
    LOOKUP_TABLE_ERROR = 3
    NOT_CONFIGURED = 4


THERMOSTAT_STATUS_LEN = 38

# ── Advertisement layout ────────────────────────────────────────────────────

ADVERT_PACKAGE_TYPE_NORMAL = 0x01
ADVERT_PACKAGE_TYPE_BURST = 0x03

# 16-byte manufacturer-data payload (bleak-style, AD headers/company-id stripped)
ADVERT_NORMAL_LEN = 16
ADVERT_NORMAL_MAC_OFFSET = 1
ADVERT_NORMAL_MAC_LEN = 6
ADVERT_NORMAL_ENC_FLAGS_OFFSET = 7
ADVERT_NORMAL_EVENT_FLAGS_OFFSET = 8
ADVERT_NORMAL_REGULATOR_ERROR_OFFSET = 10

ENC_FLAG_CHANNEL1_PSK = 0x01
ENC_FLAG_CHANNEL2_PSK = 0x08
ENC_FLAG_CHANNEL3_PSK = 0x10
ENC_FLAG_OPEN_API_PSK_PROVISIONED = 0x20
ENC_FLAG_ADVERT_CHANNEL_IS_OPEN_API = 0x40
ENC_FLAG_STATUS_ADVERT_ACTIVE = 0x80

# Burst chunk fragment (bleak-style, 10-byte header stripped already by caller
# when the raw 21-byte manufacturer payload is sliced) — offsets below are
# relative to the manufacturer-specific-data payload as bleak returns it,
# i.e. starting at Package Type.
ADVERT_BURST_PACKAGE_TYPE_OFFSET = 0
ADVERT_BURST_COUNTER_OFFSET = 1
ADVERT_BURST_CHUNK_INFO_OFFSET = 2
ADVERT_BURST_FRAGMENT_OFFSET = 3
ADVERT_BURST_FRAGMENT_MAX_LEN = 21

STATUS_ADVERT_INNER_LEN = INNER_HEADER_LEN + THERMOSTAT_STATUS_LEN  # 44
