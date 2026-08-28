"""Pure crypto primitives: HKDF key derivation, HMAC transcript, AES-GCM wrap/unwrap.

No BLE, no asyncio. Bytes in, bytes out.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .const import GCM_NONCE_LEN, HKDF_SALT
from .exceptions import CryptoError


class SessionKeys:
    """The two keys derived from a single handshake's PSK + nonce pair."""

    __slots__ = ("hmac_key", "session_key")

    def __init__(self, session_key: bytes, hmac_key: bytes) -> None:
        self.session_key = session_key
        self.hmac_key = hmac_key


def derive_keys(psk: bytes, client_nonce: bytes, server_nonce: bytes) -> SessionKeys:
    """Derive the AES-128 session key and the HMAC-SHA256 key from the PSK.

    HKDF-SHA256, salted, with the nonce transcript + a purpose suffix as info.
    """
    session_key = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=HKDF_SALT,
        info=client_nonce + server_nonce + b"AES",
    ).derive(psk)
    hmac_key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=HKDF_SALT,
        info=client_nonce + server_nonce + b"HMAC",
    ).derive(psk)
    return SessionKeys(session_key, hmac_key)


def compute_hmac(hmac_key: bytes, client_nonce: bytes, server_nonce: bytes, suffix: bytes) -> bytes:
    """HMAC-SHA256 over the nonce transcript with a purpose suffix (b"_CLIENT" / b"_SERVER")."""
    transcript = client_nonce + server_nonce + suffix
    return hmac.new(hmac_key, transcript, hashlib.sha256).digest()


def verify_hmac(hmac_key: bytes, client_nonce: bytes, server_nonce: bytes, suffix: bytes, tag: bytes) -> bool:
    expected = compute_hmac(hmac_key, client_nonce, server_nonce, suffix)
    return hmac.compare_digest(expected, tag)


def wrap(session_key: bytes, inner_payload: bytes, *, nonce: bytes | None = None) -> bytes:
    """AES-128-GCM encrypt. Returns nonce(12) || ciphertext || tag(16). No outer header."""
    nonce = nonce if nonce is not None else os.urandom(GCM_NONCE_LEN)
    if len(nonce) != GCM_NONCE_LEN:
        raise ValueError(f"GCM nonce must be {GCM_NONCE_LEN} bytes, got {len(nonce)}")
    ciphertext_tag = AESGCM(session_key).encrypt(nonce, inner_payload, None)
    return nonce + ciphertext_tag


def unwrap(session_key: bytes, wrapped: bytes) -> bytes:
    """Reverse of wrap(): split nonce/ciphertext+tag and AES-GCM decrypt.

    Raises CryptoError (never the raw cryptography exception) on auth failure,
    so callers (e.g. the advert decoder) can catch one type and drop silently.
    """
    if len(wrapped) < GCM_NONCE_LEN:
        raise CryptoError("Wrapped payload shorter than the GCM nonce")
    nonce, ciphertext_tag = wrapped[:GCM_NONCE_LEN], wrapped[GCM_NONCE_LEN:]
    try:
        return AESGCM(session_key).decrypt(nonce, ciphertext_tag, None)
    except Exception as exc:  # cryptography raises InvalidTag / ValueError
        raise CryptoError("AES-GCM authentication failed") from exc
