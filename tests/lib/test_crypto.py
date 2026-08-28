"""C-7..C-9: HKDF derivation, HMAC transcript, AES-GCM wrap/unwrap."""

from __future__ import annotations

import pytest
from eb300_ble.const import CHANNEL_OPEN_API, OuterMessageType, outer_header
from eb300_ble.crypto import compute_hmac, derive_keys, unwrap, verify_hmac, wrap
from eb300_ble.exceptions import CryptoError

PSK = bytes(range(32))
CLIENT_NONCE = bytes(range(16))
SERVER_NONCE = bytes(range(16, 32))


# ── C-7: HKDF derivation ─────────────────────────────────────────────────


def test_c7_key_lengths():
    keys = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    assert len(keys.session_key) == 16
    assert len(keys.hmac_key) == 32


def test_c7_derivation_is_deterministic():
    a = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    b = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    assert a.session_key == b.session_key
    assert a.hmac_key == b.hmac_key


def test_c7_frozen_vector():
    # Golden vector: fixed PSK (bytes 0..31) + fixed nonces (0..15, 16..31) -> fixed
    # derived keys. Pinned so an accidental change to the HKDF salt/info construction
    # is caught immediately rather than silently producing different session keys.
    keys = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    assert keys.session_key.hex() == "5cbeaa61c26996203d4e6e1872949911"
    assert keys.hmac_key.hex() == "56d8e54cc4c996209664ecab7a984bfa945b15598604850054f1246dcc31bf37"


def test_c7_different_nonces_give_different_keys():
    a = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    b = derive_keys(PSK, CLIENT_NONCE, bytes(16))
    assert a.session_key != b.session_key
    assert a.hmac_key != b.hmac_key


# ── C-8: HMAC transcript ─────────────────────────────────────────────────


def test_c8_client_and_server_hmac_differ():
    keys = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    client_hmac = compute_hmac(keys.hmac_key, CLIENT_NONCE, SERVER_NONCE, b"_CLIENT")
    server_hmac = compute_hmac(keys.hmac_key, CLIENT_NONCE, SERVER_NONCE, b"_SERVER")
    assert client_hmac != server_hmac
    assert len(client_hmac) == 32 and len(server_hmac) == 32


def test_c8_verify_hmac_round_trip():
    keys = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    tag = compute_hmac(keys.hmac_key, CLIENT_NONCE, SERVER_NONCE, b"_SERVER")
    assert verify_hmac(keys.hmac_key, CLIENT_NONCE, SERVER_NONCE, b"_SERVER", tag) is True


def test_c8_verify_hmac_rejects_tampered_tag():
    keys = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE)
    tag = bytearray(compute_hmac(keys.hmac_key, CLIENT_NONCE, SERVER_NONCE, b"_SERVER"))
    tag[0] ^= 0xFF
    assert verify_hmac(keys.hmac_key, CLIENT_NONCE, SERVER_NONCE, b"_SERVER", bytes(tag)) is False


# ── C-9: AES-GCM wrap/unwrap ──────────────────────────────────────────────


def test_c9_outer_header_is_0x44():
    assert outer_header(OuterMessageType.ENCRYPTED_DATA, CHANNEL_OPEN_API) == 0x44


def test_c9_wrap_structure_and_round_trip():
    session_key = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE).session_key
    inner = b"hello eb300"
    wrapped = wrap(session_key, inner)
    assert len(wrapped) == 12 + len(inner) + 16  # nonce + ciphertext + GCM tag
    assert unwrap(session_key, wrapped) == inner


def test_c9_flipped_byte_raises_crypto_error():
    session_key = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE).session_key
    wrapped = bytearray(wrap(session_key, b"hello eb300"))
    wrapped[-1] ^= 0xFF  # corrupt the GCM tag
    with pytest.raises(CryptoError):
        unwrap(session_key, bytes(wrapped))


def test_c9_wrong_key_raises_crypto_error():
    session_key = derive_keys(PSK, CLIENT_NONCE, SERVER_NONCE).session_key
    other_key = derive_keys(PSK, CLIENT_NONCE, bytes(16)).session_key
    wrapped = wrap(session_key, b"hello eb300")
    with pytest.raises(CryptoError):
        unwrap(other_key, wrapped)
