"""Blocking-call fix: the trust-all TLS context must not be built on the loop.

`ssl.create_default_context()` runs `load_default_certs()` — a synchronous
walk of the system CA store — which HA logs as "Blocking call to ssl..." on
every LAN reconnect (issue #18 retest). With CERT_NONE the CA store is never
consulted anyway, so the context is built without it, once, off the loop.
"""

from __future__ import annotations

import asyncio
import ssl
import threading
from unittest.mock import patch

from custom_components.delonghi_daedalus import api


def test_trust_all_context_does_not_load_default_certs() -> None:
    """create_default_context() must not be called at all: its
    load_default_certs() is the blocking part, and a trust-all context never
    consults the CA store."""

    def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003 — spy signature
        raise AssertionError("ssl.create_default_context() must not be used")

    with patch("ssl.create_default_context", side_effect=_boom):
        ctx = api._build_trust_all_ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False
    # Same TLS floor create_default_context applied by default.
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_ssl_context_is_built_off_the_event_loop_and_cached() -> None:
    threads: list[str] = []
    real_build = api._build_trust_all_ssl_context

    def _spy_build() -> ssl.SSLContext:
        threads.append(threading.current_thread().name)
        return real_build()

    async def _run() -> None:
        loop_thread = threading.current_thread().name
        api._SSL_CTX = None
        try:
            with patch.object(api, "_build_trust_all_ssl_context", _spy_build):
                first = await api._get_trust_all_ssl_context()
                second = await api._get_trust_all_ssl_context()
            assert threads, "context was never built"
            assert threads == [threads[0]], "context rebuilt instead of cached"
            assert threads[0] != loop_thread, "TLS context built on the event-loop thread"
            assert first is second
            assert first.verify_mode == ssl.CERT_NONE
        finally:
            api._SSL_CTX = None

    asyncio.run(_run())
