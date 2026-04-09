"""Unit tests for the cremalink-compatible LAN crypto + helpers.

These tests act as regression anchors for the key derivation formula and
the exact wire format. If anyone touches derive_session, build_*_payload,
or the IV rotation, they have to update these vectors (or justify why).
"""

from __future__ import annotations

import base64
import hashlib
import hmac as _hmac
import json
import os

import pytest

from custom_components.delonghi_coffee.lan import (
    LanCryptoError,
    LanSession,
    _aes_decrypt,
    _aes_encrypt,
    _pad_zero,
    _rotate_iv_from_ciphertext,
    _unpad_zero,
    build_command_payload,
    build_heartbeat_payload,
    decrypt_device_to_app,
    derive_session,
    encrypt_app_to_device,
    sign_payload,
    verify_signature,
)

# ─────────────────────────────────────────────────────────────────────────
# Fixture values — deterministic so a single change to derive_session()
# flips multiple tests at once.
# ─────────────────────────────────────────────────────────────────────────

_LAN_KEY = "0123456789abcdef0123456789abcdef"
_R1 = "rnd_one_0001"
_R2 = "rnd_two_0002"
_T1 = 1700000000
_T2 = 1700000001


def _make_session() -> LanSession:
    return derive_session(_LAN_KEY, _R1, _R2, _T1, _T2)


def _nested_hmac(key: bytes, parts: bytes) -> bytes:
    """Spec reference — H(k, H(k, m) || m). Mirrors cremalink precisely."""
    inner = _hmac.new(key, parts, hashlib.sha256).digest()
    return _hmac.new(key, inner + parts, hashlib.sha256).digest()


# ─────────────────────────────────────────────────────────────────────────
# Zero padding.
# ─────────────────────────────────────────────────────────────────────────


def test_pad_zero_always_adds_at_least_one_byte() -> None:
    # 16-byte input must still be padded (otherwise we can't distinguish
    # "plaintext was already a multiple" from "plaintext ends in \x00").
    assert len(_pad_zero(b"a" * 16)) == 32
    assert len(_pad_zero(b"")) == 16


def test_pad_zero_rounds_up_to_block() -> None:
    assert len(_pad_zero(b"abc")) == 16
    assert len(_pad_zero(b"a" * 17)) == 32


def test_unpad_zero_strips_trailing_nulls() -> None:
    assert _unpad_zero(b"hello\x00\x00\x00") == b"hello"
    assert _unpad_zero(b"hello") == b"hello"
    assert _unpad_zero(b"\x00" * 16) == b""


# ─────────────────────────────────────────────────────────────────────────
# derive_session — structural + deterministic + formula-matching.
# ─────────────────────────────────────────────────────────────────────────


def test_derive_session_returns_correct_key_sizes() -> None:
    s = _make_session()
    # cremalink keeps the full 32-byte HMAC digest as the crypto key,
    # which makes this effectively AES-256-CBC.
    assert len(s.app_sign_key) == 32
    assert len(s.app_crypto_key) == 32
    assert len(s.dev_crypto_key) == 32
    # IV seeds are truncated to the AES block size.
    assert len(s.app_iv) == 16
    assert len(s.dev_iv) == 16
    # Metadata round-trips through the dataclass.
    assert s.random_1 == _R1
    assert s.random_2 == _R2
    assert s.time_1 == _T1
    assert s.time_2 == _T2


def test_derive_session_is_deterministic() -> None:
    a = _make_session()
    b = _make_session()
    assert a.app_sign_key == b.app_sign_key
    assert a.app_crypto_key == b.app_crypto_key
    assert a.dev_crypto_key == b.dev_crypto_key
    assert a.app_iv == b.app_iv
    assert a.dev_iv == b.dev_iv


def test_derive_session_roles_are_distinct() -> None:
    """Every tag byte (0x30, 0x31, 0x32) must produce a distinct key."""
    s = _make_session()
    assert s.app_sign_key != s.app_crypto_key
    assert s.app_sign_key[:16] != s.app_iv
    assert s.app_crypto_key != s.dev_crypto_key
    assert s.app_iv != s.dev_iv


def test_derive_session_matches_nested_hmac_spec_app_branch() -> None:
    """app_crypto_key must equal H(k, H(k, r1+r2+t1+t2+0x31) || ...)."""
    s = _make_session()
    key = _LAN_KEY.encode("utf-8")
    parts = (
        _R1.encode("utf-8")
        + _R2.encode("utf-8")
        + str(_T1).encode("utf-8")
        + str(_T2).encode("utf-8")
        + b"\x31"
    )
    assert s.app_crypto_key == _nested_hmac(key, parts)


def test_derive_session_matches_nested_hmac_spec_sign_tag() -> None:
    """Sign key uses tag 0x30 (not 0x31 or 0x32)."""
    s = _make_session()
    key = _LAN_KEY.encode("utf-8")
    parts = (
        _R1.encode("utf-8")
        + _R2.encode("utf-8")
        + str(_T1).encode("utf-8")
        + str(_T2).encode("utf-8")
        + b"\x30"
    )
    assert s.app_sign_key == _nested_hmac(key, parts)


def test_derive_session_matches_nested_hmac_spec_iv_seed() -> None:
    """IV seed is the nested HMAC with tag 0x32, truncated to 16 bytes."""
    s = _make_session()
    key = _LAN_KEY.encode("utf-8")
    parts = (
        _R1.encode("utf-8")
        + _R2.encode("utf-8")
        + str(_T1).encode("utf-8")
        + str(_T2).encode("utf-8")
        + b"\x32"
    )
    expected = _nested_hmac(key, parts)[:16]
    assert s.app_iv == expected


def test_derive_session_device_branch_reverses_inputs() -> None:
    """dev_* derivation uses r2+r1+t2+t1 (reversed) per cremalink spec."""
    s = _make_session()
    key = _LAN_KEY.encode("utf-8")
    parts = (
        _R2.encode("utf-8")
        + _R1.encode("utf-8")
        + str(_T2).encode("utf-8")
        + str(_T1).encode("utf-8")
        + b"\x31"
    )
    assert s.dev_crypto_key == _nested_hmac(key, parts)


def test_derive_session_flipping_inputs_swaps_app_and_dev_branches() -> None:
    """Feeding swapped randoms mirrors the roles — app becomes dev."""
    a = _make_session()
    b = derive_session(_LAN_KEY, _R2, _R1, _T2, _T1)
    assert a.app_crypto_key == b.dev_crypto_key
    assert a.dev_crypto_key == b.app_crypto_key
    assert a.app_iv == b.dev_iv
    assert a.dev_iv == b.app_iv


def test_derive_session_rejects_empty_inputs() -> None:
    with pytest.raises(LanCryptoError, match="lan_key"):
        derive_session("", _R1, _R2, _T1, _T2)
    with pytest.raises(LanCryptoError, match="random_1"):
        derive_session(_LAN_KEY, "", _R2, _T1, _T2)
    with pytest.raises(LanCryptoError, match="random_2"):
        derive_session(_LAN_KEY, _R1, "", _T1, _T2)


# ─────────────────────────────────────────────────────────────────────────
# AES round trip with zero padding.
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "plaintext",
    [
        "",  # empty
        "hello world",
        "a" * 15,  # pre-block
        "a" * 16,  # block boundary — zero padding MUST add a full block
        "a" * 17,  # post-block
        '{"seq_no":"1","data":{}}',  # heartbeat-shape
        "x" * 1024,  # large
    ],
)
def test_aes_round_trip(plaintext: str) -> None:
    """AES-256-CBC + zero pad round-trips for non-null-terminated strings."""
    key = os.urandom(32)
    iv = os.urandom(16)
    enc = _aes_encrypt(plaintext, key, iv)
    # Result is valid base64.
    base64.b64decode(enc)
    recovered = _aes_decrypt(enc, key, iv)
    assert recovered.decode("utf-8") == plaintext


def test_aes_decrypt_with_wrong_key_produces_different_bytes() -> None:
    """Zero padding is lenient: wrong key won't necessarily raise, but the
    plaintext definitely won't match."""
    key1 = os.urandom(32)
    key2 = os.urandom(32)
    iv = os.urandom(16)
    enc = _aes_encrypt("secret command", key1, iv)
    recovered = _aes_decrypt(enc, key2, iv)
    assert recovered != b"secret command"


# ─────────────────────────────────────────────────────────────────────────
# IV chain — next IV = last 16 bytes of ciphertext.
# ─────────────────────────────────────────────────────────────────────────


def test_rotate_iv_is_last_16_bytes_of_raw_ciphertext() -> None:
    key = os.urandom(32)
    iv = os.urandom(16)
    enc = _aes_encrypt("hello world this is a longer message", key, iv)
    next_iv = _rotate_iv_from_ciphertext(enc)
    assert len(next_iv) == 16
    assert next_iv == base64.b64decode(enc)[-16:]


def test_encrypt_app_to_device_advances_iv_chain() -> None:
    s = _make_session()
    before = s.app_iv
    encrypt_app_to_device(s, "first payload")
    mid = s.app_iv
    assert mid != before
    encrypt_app_to_device(s, "second payload")
    assert s.app_iv != mid


def test_encrypt_twice_with_same_plaintext_yields_different_ciphertexts() -> None:
    """IV rotation means identical payloads MUST produce different outputs
    on consecutive calls — this is the whole point of an IV chain."""
    s = _make_session()
    enc_a, _ = encrypt_app_to_device(s, "same payload")
    enc_b, _ = encrypt_app_to_device(s, "same payload")
    assert enc_a != enc_b


# ─────────────────────────────────────────────────────────────────────────
# Signing — over the plaintext, not the ciphertext.
# ─────────────────────────────────────────────────────────────────────────


def test_sign_and_verify_round_trip() -> None:
    s = _make_session()
    sig = sign_payload(s.app_sign_key, "command bytes")
    assert verify_signature(s.app_sign_key, "command bytes", sig)


def test_sign_is_over_plaintext_not_ciphertext() -> None:
    """Two different ciphertexts of the same plaintext share a signature."""
    s = _make_session()
    enc_a, sig_a = encrypt_app_to_device(s, "payload")
    enc_b, sig_b = encrypt_app_to_device(s, "payload")
    assert enc_a != enc_b  # IV advanced
    assert sig_a == sig_b  # but the signature is over the plaintext


def test_verify_rejects_tampered_payload() -> None:
    s = _make_session()
    sig = sign_payload(s.app_sign_key, "original")
    assert not verify_signature(s.app_sign_key, "tampered", sig)


def test_verify_rejects_tampered_signature() -> None:
    s = _make_session()
    good = sign_payload(s.app_sign_key, "payload")
    bad = base64.b64encode(b"\x00" * 32).decode()
    assert good != bad
    assert not verify_signature(s.app_sign_key, "payload", bad)


def test_verify_tolerates_garbage_signature() -> None:
    s = _make_session()
    assert not verify_signature(s.app_sign_key, "payload", "not_base64!@#")
    assert not verify_signature(s.app_sign_key, "payload", "")


# ─────────────────────────────────────────────────────────────────────────
# Envelope round trip — full app → device / device → app simulation.
# ─────────────────────────────────────────────────────────────────────────


def _sync_dev_with_app(rx: LanSession, tx: LanSession) -> None:
    """Make rx's dev channel talk to tx's app channel.

    In the wild, the coffee machine derives its own dev keys that match
    the server's app keys by symmetry. For a unit test we just borrow the
    opposite-side keys from the tx session.
    """
    rx.dev_crypto_key = tx.app_crypto_key
    rx.dev_iv = tx.app_iv


def test_app_to_device_round_trip_via_matching_session() -> None:
    tx = _make_session()
    rx = _make_session()
    _sync_dev_with_app(rx, tx)

    payload = '{"seq_no":"1","data":{"cmd":"0d07840f0201"}}'
    enc, sig = encrypt_app_to_device(tx, payload)
    plaintext = decrypt_device_to_app(rx, enc)
    assert plaintext.decode("utf-8") == payload
    assert verify_signature(tx.app_sign_key, payload, sig)


def test_envelope_chain_stays_synced_over_multiple_messages() -> None:
    tx = _make_session()
    rx = _make_session()
    _sync_dev_with_app(rx, tx)

    for i in range(5):
        payload = f'{{"seq_no":"{i}","data":{{"step":{i}}}}}'
        enc, _ = encrypt_app_to_device(tx, payload)
        got = decrypt_device_to_app(rx, enc)
        assert got.decode("utf-8") == payload
        assert tx.app_iv == rx.dev_iv  # both sides walk the same IV chain


# ─────────────────────────────────────────────────────────────────────────
# Payload formatting helpers — exact wire format (no spaces, str seq_no).
# ─────────────────────────────────────────────────────────────────────────


def test_build_heartbeat_payload_is_compact_json() -> None:
    assert build_heartbeat_payload(7) == '{"seq_no":"7","data":{}}'


def test_build_command_payload_is_compact_json() -> None:
    out = build_command_payload(3, {"cmd": "brew"})
    assert out == '{"seq_no":"3","data":{"cmd":"brew"}}'


def test_payloads_parse_back_with_stringified_seq() -> None:
    hb = json.loads(build_heartbeat_payload(42))
    assert hb["seq_no"] == "42"
    assert hb["data"] == {}
    cmd = json.loads(build_command_payload(1, {"x": 1}))
    assert cmd["data"] == {"x": 1}
    assert cmd["seq_no"] == "1"  # stringified on the wire
