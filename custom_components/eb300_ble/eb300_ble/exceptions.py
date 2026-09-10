"""Exception hierarchy for the eb300_ble library."""

from __future__ import annotations


class EB300Error(Exception):
    """Base class for all eb300_ble errors."""


class EB300ConnectionError(EB300Error):
    """BLE connection could not be established or was lost."""


class HandshakeError(EB300Error):
    """The 4-step handshake failed."""

    def __init__(self, message: str, *, step: int, error_code: int | None = None) -> None:
        super().__init__(message)
        self.step = step
        self.error_code = error_code

    @property
    def is_psk_rejection(self) -> bool:
        """True when the device actively refused the key, not merely failed to answer.

        Only two of the failure shapes mean "wrong PSK": the device replying
        ERROR to a handshake step (`error_code` set), and the step-4 server
        HMAC mismatch. A timeout (step 0) or a malformed/unexpected message
        type is a connectivity or protocol problem and retrying it can
        succeed, so callers must not treat those as an auth failure.
        """
        return self.error_code is not None or self.step == 4


class DeviceError(EB300Error):
    """The device returned a non-zero error code for a request."""

    def __init__(self, error_code: int, *, pid: int | None = None) -> None:
        from .const import ErrorCode

        try:
            name = ErrorCode(error_code).name
        except ValueError:
            name = f"UNKNOWN({error_code})"
        pid_part = f" for PID 0x{pid:04X}" if pid is not None else ""
        super().__init__(f"Device error {error_code} ({name}){pid_part}")
        self.error_code = error_code
        self.pid = pid


class RequestTimeoutError(EB300Error):
    """No response was received for a request within the timeout window."""

    def __init__(self, pid: int, counter: int, timeout: float) -> None:
        super().__init__(
            f"Timed out after {timeout}s waiting for response to PID 0x{pid:04X} "
            f"(counter {counter})"
        )
        self.pid = pid
        self.counter = counter


class SessionExhaustedError(EB300Error):
    """The per-session message counter is approaching its u16 wraparound limit."""


class ProtocolError(EB300Error):
    """Malformed bytes at the outer or inner message layer."""


class CryptoError(EB300Error):
    """AES-GCM authentication failed (wrong key or corrupted data)."""


class ValidationError(EB300Error):
    """Client-side validation of a value or structure failed before any BLE write."""
