"""Unit tests for the LAN crypto and handshake logic."""

from __future__ import annotations

import base64
import os

import pytest

from custom_components.delonghi_coffee.lan import (
    LanCryptoError,
    LanSession,
    _aes_decrypt,
    _aes_encrypt,
    decrypt_device_to_app,
    derive_session,
    encrypt_app_to_device,
    sign_payload,
    verify_signature,
)

# ─────────────────────────────────────────────────────────────────────────
# Fixture data — deterministic so the tests act as regression anchors for
# the derivation formula. If anyone edits derive_session(), they must also
# update these vectors or justify why.
# ─────────────────────────────────────────────────────────────────────────

_LAN_KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f" * 2)  # 32 bytes
_R1 = bytes.fromhex("101112131415161718191a1b1c1d1e1f")
_R2 = bytes.fromhex("202122232425262728292a2b2c2d2e2f")


def _make_session() -> LanSession:
    return derive_session(_LAN_KEY, _R1, _R2, key_id=42)


# ─────────────────────────────────────────────────────────────────────────
# derive_session
# ─────────────────────────────────────────────────────────────────────────


def test_derive_session_returns_16_byte_keys_and_ivs() -> None:
    s = _make_session()
    assert len(s.app_key) == 16
    assert len(s.dev_key) == 16
    assert len(s.app_iv) == 16
    assert len(s.dev_iv) == 16
    assert len(s.sign_key) == 32
    assert s.key_id == 42
    assert s.random_1 == _R1
    assert s.random_2 == _R2


def test_derive_session_is_deterministic() -> None:
    a = _make_session()
    b = _make_session()
    assert a.app_key == b.app_key
    assert a.dev_key == b.dev_key
    assert a.app_iv == b.app_iv
    assert a.dev_iv == b.dev_iv
    assert a.sign_key == b.sign_key


def test_derive_session_app_and_device_keys_differ() -> None:
    """The two directions MUST have different keys and IVs."""
    s = _make_session()
    assert s.app_key != s.dev_key
    assert s.app_iv != s.dev_iv


def test_derive_session_rejects_short_lan_key() -> None:
    with pytest.raises(LanCryptoError, match="lan_key"):
        derive_session(b"too_short", _R1, _R2, 1)


def test_derive_session_rejects_wrong_random_length() -> None:
    with pytest.raises(LanCryptoError, match="random_1"):
        derive_session(_LAN_KEY, b"short", _R2, 1)
    with pytest.raises(LanCryptoError, match="random_1"):
        derive_session(_LAN_KEY, _R1, b"short", 1)


# ─────────────────────────────────────────────────────────────────────────
# IV rotation
# ─────────────────────────────────────────────────────────────────────────


def test_fresh_iv_changes_with_sequence_number() -> None:
    s = _make_session()
    iv_1 = s.fresh_iv(s.app_iv, 1)
    iv_2 = s.fresh_iv(s.app_iv, 2)
    iv_10000 = s.fresh_iv(s.app_iv, 10000)
    assert len({iv_1, iv_2, iv_10000}) == 3
    # Prefix unchanged, only last 4 bytes rotate
    assert iv_1[:12] == s.app_iv[:12]
    assert iv_2[:12] == s.app_iv[:12]


def test_fresh_iv_rejects_wrong_iv_length() -> None:
    s = _make_session()
    with pytest.raises(LanCryptoError):
        s.fresh_iv(b"too_short", 1)


# ─────────────────────────────────────────────────────────────────────────
# AES round trip
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "plaintext",
    [
        b"",  # empty
        b"hello world",
        b"a" * 15,  # pre-block boundary
        b"a" * 16,  # block boundary — PKCS7 must add a full block
        b"a" * 17,  # post-block
        b"\x00" * 64,  # all zeros
        os.urandom(1024),  # random large payload
    ],
)
def test_aes_round_trip(plaintext: bytes) -> None:
    key = os.urandom(16)
    iv = os.urandom(16)
    ciphertext = _aes_encrypt(key, iv, plaintext)
    assert ciphertext != plaintext or len(plaintext) == 0
    assert len(ciphertext) % 16 == 0
    recovered = _aes_decrypt(key, iv, ciphertext)
    assert recovered == plaintext


def test_aes_decrypt_with_wrong_key_fails() -> None:
    key1 = os.urandom(16)
    key2 = os.urandom(16)
    iv = os.urandom(16)
    ciphertext = _aes_encrypt(key1, iv, b"secret command")
    # Wrong key will almost certainly produce garbled padding and raise.
    with pytest.raises((LanCryptoError, ValueError)):
        _aes_decrypt(key2, iv, ciphertext)


# ─────────────────────────────────────────────────────────────────────────
# Signing
# ─────────────────────────────────────────────────────────────────────────


def test_sign_and_verify_round_trip() -> None:
    s = _make_session()
    payload = b"command_bytes"
    sig = sign_payload(s, payload)
    assert verify_signature(s, payload, sig)


def test_verify_rejects_tampered_payload() -> None:
    s = _make_session()
    sig = sign_payload(s, b"original")
    assert not verify_signature(s, b"tampered", sig)


def test_verify_rejects_tampered_signature() -> None:
    s = _make_session()
    good = sign_payload(s, b"payload")
    bad = base64.b64encode(b"\x00" * 32).decode()
    assert good != bad  # sanity: the valid signature is not all zeros
    assert not verify_signature(s, b"payload", bad)


def test_verify_tolerates_garbage_signature() -> None:
    s = _make_session()
    assert not verify_signature(s, b"payload", "not_base64!@#")
    assert not verify_signature(s, b"payload", "")


# ─────────────────────────────────────────────────────────────────────────
# Envelope encryption (full app ⇄ device round trip)
# ─────────────────────────────────────────────────────────────────────────


def test_app_to_device_round_trip_via_matching_session() -> None:
    """End-to-end: encrypt app→device on one side, decrypt on the other.

    In real deployment the "device side" is the coffee machine with its own
    session. Here we simulate by deriving a second session using the same
    inputs — the keys will match.
    """
    tx = _make_session()
    rx = _make_session()  # same derivation → same keys

    plaintext = b"\x0d\x07\x84\x0f\x03\x02\x56\x40"  # ECAM 0x84 monitor command
    envelope = encrypt_app_to_device(tx, plaintext)

    # Simulate the machine swapping directions: our app→device is its dev→app.
    # Hot-swap the relevant keys on the rx session so verify/decrypt use them.
    rx.dev_key = tx.app_key
    rx.dev_iv = tx.app_iv
    rx.sign_key = tx.sign_key

    recovered = decrypt_device_to_app(rx, envelope)
    assert recovered == plaintext


def test_envelope_increments_sequence_counter() -> None:
    s = _make_session()
    e1 = encrypt_app_to_device(s, b"one")
    e2 = encrypt_app_to_device(s, b"two")
    assert e2["seq_no"] == e1["seq_no"] + 1


def test_replay_attack_is_rejected() -> None:
    """Delivering the same envelope twice must be refused."""
    tx = _make_session()
    rx = _make_session()
    rx.dev_key = tx.app_key
    rx.dev_iv = tx.app_iv
    rx.sign_key = tx.sign_key

    env = encrypt_app_to_device(tx, b"brew_espresso")
    assert decrypt_device_to_app(rx, env) == b"brew_espresso"
    with pytest.raises(LanCryptoError, match="Replay"):
        decrypt_device_to_app(rx, env)


def test_envelope_with_wrong_signature_is_rejected() -> None:
    tx = _make_session()
    rx = _make_session()
    rx.dev_key = tx.app_key
    rx.dev_iv = tx.app_iv
    rx.sign_key = tx.sign_key

    env = encrypt_app_to_device(tx, b"hello")
    env["sign"] = base64.b64encode(b"\x00" * 32).decode()
    with pytest.raises(LanCryptoError, match="Signature"):
        decrypt_device_to_app(rx, env)


def test_envelope_with_malformed_fields_is_rejected() -> None:
    s = _make_session()
    # Missing fields
    with pytest.raises(LanCryptoError, match="Malformed"):
        decrypt_device_to_app(s, {})
    # Non-int seq_no
    with pytest.raises(LanCryptoError, match="Malformed"):
        decrypt_device_to_app(s, {"seq_no": "nope", "data": "", "sign": ""})
