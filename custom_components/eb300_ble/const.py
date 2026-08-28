"""Constants for the eb300_ble integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "eb300_ble"

CONF_PSK = "psk"  # base64-encoded 32-byte PSK, stored only in the config entry

DEFAULT_POLL_INTERVAL_SECONDS = 300
MIN_POLL_INTERVAL_SECONDS = 60
MAX_POLL_INTERVAL_SECONDS = 1800

DEFAULT_SCAN_TIMEOUT = 10.0
DEFAULT_CONNECT_TIMEOUT = 20.0

# R-13 (docs/HARDWARE_NOTES.md): measured ~6% single-attempt BLE connect
# timeout rate on real hardware. One retry drops the compound failure rate
# to ~0.36% — required for the coordinator to hit a reasonable reliability
# bar, not just a nice-to-have.
CONNECT_RETRY_ATTEMPTS = 2

# bleak_retry_connector.establish_connection()'s own internal retry budget
# for the "connect timed out" failure mode (default 4, ~20s each). Observed
# on real hardware (docs/HARDWARE_NOTES.md): stacked on top of
# CONNECT_RETRY_ATTEMPTS, a single unreachable-device poll or write could
# occupy a shared BLE proxy's connection slot for minutes, starving every
# other Bluetooth device sharing it (a habluetooth.auto_scheduler warning
# about unrelated devices unable to get a scanner slot). Halved here to
# bound that window while keeping CONNECT_RETRY_ATTEMPTS's own retry-once
# insurance intact. Does not affect the separate "out of connection slots"
# transient-error path, which bleak_retry_connector caps via its own
# internal constant (MAX_TRANSIENT_ERRORS=9) — not exposed as a parameter,
# so not tunable here without monkeypatching third-party module state
# shared by every other Bluetooth integration in the HA process.
BLE_CONNECT_MAX_ATTEMPTS = 2

# Hard ceiling on how long one connect+operation may hold a BLE proxy's
# connection slot. This is the only lever we actually control for the
# unreachable-device case: bleak_retry_connector's `max_attempts` (above)
# gates only its `timeouts + connect_errors` counters, while
# BleakDeviceNotFoundError/BleakNotFoundError — what an offline device
# produces — are classified as *transient* and gated instead by its module
# constant MAX_TRANSIENT_ERRORS = 9, at up to BLEAK_SAFETY_TIMEOUT = 60s each
# plus out-of-slots backoff (verified against bleak_retry_connector 4.6.3).
# That is the multiple-minute worst case behind the 190s and 319s operations
# observed starving unrelated devices on the same proxy (docs/HARDWARE_NOTES.md,
# 2026-08-20); BLE_CONNECT_MAX_ATTEMPTS cannot bound it.
#
# Sized well above normal operation — typical poll ~1s, with slow-but-
# successful outliers of 10.6s / 27s / 40.6s already on record — and well
# below the pathological cases. A timeout counts as one failed attempt, so
# CONNECT_RETRY_ATTEMPTS still applies on top: worst case is now roughly
# 2 x 45s rather than open-ended.
BLE_OPERATION_TIMEOUT = 45.0

# Connection slots on a BLE proxy are the scarce shared resource across
# every BLE integration in the house, not just this one — serialize this
# integration's own connections so multiple eb300_ble devices never contend
# with each other for the same slot.
_CONNECTION_SEMAPHORE_LIMIT = 1

MIN_TARGET_TEMP_C = 5.0
MAX_TARGET_TEMP_C = 35.0
TARGET_TEMP_STEP_C = 0.5

UPDATE_INTERVAL = timedelta(seconds=DEFAULT_POLL_INTERVAL_SECONDS)

CONF_USE_ROOM_SENSOR = "use_room_sensor"

# Dragging the HA slider fires a burst of set_temperature calls —
# coalesce them into one BLE write, sent this long after the last one.
CLIMATE_SET_TEMPERATURE_DEBOUNCE_SECONDS = 1.0

# Home program services. Index 0 = Monday, matching the
# device's own day ordering in the 0x10C0 struct.
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SERVICE_GET_HOME_PROGRAM = "get_home_program"
SERVICE_SET_HOME_PROGRAM = "set_home_program"
