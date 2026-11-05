"""Shared isolation for the Daedalus tests (audit 2026-11-05, F1).

`redact._KNOWN_HOST_FORMS` is process-wide state (the host registry backs the
logger filter). Tests that exercise `connect_lan` register their fixture
hosts; restore the set around every test so registration order never leaks
into unrelated assertions.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from custom_components.delonghi_daedalus import redact


@pytest.fixture(autouse=True)
def _isolate_host_registry() -> Iterator[None]:
    saved = set(redact._KNOWN_HOST_FORMS)
    try:
        yield
    finally:
        redact._KNOWN_HOST_FORMS.clear()
        redact._KNOWN_HOST_FORMS.update(saved)
