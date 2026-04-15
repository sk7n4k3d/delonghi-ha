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
    parts = _R1.encode("utf-8") + _R2.encode("utf-8") + str(_T1).encode("utf-8") + str(_T2).encode("utf-8") + b"\x31"
    assert s.app_crypto_key == _nested_hmac(key, parts)


def test_derive_session_matches_nested_hmac_spec_sign_tag() -> None:
    """Sign key uses tag 0x30 (not 0x31 or 0x32)."""
    s = _make_session()
    key = _LAN_KEY.encode("utf-8")
    parts = _R1.encode("utf-8") + _R2.encode("utf-8") + str(_T1).encode("utf-8") + str(_T2).encode("utf-8") + b"\x30"
    assert s.app_sign_key == _nested_hmac(key, parts)


def test_derive_session_matches_nested_hmac_spec_iv_seed() -> None:
    """IV seed is the nested HMAC with tag 0x32, truncated to 16 bytes."""
    s = _make_session()
    key = _LAN_KEY.encode("utf-8")
    parts = _R1.encode("utf-8") + _R2.encode("utf-8") + str(_T1).encode("utf-8") + str(_T2).encode("utf-8") + b"\x32"
    expected = _nested_hmac(key, parts)[:16]
    assert s.app_iv == expected


def test_derive_session_device_branch_reverses_inputs() -> None:
    """dev_* derivation uses r2+r1+t2+t1 (reversed) per cremalink spec."""
    s = _make_session()
    key = _LAN_KEY.encode("utf-8")
    parts = _R2.encode("utf-8") + _R1.encode("utf-8") + str(_T2).encode("utf-8") + str(_T1).encode("utf-8") + b"\x31"
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


# ─────────────────────────────────────────────────────────────────────────
# run_lan_diagnostic — in-process end-to-end check. Mocks are not needed
# because the helper spins its own loopback server.
# ─────────────────────────────────────────────────────────────────────────


import asyncio  # noqa: E402

from custom_components.delonghi_coffee.lan import (  # noqa: E402
    LanDiagnosticResult,
    run_lan_diagnostic,
)

_HAS_AIOHTTP = False
try:
    import aiohttp  # noqa: F401

    _HAS_AIOHTTP = True
except ImportError:
    pass


def test_run_lan_diagnostic_missing_key_fails_gracefully() -> None:
    """No lan_key → diagnostic stops at cloud_config without raising."""
    result = asyncio.run(run_lan_diagnostic(lan_key=None, lan_ip="10.0.0.1", dsn="DSN-NOKEY"))
    assert isinstance(result, LanDiagnosticResult)
    assert result.success is False
    assert result.stage == "cloud_config"
    assert "lan_key" in result.reason
    assert result.details["dsn"] == "DSN-NOKEY"


@pytest.mark.skipif(not _HAS_AIOHTTP, reason="aiohttp not installed")
def test_run_lan_diagnostic_full_pipeline_success() -> None:
    """Crypto + handshake + encrypt + decrypt all the way through."""
    result = asyncio.run(
        run_lan_diagnostic(
            lan_key="0123456789abcdef0123456789abcdef",
            lan_ip="127.0.0.1",
            dsn="DSN-OK",
        )
    )
    assert result.success is True, result.summary()
    assert result.stage == "teardown"
    assert result.details["lan_key_present"] is True
    assert result.details["diagnostic_port"] > 0


def test_run_lan_diagnostic_never_raises_on_unexpected_error(monkeypatch) -> None:
    """Every error path should be caught and returned as a result."""
    from custom_components.delonghi_coffee import lan as lan_module

    def _boom(*_args, **_kwargs):  # noqa: ANN202
        raise RuntimeError("simulated server boot failure")

    # Force server startup to explode — the helper must still return.
    monkeypatch.setattr(lan_module, "DeLonghiLanServer", _boom)
    result = asyncio.run(
        run_lan_diagnostic(
            lan_key="0123456789abcdef0123456789abcdef",
            lan_ip="127.0.0.1",
            dsn="DSN-BOOM",
        )
    )
    assert isinstance(result, LanDiagnosticResult)
    assert result.success is False
    assert result.stage == "server_start"
    assert "simulated server boot failure" in result.reason


# ─────────────────────────────────────────────────────────────────────────
# Coordinator LAN integration — verify startup logic and property callback.
# ─────────────────────────────────────────────────────────────────────────

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from custom_components.delonghi_coffee.coordinator import DeLonghiCoordinator  # noqa: E402


def _make_coordinator(lan_config: dict | None = None) -> DeLonghiCoordinator:
    """Build a coordinator with mocked HA and API, injecting lan_config."""
    hass = MagicMock()
    api = MagicMock()
    coord = DeLonghiCoordinator(hass, api, dsn="DSN-TEST")
    # Stub HA's async_set_updated_data (not present in test mock base)
    coord.async_set_updated_data = MagicMock()
    if lan_config is not None:
        coord._lan_config = lan_config
    return coord


class TestCoordinatorLanStartup:
    """Verify _try_start_lan conditions."""

    @pytest.mark.skipif(not _HAS_AIOHTTP, reason="aiohttp not installed")
    def test_lan_starts_when_enabled_with_key_and_ip(self) -> None:
        coord = _make_coordinator(
            {"lan_enabled": True, "lanip_key": "abcdef1234567890abcdef1234567890", "lan_ip": "192.168.1.100"}
        )

        with (
            patch.object(DeLonghiCoordinator, "_get_local_ip", return_value="192.168.1.50"),
            patch("custom_components.delonghi_coffee.coordinator.DeLonghiLanServer") as mock_server_cls,
            patch("custom_components.delonghi_coffee.coordinator.register_with_device", new_callable=AsyncMock),
        ):
            mock_server = MagicMock()
            mock_server.start = AsyncMock()
            mock_server.port = 10280
            mock_server_cls.return_value = mock_server

            asyncio.run(coord._try_start_lan())

            mock_server_cls.assert_called_once()
            mock_server.start.assert_awaited_once()
            assert coord._lan_server is mock_server
            assert coord._lan_active is True
            assert coord._lan_start_attempted is True

    def test_lan_does_not_start_when_disabled(self) -> None:
        coord = _make_coordinator(
            {"lan_enabled": False, "lanip_key": "abcdef1234567890abcdef1234567890", "lan_ip": "192.168.1.100"}
        )

        with patch("custom_components.delonghi_coffee.coordinator.DeLonghiLanServer") as mock_cls:
            asyncio.run(coord._try_start_lan())
            mock_cls.assert_not_called()
            assert coord._lan_server is None
            assert coord._lan_active is False
            assert coord._lan_start_attempted is True

    def test_lan_does_not_start_when_key_missing(self) -> None:
        coord = _make_coordinator({"lan_enabled": True, "lanip_key": "", "lan_ip": "192.168.1.100"})

        with patch("custom_components.delonghi_coffee.coordinator.DeLonghiLanServer") as mock_cls:
            asyncio.run(coord._try_start_lan())
            mock_cls.assert_not_called()
            assert coord._lan_server is None

    def test_lan_does_not_start_when_ip_missing(self) -> None:
        coord = _make_coordinator({"lan_enabled": True, "lanip_key": "abcdef1234567890abcdef1234567890", "lan_ip": ""})

        with patch("custom_components.delonghi_coffee.coordinator.DeLonghiLanServer") as mock_cls:
            asyncio.run(coord._try_start_lan())
            mock_cls.assert_not_called()

    def test_lan_does_not_start_when_no_local_ip(self) -> None:
        coord = _make_coordinator(
            {"lan_enabled": True, "lanip_key": "abcdef1234567890abcdef1234567890", "lan_ip": "192.168.1.100"}
        )

        with (
            patch.object(DeLonghiCoordinator, "_get_local_ip", return_value=None),
            patch("custom_components.delonghi_coffee.coordinator.DeLonghiLanServer") as mock_cls,
        ):
            asyncio.run(coord._try_start_lan())
            mock_cls.assert_not_called()


class TestCoordinatorLanPropertyCallback:
    """Verify _on_lan_property caching."""

    def test_property_cached_with_name_and_value(self) -> None:
        coord = _make_coordinator()
        coord.data = {"status": "RUN"}
        data = {"property": {"name": "d302_monitor", "value": "0d1784cafebabe"}}

        asyncio.run(coord._on_lan_property(data))

        assert coord._lan_properties["d302_monitor"] == "0d1784cafebabe"

    def test_property_callback_handles_missing_property_key(self) -> None:
        coord = _make_coordinator()
        coord.data = {"status": "RUN"}
        data = {"seq_no": "1", "data": {}}

        asyncio.run(coord._on_lan_property(data))
        assert coord._lan_properties == {}

    def test_property_callback_handles_empty_data(self) -> None:
        coord = _make_coordinator()
        coord.data = {}

        asyncio.run(coord._on_lan_property({}))
        assert coord._lan_properties == {}

    def test_property_array_shape_is_cached(self) -> None:
        coord = _make_coordinator()
        coord.data = {"status": "RUN"}
        data = {
            "seq_no": "17",
            "data": {},
            "properties": [
                {"property": {"name": "d302_monitor", "value": "AAA="}},
                {"property": {"name": "d701_tot_bev_b", "value": 42}},
            ],
        }

        asyncio.run(coord._on_lan_property(data))

        assert coord._lan_properties["d302_monitor"] == "AAA="
        assert coord._lan_properties["d701_tot_bev_b"] == 42


class TestCoordinatorSendCommandLan:
    """Verify send_command_lan enqueues via LAN when session active."""

    def test_returns_false_when_no_server(self) -> None:
        coord = _make_coordinator()
        assert asyncio.run(coord.send_command_lan(b"\x0d\x07\x84")) is False

    def test_returns_false_when_no_session(self) -> None:
        coord = _make_coordinator()
        coord._lan_server = MagicMock()
        coord._lan_server.session = None
        assert asyncio.run(coord.send_command_lan(b"\x0d\x07\x84")) is False

    def test_enqueues_when_session_active(self) -> None:
        coord = _make_coordinator()
        coord.api._cmd_property = "data_request"
        coord.api._build_packet = MagicMock(return_value="b64packet==")
        server = MagicMock()
        server.session = MagicMock()
        server.enqueue_command = AsyncMock()
        coord._lan_server = server

        ok = asyncio.run(coord.send_command_lan(b"\x0d\x07\x84\x0f\x02\x01\x55\x12"))

        assert ok is True
        coord.api._build_packet.assert_called_once()
        # include_app_id should be False for data_request (legacy PrimaDonna)
        assert coord.api._build_packet.call_args.kwargs == {"include_app_id": False}
        server.enqueue_command.assert_awaited_once()
        payload = server.enqueue_command.call_args.args[0]
        prop = payload["properties"][0]["property"]
        assert prop["name"] == "data_request"
        assert prop["dsn"] == "DSN-TEST"
        assert prop["value"] == "b64packet=="
        assert prop["base_type"] == "string"

    def test_uses_app_data_request_for_striker(self) -> None:
        coord = _make_coordinator()
        coord.api._cmd_property = "app_data_request"
        coord.api._build_packet = MagicMock(return_value="b64==")
        server = MagicMock()
        server.session = MagicMock()
        server.enqueue_command = AsyncMock()
        coord._lan_server = server

        asyncio.run(coord.send_command_lan(b"\x0d\x07\x84"))

        assert coord.api._build_packet.call_args.kwargs == {"include_app_id": True}
        payload = server.enqueue_command.call_args.args[0]
        assert payload["properties"][0]["property"]["name"] == "app_data_request"


class TestCoordinatorGetLocalIp:
    """Verify _get_local_ip helper."""

    def test_returns_string_for_reachable_ip(self) -> None:
        # 127.0.0.1 is always reachable
        result = DeLonghiCoordinator._get_local_ip("127.0.0.1")
        assert result is not None
        assert isinstance(result, str)

    def test_returns_none_for_invalid_ip(self) -> None:
        result = DeLonghiCoordinator._get_local_ip("not_an_ip_address")
        assert result is None


class TestCoordinatorStopLan:
    """Verify async_stop_lan cleanup."""

    def test_stop_lan_calls_server_stop(self) -> None:
        coord = _make_coordinator()
        mock_server = MagicMock()
        mock_server.stop = AsyncMock()
        coord._lan_server = mock_server
        coord._lan_active = True

        asyncio.run(coord.async_stop_lan())

        mock_server.stop.assert_awaited_once()
        assert coord._lan_server is None
        assert coord._lan_active is False

    def test_stop_lan_noop_when_no_server(self) -> None:
        coord = _make_coordinator()
        # Should not raise
        asyncio.run(coord.async_stop_lan())
