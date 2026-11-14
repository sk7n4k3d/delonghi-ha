"""cloud_keys table: alias integrity, lookup, bool coercion, translations.

The table content is inferred from mimasek71's Eletta Ultra snapshot
(issue #18) — these tests guard the invariants (no alias collisions, every
known key translated) rather than the guessed raw names themselves.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.delonghi_daedalus.cloud_keys import (
    CLOUD_KEYS,
    coerce_bool,
    lookup_cloud_key,
    normalize,
    plan_cloud_entities,
)

TRANSLATIONS_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "delonghi_daedalus" / "translations"


def test_ids_unique() -> None:
    ids = [meta.key for meta in CLOUD_KEYS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("meta", CLOUD_KEYS, ids=lambda m: m.key)
def test_alias_roundtrip(meta) -> None:
    """Every alias must resolve back to its own entry (catches collisions)."""
    for alias in (meta.key, *meta.aliases):
        found = lookup_cloud_key(alias)
        assert found is not None and found.key == meta.key, f"alias {alias!r} resolved to {found}"


def test_lookup_uses_last_dot_segment() -> None:
    assert lookup_cloud_key("alarms.Descale").key == "descale"
    assert lookup_cloud_key("MachineStatus.OperatingState").key == "operating_state"
    assert lookup_cloud_key("a.b.tank_presence").key == "tank_presence"


def test_lookup_unknown_returns_none() -> None:
    assert lookup_cloud_key("totally_unknown_field") is None
    assert lookup_cloud_key("") is None
    assert lookup_cloud_key("...") is None


def test_normalize() -> None:
    assert normalize("alarms.Descale") == "descale"
    assert normalize("  MixedCASE  ") == "mixedcase"
    assert normalize("with_underscore") == "withunderscore"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("FALSE", False),
        ("Ano", True),
        ("Ne", False),
        (" 1 ", True),
        ("garbage", None),
        (None, None),
        ([], None),
    ],
)
def test_coerce_bool(raw, expected) -> None:
    assert coerce_bool(raw) is expected


def test_plan_partitions_platforms() -> None:
    cloud = {
        "status_flat": {"Descale": True, "OperatingState": "Standby", "opaque.field": 3},
        "capabilities_flat": {"Languages": 27},
    }
    binary = {(s, k) for s, k, _ in plan_cloud_entities(cloud, want_binary=True)}
    sensors = {(s, k, m is None) for s, k, m in plan_cloud_entities(cloud, want_binary=False)}
    assert binary == {("status_flat", "Descale")}
    assert sensors == {
        ("status_flat", "OperatingState", False),
        ("status_flat", "opaque.field", True),  # unknown → raw fallback sensor
        ("capabilities_flat", "Languages", False),
    }


def test_plan_drops_blank_keys() -> None:
    cloud = {"status_flat": {"   ": 1}}
    assert plan_cloud_entities(cloud, want_binary=True) == []
    assert plan_cloud_entities(cloud, want_binary=False) == []


def test_plan_caps_per_source() -> None:
    cloud = {"status_flat": {f"opaque{i}": i for i in range(200)}}
    assert len(plan_cloud_entities(cloud, want_binary=False)) <= 80


@pytest.mark.parametrize("lang", ["en", "fr"])
def test_translations_cover_every_known_key(lang: str) -> None:
    data = json.loads((TRANSLATIONS_DIR / f"{lang}.json").read_text(encoding="utf-8"))
    entity = data["entity"]
    for meta in CLOUD_KEYS:
        section = "binary_sensor" if meta.binary else "sensor"
        entry = entity.get(section, {}).get(f"cloud_{meta.key}")
        assert entry is not None, f"{lang}.json missing {section}.cloud_{meta.key}"
        assert entry["name"].strip(), f"{lang}.json empty name for cloud_{meta.key}"
