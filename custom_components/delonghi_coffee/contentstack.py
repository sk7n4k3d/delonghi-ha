"""ContentStack CMS client for De'Longhi drink catalog and bean adapt data."""

from __future__ import annotations

import logging
import re
from typing import Any

import requests

_LOGGER = logging.getLogger(__name__)

# ContentStack APP stack (machine-specific data: drinks, capabilities, beans)
_CS_BASE = "https://eu-cdn.contentstack.com/v3"
_CS_API_KEY = "blte8a11bbe13d91219"
_CS_TOKEN = "csf710b7815abf44748585d287"
_CS_ENV = "production"
_CS_HEADERS = {
    "api_key": _CS_API_KEY,
    "access_token": _CS_TOKEN,
    "environment": _CS_ENV,
}
_CS_TIMEOUT = (5, 15)

# Model families that De'Longhi actually populates in the ContentStack CMS.
# A full enumeration of the prod_drink content type on 2026-04-09 returned
# only ECAM47060/70/80 and ECAM63030/50/60/70/630 entries — no ECAM61
# (PrimaDonna Soul), ECAM37 (Dinamica Plus), ECAM45 or any other family.
# When the machine model sits outside this set, we skip the fetch entirely
# instead of logging warnings on every setup. See issue #6 for details.
_SUPPORTED_FAMILIES: tuple[str, ...] = ("ECAM47", "ECAM63")


def _iter_pattern_candidates(pattern: str) -> list[str]:
    """Yield progressively shorter ECAM prefixes for CMS lookup.

    ContentStack prod_drink titles embed the compact ECAM model
    (e.g. ``ECAM63050``). For unknown machines in a supported family we
    retry with a shorter prefix until we hit entries — "ECAM63099" →
    "ECAM6309" → "ECAM630" → "ECAM63". We bottom out at a two-digit
    family prefix so we never query "ECAM" on its own (which would match
    the entire catalog).
    """
    match = re.fullmatch(r"(ECAM)(\d+)", pattern.upper())
    if not match:
        return [pattern] if pattern else []
    prefix, digits = match.group(1), match.group(2)
    # Always keep the caller's exact pattern first, even when it's shorter
    # than the two-digit family floor.
    stop = min(2, len(digits))
    candidates = [f"{prefix}{digits[:n]}" for n in range(len(digits), stop - 1, -1)]
    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for cand in candidates:
        if cand not in seen:
            unique.append(cand)
            seen.add(cand)
    return unique


def is_family_supported(pattern: str) -> bool:
    """Return True when ``pattern`` belongs to a family present in the CMS."""
    if not pattern:
        return False
    upper = pattern.upper()
    return any(upper.startswith(family) for family in _SUPPORTED_FAMILIES)


def _cs_get(content_type: str, query: dict[str, Any] | None = None, limit: int = 100, skip: int = 0) -> list[dict[str, Any]]:
    """Fetch entries from ContentStack."""
    import json

    params: dict[str, Any] = {"limit": limit, "skip": skip}
    if query:
        params["query"] = json.dumps(query)
    try:
        resp = requests.get(
            f"{_CS_BASE}/content_types/{content_type}/entries",
            headers=_CS_HEADERS,
            params=params,
            timeout=_CS_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json().get("entries", [])
    except (requests.RequestException, ValueError) as err:
        _LOGGER.warning("ContentStack fetch %s failed: %s", content_type, err)
        return []


def fetch_drink_catalog(sku: str, model_name: str = "") -> dict[int, dict[str, Any]]:
    """Fetch all drinks for a machine from ContentStack.

    Args:
        sku: Machine SKU (e.g. "0132250181") or model (e.g. "ECAM63050").
        model_name: Model name for title matching fallback.

    Returns:
        Dict of drink_id → {name, clusters, ingredients: [{name, min, max, default}]}.
    """
    # Build the list of candidate title patterns to probe. We try the caller's
    # primary pattern first, then fall back to its family-prefix variants for
    # unknown models (e.g. ECAM63099 → ECAM6309 → ECAM630 → ECAM63). If a
    # separate model_name was provided, we append its candidates too.
    patterns: list[str] = _iter_pattern_candidates(sku) if sku else []
    if model_name and model_name != sku:
        for candidate in _iter_pattern_candidates(model_name):
            if candidate not in patterns:
                patterns.append(candidate)

    # Short-circuit on families we know are absent from the CMS. Spamming
    # the HTTP endpoint on every setup just to rediscover that ECAM61 isn't
    # indexed is pointless; see issue #6.
    if patterns and not any(is_family_supported(p) for p in patterns):
        _LOGGER.info(
            "ContentStack: family not indexed for '%s' (supported: %s), skipping drink fetch",
            sku or model_name,
            ", ".join(_SUPPORTED_FAMILIES),
        )
        return {}

    entries: list[dict[str, Any]] = []
    for pattern in patterns:
        if not is_family_supported(pattern):
            continue
        entries = _cs_get("prod_drink", query={"title": {"$regex": pattern}}, limit=100)
        if entries:
            _LOGGER.info("ContentStack: found %d drinks for pattern '%s'", len(entries), pattern)
            break

    if not entries:
        _LOGGER.info(
            "ContentStack: no drinks indexed for SKU=%s model=%s (tried %s)",
            sku,
            model_name,
            ", ".join(patterns) or "<none>",
        )
        return {}

    catalog: dict[int, dict[str, Any]] = {}
    for entry in entries:
        try:
            drink_id = int(entry.get("drink_id", "0"))
        except (ValueError, TypeError):
            continue
        if drink_id == 0:
            continue

        ingredients: list[dict[str, Any]] = []
        for ing in entry.get("ingredients", []):
            name = ing.get("name", "")
            if not name:
                continue
            try:
                ingredients.append({
                    "name": name,
                    "min": int(ing.get("minval", "0")),
                    "max": int(ing.get("maxval", "0")),
                    "default": int(ing.get("defval", "0")),
                })
            except (ValueError, TypeError):
                continue

        catalog[drink_id] = {
            "name": entry.get("original_title", entry.get("title", f"Drink {drink_id}")),
            "clusters": entry.get("cluster", []),
            "ingredients": ingredients,
        }

    _LOGGER.info("ContentStack: parsed %d drinks", len(catalog))
    return catalog


def fetch_bean_adapt(sku: str, model_name: str = "") -> dict[str, Any] | None:
    """Fetch bean adapt calibration data for a machine.

    Returns:
        Dict with bean_table, roasting_table, grinder settings, flow settings,
        or None if not found.
    """
    patterns: list[str] = _iter_pattern_candidates(sku) if sku else []
    if model_name and model_name != sku:
        for candidate in _iter_pattern_candidates(model_name):
            if candidate not in patterns:
                patterns.append(candidate)

    if patterns and not any(is_family_supported(p) for p in patterns):
        _LOGGER.info(
            "ContentStack: bean_adapt not indexed for '%s' (supported: %s), skipping",
            sku or model_name,
            ", ".join(_SUPPORTED_FAMILIES),
        )
        return None

    entries: list[dict[str, Any]] = []
    for pattern in patterns:
        if not is_family_supported(pattern):
            continue
        entries = _cs_get("bean_adapt", query={"title": {"$regex": pattern}}, limit=5)
        if entries:
            break
    if not entries:
        _LOGGER.debug(
            "ContentStack: no bean adapt data for SKU=%s model=%s (tried %s)",
            sku,
            model_name,
            ", ".join(patterns) or "<none>",
        )
        return None

    entry = entries[0]
    tp = entry.get("technical_parameters", {})
    contents = entry.get("contents", {})

    result: dict[str, Any] = {
        "title": entry.get("title", ""),
        "bean_types": contents.get("bean_type", []),
        "roasting_levels": contents.get("roasting_levels", []),
        "taste_feedback": contents.get("taste_feedback", []),
        "bean_table": tp.get("bean_table", {}).get("value", []),
        "roasting_table": tp.get("roasting_table", {}).get("value", []),
        "grinder_min": _int(tp.get("grinder_level_min")),
        "grinder_max": _int(tp.get("grinder_level_max")),
        "grinder_step": _int(tp.get("grinder_level_step")),
        "flow_min": _int(tp.get("min_flow")),
        "flow_max": _int(tp.get("max_flow")),
        "flow_delta": _int(tp.get("delta_value")),
        "preinfusion_water_min": _int(tp.get("preinfusion_water_min")),
        "preinfusion_water_max": _int(tp.get("preinfusion_water_max")),
    }
    _LOGGER.info("ContentStack: loaded bean adapt for %s", result["title"])
    return result


def fetch_coffee_beans(locale: str = "en-gb", limit: int = 100) -> list[dict[str, Any]]:
    """Fetch coffee bean catalog (roaster names, profiles, buy links).

    Returns:
        List of {name, roaster, roasting_level, coffee_type, acidity, bitterness, body_level, ...}.
    """
    all_beans: list[dict[str, Any]] = []
    skip = 0
    while True:
        entries = _cs_get("coffee_bean", limit=limit, skip=skip)
        if not entries:
            break
        for entry in entries:
            all_beans.append({
                "name": entry.get("name", entry.get("title", "")),
                "roaster": entry.get("roaster", ""),
                "roaster_id": entry.get("roaster_id", ""),
                "roasting_level": entry.get("roasting_level", ""),
                "coffee_type": entry.get("coffee_type", ""),
                "botany": entry.get("botany", ""),
                "acidity": _int(entry.get("acidity")),
                "bitterness": _int(entry.get("bitterness")),
                "body_level": _int(entry.get("body_level")),
                "taste_hint": entry.get("taste_hint", ""),
                "description": entry.get("description", ""),
                "image": entry.get("image", ""),
                "buy_from": entry.get("buy_from", ""),
            })
        if len(entries) < limit:
            break
        skip += limit

    _LOGGER.info("ContentStack: loaded %d coffee beans", len(all_beans))
    return all_beans


def _int(val: Any) -> int:
    """Safe int conversion."""
    if val is None:
        return 0
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0
