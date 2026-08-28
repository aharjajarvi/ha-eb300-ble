"""BLE advertisement parsing: normal 16-byte status advert + burst-chunk reassembly.

No BLE library dependency here — this operates on the manufacturer-specific-data
payload as bleak (or any AD-parsing scanner) already hands it over, i.e. with the
AD length/type/company-id header already stripped.
"""

from __future__ import annotations

import logging
import struct
from dataclasses import dataclass

from . import protocol
from .const import (
    ADVERT_NORMAL_LEN,
    ADVERT_PACKAGE_TYPE_BURST,
    ADVERT_PACKAGE_TYPE_NORMAL,
    ENC_FLAG_ADVERT_CHANNEL_IS_OPEN_API,
    ENC_FLAG_OPEN_API_PSK_PROVISIONED,
    ENC_FLAG_STATUS_ADVERT_ACTIVE,
)
from .crypto import unwrap
from .exceptions import CryptoError, ProtocolError
from .protocol import InnerMessage

_LOGGER = logging.getLogger(__name__)


# ── Normal advertisement (Package Type 0x01) ────────────────────────────────


@dataclass(frozen=True, slots=True)
class NormalAdvertisement:
    package_type: int
    mac: bytes
    encryption_flags: int
    event_flags: int
    regulator_error_flags: int

    @property
    def open_api_psk_provisioned(self) -> bool:
        return bool(self.encryption_flags & ENC_FLAG_OPEN_API_PSK_PROVISIONED)

    @property
    def advert_channel_is_open_api(self) -> bool:
        return bool(self.encryption_flags & ENC_FLAG_ADVERT_CHANNEL_IS_OPEN_API)

    @property
    def status_advert_active(self) -> bool:
        return bool(self.encryption_flags & ENC_FLAG_STATUS_ADVERT_ACTIVE)


def parse_normal_advertisement(data: bytes) -> NormalAdvertisement:
    """Parse the 16-byte manufacturer-data payload of a normal (Package Type 0x01) advert."""
    if len(data) < ADVERT_NORMAL_LEN:
        raise ProtocolError(f"Normal advertisement payload too short: {len(data)} < {ADVERT_NORMAL_LEN} bytes")
    package_type = data[0]
    mac = bytes(data[1:7])
    encryption_flags = data[7]
    (event_flags,) = struct.unpack_from("<H", data, 8)
    (regulator_error_flags,) = struct.unpack_from("<H", data, 10)
    return NormalAdvertisement(
        package_type=package_type,
        mac=mac,
        encryption_flags=encryption_flags,
        event_flags=event_flags,
        regulator_error_flags=regulator_error_flags,
    )


def is_burst_chunk(data: bytes) -> bool:
    return bool(data) and data[0] == ADVERT_PACKAGE_TYPE_BURST


def is_normal_advertisement(data: bytes) -> bool:
    return bool(data) and data[0] == ADVERT_PACKAGE_TYPE_NORMAL


# ── Burst chunk reassembly (Package Type 0x03) ──────────────────────────────


@dataclass(frozen=True, slots=True)
class BurstChunk:
    counter: int
    chunk_index: int
    total_chunks: int
    fragment: bytes


def parse_burst_chunk(data: bytes) -> BurstChunk:
    if len(data) < 3:
        raise ProtocolError(f"Burst chunk payload too short: {len(data)} bytes")
    package_type = data[0]
    if package_type != ADVERT_PACKAGE_TYPE_BURST:
        raise ProtocolError(f"Not a burst chunk: package type 0x{package_type:02X}")
    counter = data[1]
    chunk_info = data[2]
    total_chunks = (chunk_info >> 4) & 0x0F
    chunk_index = chunk_info & 0x0F
    fragment = bytes(data[3:])
    return BurstChunk(counter=counter, chunk_index=chunk_index, total_chunks=total_chunks, fragment=fragment)


class BurstReassembler:
    """Accumulates burst chunks by counter and emits full ciphertext once complete.

    Repeated chunks (BurstCount > 1) and out-of-order arrival are both fine —
    chunks are keyed by index within their counter's bucket. A missing chunk
    simply means the counter's bucket never completes; it is never emitted and
    never raises. Multiple counters in flight are tracked independently, so
    interleaved status messages don't corrupt each other.
    """

    def __init__(self) -> None:
        self._pending: dict[int, dict[int, bytes]] = {}
        self._expected_total: dict[int, int] = {}

    def add_chunk(self, chunk: BurstChunk) -> bytes | None:
        chunks = self._pending.setdefault(chunk.counter, {})
        chunks[chunk.chunk_index] = chunk.fragment
        self._expected_total[chunk.counter] = chunk.total_chunks

        if len(chunks) < chunk.total_chunks or not all(i in chunks for i in range(chunk.total_chunks)):
            return None

        ordered = b"".join(chunks[i] for i in range(chunk.total_chunks))
        del self._pending[chunk.counter]
        del self._expected_total[chunk.counter]
        return ordered

    def discard(self, counter: int) -> None:
        self._pending.pop(counter, None)
        self._expected_total.pop(counter, None)

    def clear(self) -> None:
        self._pending.clear()
        self._expected_total.clear()


# ── Decryption ───────────────────────────────────────────────────────────


def decrypt_status_advert(session_key: bytes, ciphertext: bytes) -> InnerMessage | None:
    """Decrypt a reassembled status advert and parse its inner message.

    Adverts armed under a session key that has since been replaced by a new
    handshake will fail GCM authentication. That is expected, not exceptional:
    swallow it silently (one debug line, nothing escapes) rather than raising
    or logging an error storm.
    """
    try:
        inner_payload = unwrap(session_key, ciphertext)
    except CryptoError:
        _LOGGER.debug("Dropping status advert: GCM authentication failed (stale session key)")
        return None

    messages = protocol.parse_inner_messages(inner_payload)
    return messages[0] if messages else None
