"""C-12..C-14: normal advert parsing, burst reassembly, stale-key decode."""

from __future__ import annotations

import logging
import struct

import pytest
from eb300_ble.advertisement import (
    BurstChunk,
    BurstReassembler,
    decrypt_status_advert,
    is_burst_chunk,
    is_normal_advertisement,
    parse_burst_chunk,
    parse_normal_advertisement,
)
from eb300_ble.crypto import derive_keys, wrap
from eb300_ble.exceptions import ProtocolError
from eb300_ble.protocol import build_inner_message

PSK = bytes(range(32))
CLIENT_NONCE = bytes(range(16))
SERVER_NONCE = bytes(range(16, 32))


# ── C-12: normal advertisement parse ─────────────────────────────────────


def _normal_advert_bytes(*, encryption_flags: int) -> bytes:
    mac = bytes.fromhex("AABBCCDDEEFF")
    return (
        bytes([0x01])
        + mac
        + bytes([encryption_flags])
        + struct.pack("<H", 0x1234)  # event flags
        + struct.pack("<H", 0x5678)  # regulator error flags
        + bytes(4)  # reserved
    )


def test_c12_parses_mac_and_flags():
    data = _normal_advert_bytes(encryption_flags=0x20 | 0x80)  # bit5 + bit7
    advert = parse_normal_advertisement(data)
    assert advert.package_type == 0x01
    assert advert.mac == bytes.fromhex("AABBCCDDEEFF")
    assert advert.open_api_psk_provisioned is True
    assert advert.status_advert_active is True
    assert advert.advert_channel_is_open_api is False
    assert advert.event_flags == 0x1234
    assert advert.regulator_error_flags == 0x5678


def test_c12_flags_clear_when_bits_unset():
    data = _normal_advert_bytes(encryption_flags=0x00)
    advert = parse_normal_advertisement(data)
    assert advert.open_api_psk_provisioned is False
    assert advert.status_advert_active is False


def test_c12_too_short_payload_raises():
    with pytest.raises(ProtocolError):
        parse_normal_advertisement(bytes(15))


def test_package_type_discriminators():
    normal = _normal_advert_bytes(encryption_flags=0)
    burst = bytes([0x03, 0x01, 0x20]) + b"x"
    assert is_normal_advertisement(normal) is True
    assert is_burst_chunk(normal) is False
    assert is_burst_chunk(burst) is True
    assert is_normal_advertisement(burst) is False
    assert is_normal_advertisement(b"") is False
    assert is_burst_chunk(b"") is False


# ── C-13: burst reassembly ────────────────────────────────────────────────


def test_c13_out_of_order_chunks_reassemble():
    reassembler = BurstReassembler()
    chunks = [
        BurstChunk(counter=5, chunk_index=i, total_chunks=3, fragment=bytes([i]) * 4) for i in range(3)
    ]
    # Deliver out of order: 2, 0, 1
    assert reassembler.add_chunk(chunks[2]) is None
    assert reassembler.add_chunk(chunks[0]) is None
    result = reassembler.add_chunk(chunks[1])
    assert result == chunks[0].fragment + chunks[1].fragment + chunks[2].fragment


def test_c13_missing_chunk_never_emits():
    reassembler = BurstReassembler()
    assert reassembler.add_chunk(BurstChunk(counter=1, chunk_index=0, total_chunks=3, fragment=b"a")) is None
    assert reassembler.add_chunk(BurstChunk(counter=1, chunk_index=2, total_chunks=3, fragment=b"c")) is None
    # chunk_index 1 never arrives — nothing should have been emitted, and asking
    # again must not crash or spuriously complete.
    assert reassembler.add_chunk(BurstChunk(counter=1, chunk_index=2, total_chunks=3, fragment=b"c")) is None


def test_c13_interleaved_counters_kept_separate():
    reassembler = BurstReassembler()
    assert reassembler.add_chunk(BurstChunk(counter=1, chunk_index=0, total_chunks=2, fragment=b"A0")) is None
    assert reassembler.add_chunk(BurstChunk(counter=2, chunk_index=0, total_chunks=2, fragment=b"B0")) is None
    result1 = reassembler.add_chunk(BurstChunk(counter=1, chunk_index=1, total_chunks=2, fragment=b"A1"))
    assert result1 == b"A0A1"
    result2 = reassembler.add_chunk(BurstChunk(counter=2, chunk_index=1, total_chunks=2, fragment=b"B1"))
    assert result2 == b"B0B1"


def test_c13_parse_burst_chunk_layout():
    # Package Type 0x03, counter 7, chunk_info = total=3 (high nibble), index=1 (low nibble)
    data = bytes([0x03, 0x07, 0x31]) + b"fragment-bytes"
    chunk = parse_burst_chunk(data)
    assert chunk.counter == 7
    assert chunk.total_chunks == 3
    assert chunk.chunk_index == 1
    assert chunk.fragment == b"fragment-bytes"


def test_c13_parse_burst_chunk_too_short_raises():
    with pytest.raises(ProtocolError):
        parse_burst_chunk(bytes(2))


def test_c13_parse_burst_chunk_wrong_package_type_raises():
    with pytest.raises(ProtocolError):
        parse_burst_chunk(bytes([0x01, 0x00, 0x00]) + b"x")


def test_c13_discard_and_clear():
    reassembler = BurstReassembler()
    reassembler.add_chunk(BurstChunk(counter=1, chunk_index=0, total_chunks=2, fragment=b"a"))
    reassembler.add_chunk(BurstChunk(counter=2, chunk_index=0, total_chunks=2, fragment=b"b"))

    reassembler.discard(1)
    assert reassembler.add_chunk(BurstChunk(counter=1, chunk_index=1, total_chunks=2, fragment=b"a1")) is None

    reassembler.clear()
    assert reassembler.add_chunk(BurstChunk(counter=2, chunk_index=1, total_chunks=2, fragment=b"b1")) is None


# ── C-14: stale-key advert swallowed silently ────────────────────────────


def test_c14_stale_key_swallowed_with_one_debug_line(caplog):
    fresh_session_key = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE).session_key
    stale_session_key = derive_keys(PSK, CLIENT_NONCE, bytes(16)).session_key
    ciphertext = wrap(stale_session_key, b"some inner message payload")

    with caplog.at_level(logging.DEBUG, logger="eb300_ble.advertisement"):
        result = decrypt_status_advert(fresh_session_key, ciphertext)

    assert result is None
    debug_records = [r for r in caplog.records if r.name == "eb300_ble.advertisement"]
    assert len(debug_records) == 1
    assert debug_records[0].levelno == logging.DEBUG


def test_decrypt_status_advert_success():
    session_key = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE).session_key
    inner = build_inner_message(operation=0x04, pid=0x1004, counter=1, data=b"status-bytes")
    ciphertext = wrap(session_key, inner)

    msg = decrypt_status_advert(session_key, ciphertext)

    assert msg is not None
    assert msg.pid == 0x1004
    assert msg.data == b"status-bytes"
