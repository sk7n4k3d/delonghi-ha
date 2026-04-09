"""Constants for De'Longhi Coffee integration."""

from typing import Any, Final

DOMAIN: Final = "delonghi_coffee"

# ──────────────────────────────────────────────────────────────────────
# API credentials — These are PUBLIC app-level keys extracted from the
# official De'Longhi Coffee Link Android app. They are NOT user secrets.
# Every user of the official app shares the same keys. They identify the
# application to Gigya (identity provider) and Ayla Networks (IoT cloud),
# similar to how a Google Maps API key is embedded in every app using it.
# ──────────────────────────────────────────────────────────────────────

# Gigya (De'Longhi identity provider)
GIGYA_API_KEY: Final = "4_DRIMLu7jk9bkKwpRRoQOuw"

# Region configurations
CONF_REGION: Final = "region"

# Gigya always uses EU1 datacenter — confirmed from AndroidManifest.xml
# Only Ayla endpoints change per region
GIGYA_URL: Final = "https://accounts.eu1.gigya.com"

# Each region has different Ayla credentials AND endpoints
REGIONS: Final[dict[str, dict[str, str]]] = {
    "EU": {
        "name": "Europe",
        "ayla_app_id": "DLonghiCoffeeIdKit-sQ-id",
        "ayla_app_secret": "DLonghiCoffeeIdKit-HT6b0VNd4y6CSha9ivM5k8navLw",
        "ayla_user": "https://user-field-eu.aylanetworks.com",
        "ayla_ads": "https://ads-eu.aylanetworks.com",
    },
    "US": {
        "name": "United States",
        "ayla_app_id": "DeLonghiCoffeeIdKit-yA-id",
        "ayla_app_secret": "DeLonghiCoffeeIdKit-2oUcfCkA0pUIACH8jSCwWsf1RcU",
        "ayla_user": "https://user-field.aylanetworks.com",
        "ayla_ads": "https://ads-field.aylanetworks.com",
    },
    "CN": {
        "name": "China",
        "ayla_app_id": "DeLonghiCoffeeIdKit-yA-id",
        "ayla_app_secret": "DeLonghiCoffeeIdKit-2oUcfCkA0pUIACH8jSCwWsf1RcU",
        "ayla_user": "https://user-field.ayla.com.cn",
        "ayla_ads": "https://ads-field.ayla.com.cn",
    },
}

# App signature appended to every command
APP_SIGNATURE: Final = bytes([0x20, 0x40, 0x35, 0xEF])

# Scan interval — status polling (monitor only, lightweight)
SCAN_INTERVAL_SECONDS: Final = 60

# Full refresh interval — counters, profiles, beans (heavy, includes ping)
FULL_REFRESH_INTERVAL: Final = 600  # 10 minutes

# MQTT keepalive interval — ping to prevent session expiry (cloud timeout ~300s)
MQTT_KEEPALIVE_INTERVAL: Final = 240  # 4 minutes

# HTTP timeouts (connect, read) in seconds
REQUEST_TIMEOUT: Final = (5, 15)

# Retry configuration
RETRY_COUNT: Final = 3
RETRY_DELAY: Final = 2  # seconds

# Beverage profiles — complete catalog from ContentStack + Ayla recipe keys
# Keys match Ayla recipe property names; drink_id is the ContentStack/ECAM numeric ID
BEVERAGES: Final[dict[str, dict[str, str]]] = {
    # ── Hot Coffee (1-25) ───────────────────────────────────────────────
    "espresso": {"name": "Espresso", "icon": "mdi:coffee", "drink_id": "1"},
    "regular": {"name": "Coffee", "icon": "mdi:coffee", "drink_id": "2"},
    "long_coffee": {"name": "Long Coffee", "icon": "mdi:coffee-outline", "drink_id": "3"},
    "2x_espresso": {"name": "Double Espresso", "icon": "mdi:coffee", "drink_id": "4"},
    "doppio_pl": {"name": "Doppio+", "icon": "mdi:coffee", "drink_id": "5"},
    "doppio": {"name": "Doppio+", "icon": "mdi:coffee", "drink_id": "5"},
    "americano": {"name": "Americano", "icon": "mdi:coffee-outline", "drink_id": "6"},
    "cappuccino": {"name": "Cappuccino", "icon": "mdi:coffee-maker-outline", "drink_id": "7"},
    "latte_macch": {"name": "Latte Macchiato", "icon": "mdi:glass-mug-variant", "drink_id": "8"},
    "latte_macchiato": {"name": "Latte Macchiato", "icon": "mdi:glass-mug-variant", "drink_id": "8"},
    "caffelatte": {"name": "Caffe Latte", "icon": "mdi:glass-mug-variant", "drink_id": "9"},
    "flat_white": {"name": "Flat White", "icon": "mdi:coffee", "drink_id": "10"},
    "espr_macch": {"name": "Espresso Macchiato", "icon": "mdi:coffee", "drink_id": "11"},
    "espr_macchiato": {"name": "Espresso Macchiato", "icon": "mdi:coffee", "drink_id": "11"},
    "hot_milk": {"name": "Hot Milk", "icon": "mdi:cup", "drink_id": "12"},
    "capp_doppio_pl": {"name": "Cappuccino Doppio+", "icon": "mdi:coffee-maker-outline", "drink_id": "13"},
    "capp_doppio": {"name": "Cappuccino Doppio+", "icon": "mdi:coffee-maker-outline", "drink_id": "13"},
    "capp_reverse": {"name": "Cappuccino Mix", "icon": "mdi:coffee-maker-outline", "drink_id": "15"},
    "hot_water": {"name": "Hot Water", "icon": "mdi:water-boiler", "drink_id": "16"},
    "espresso_lungo": {"name": "Espresso Lungo", "icon": "mdi:coffee-outline", "drink_id": "20"},
    "tea": {"name": "Tea", "icon": "mdi:tea", "drink_id": "22"},
    "coffee_pot": {"name": "Coffee Pot", "icon": "mdi:coffee-maker", "drink_id": "23"},
    "cortado": {"name": "Cortado", "icon": "mdi:coffee", "drink_id": "24"},
    "long_black": {"name": "Long Black", "icon": "mdi:coffee-outline", "drink_id": "25"},
    # ── New Hot (ContentStack) ──────────────────────────────────────────
    "brew_over_ice": {"name": "Brew Over Ice", "icon": "mdi:snowflake", "drink_id": "27"},
    "verlangerter": {"name": "Verlängerter", "icon": "mdi:coffee-outline", "drink_id": "28"},
    "cafe_con_leche": {"name": "Café Con Leche", "icon": "mdi:glass-mug-variant", "drink_id": "29"},
    "cafe_au_lait": {"name": "Café Au Lait", "icon": "mdi:glass-mug-variant", "drink_id": "30"},
    "galao": {"name": "Galão", "icon": "mdi:glass-mug-variant", "drink_id": "31"},
    # ── Iced (50-57) ────────────────────────────────────────────────────
    "i_americano": {"name": "Iced Americano", "icon": "mdi:snowflake", "drink_id": "50"},
    "i_cappuccino": {"name": "Iced Cappuccino", "icon": "mdi:snowflake", "drink_id": "51"},
    "i_latte_macch": {"name": "Iced Latte Macchiato", "icon": "mdi:snowflake", "drink_id": "52"},
    "i_capp_mix": {"name": "Iced Cappuccino Mix", "icon": "mdi:snowflake", "drink_id": "53"},
    "i_flatwhite": {"name": "Iced Flat White", "icon": "mdi:snowflake", "drink_id": "54"},
    "i_coldmilk": {"name": "Iced Cold Milk", "icon": "mdi:snowflake", "drink_id": "55"},
    "i_caffelatte": {"name": "Iced Caffe Latte", "icon": "mdi:snowflake", "drink_id": "56"},
    "over_ice_espr": {"name": "Iced Espresso", "icon": "mdi:snowflake", "drink_id": "57"},
    # ── Mug Hot (80-87) ─────────────────────────────────────────────────
    "mug_americano": {"name": "Mug Americano", "icon": "mdi:coffee-to-go", "drink_id": "80"},
    "mug_cappuccino": {"name": "Mug Cappuccino", "icon": "mdi:coffee-to-go", "drink_id": "81"},
    "mug_latte_macch": {"name": "Mug Latte Macchiato", "icon": "mdi:coffee-to-go", "drink_id": "82"},
    "mug_caffelatte": {"name": "Mug Caffe Latte", "icon": "mdi:coffee-to-go", "drink_id": "83"},
    "mug_capp_mix": {"name": "Mug Cappuccino Mix", "icon": "mdi:coffee-to-go", "drink_id": "84"},
    "mug_flat_white": {"name": "Mug Flat White", "icon": "mdi:coffee-to-go", "drink_id": "85"},
    "mug_hot_milk": {"name": "Mug Hot Milk", "icon": "mdi:coffee-to-go", "drink_id": "86"},
    "mug_coffee": {"name": "Mug Coffee", "icon": "mdi:coffee-to-go", "drink_id": "87"},
    # ── Mug Cold (100-107) ──────────────────────────────────────────────
    "mug_i_brew_over_ice": {"name": "Mug Iced Brew Over Ice", "icon": "mdi:snowflake", "drink_id": "100"},
    "mug_i_americano": {"name": "Mug Iced Americano", "icon": "mdi:snowflake", "drink_id": "101"},
    "mug_i_cappuccino": {"name": "Mug Iced Cappuccino", "icon": "mdi:snowflake", "drink_id": "102"},
    "mug_i_latte_macch": {"name": "Mug Iced Latte Macchiato", "icon": "mdi:snowflake", "drink_id": "103"},
    "mug_i_caffelatte": {"name": "Mug Iced Caffe Latte", "icon": "mdi:snowflake", "drink_id": "104"},
    "mug_i_capp_mix": {"name": "Mug Iced Cappuccino Mix", "icon": "mdi:snowflake", "drink_id": "105"},
    "mug_i_flat_white": {"name": "Mug Iced Flat White", "icon": "mdi:snowflake", "drink_id": "106"},
    "mug_i_cold_milk": {"name": "Mug Iced Cold Milk", "icon": "mdi:snowflake", "drink_id": "107"},
    # ── Cold Brew (120-124) ─────────────────────────────────────────────
    "a_cb_coffee": {"name": "Cold Brew Coffee", "icon": "mdi:snowflake", "drink_id": "120"},
    "b_cb_coffee_ess": {"name": "Cold Brew Essence", "icon": "mdi:snowflake", "drink_id": "121"},
    "c_cb_coffee_pot": {"name": "Cold Brew Pot", "icon": "mdi:snowflake", "drink_id": "122"},
    "d_cb_latte": {"name": "Cold Brew Latte", "icon": "mdi:snowflake", "drink_id": "123"},
    "e_cb_cappuccino": {"name": "Cold Brew Cappuccino", "icon": "mdi:snowflake", "drink_id": "124"},
    # ── Cold Brew Mug (140-142) ─────────────────────────────────────────
    "f_cb_mug": {"name": "Cold Brew Mug", "icon": "mdi:snowflake", "drink_id": "140"},
    "g_cb_latte_mug": {"name": "Cold Brew Latte Mug", "icon": "mdi:snowflake", "drink_id": "141"},
    "h_cb_capp_mug": {"name": "Cold Brew Cappuccino Mug", "icon": "mdi:snowflake", "drink_id": "142"},
    # ── My (personalized) ──────────────────────────────────────────────
    "m_americano": {"name": "My Americano", "icon": "mdi:coffee-outline"},
    "m_cappuccino": {"name": "My Cappuccino", "icon": "mdi:coffee-maker-outline"},
    "m_latte_macch": {"name": "My Latte Macchiato", "icon": "mdi:glass-mug-variant"},
    "m_caffelatte": {"name": "My Caffe Latte", "icon": "mdi:glass-mug-variant"},
    "m_capp_mix": {"name": "My Cappuccino Mix", "icon": "mdi:coffee-maker-outline"},
    "m_flat_white": {"name": "My Flat White", "icon": "mdi:coffee"},
    "m_hot_milk": {"name": "My Hot Milk", "icon": "mdi:cup"},
    "mug_to_go": {"name": "Mug To Go", "icon": "mdi:coffee-to-go"},
    # ── My Iced ─────────────────────────────────────────────────────────
    "mi_over_ice": {"name": "My Iced", "icon": "mdi:snowflake"},
    "mi_americano": {"name": "My Iced Americano", "icon": "mdi:snowflake"},
    "mi_capp": {"name": "My Iced Cappuccino", "icon": "mdi:snowflake"},
    "mi_latte_macch": {"name": "My Iced Latte Macchiato", "icon": "mdi:snowflake"},
    "mi_cafflatt": {"name": "My Iced Caffe Latte", "icon": "mdi:snowflake"},
    "mi_capp_mix": {"name": "My Iced Cappuccino Mix", "icon": "mdi:snowflake"},
    "mi_flat_white": {"name": "My Iced Flat White", "icon": "mdi:snowflake"},
    "mi_cold_milk": {"name": "My Iced Cold Milk", "icon": "mdi:snowflake"},
}

# ContentStack drink_id → recipe key reverse mapping (for beverage discovery)
DRINK_ID_TO_KEY: Final[dict[int, str]] = {
    int(meta["drink_id"]): key
    for key, meta in BEVERAGES.items()
    if "drink_id" in meta
}


def resolve_beverage_meta(
    bev_key: str,
    custom_recipe_names: dict[str, str],
) -> tuple[dict[str, str], bool]:
    """Resolve a beverage key to (meta, is_known).

    Used by the button platform to map a beverage key to a display name and
    icon. Selection priority:
        1. User-named custom recipe (custom_1..custom_6) — use the label.
        2. Beverage defined in BEVERAGES — use its name/icon.
        3. Unknown key — title-case fallback with the default coffee icon.

    Returns:
        A tuple of (meta dict with 'name' + 'icon', is_known flag).
        is_known is True when the beverage has either a custom name or a
        BEVERAGES entry, False for the title-cased fallback path.
    """
    custom_name = custom_recipe_names.get(bev_key)
    if custom_name:
        return {"name": custom_name, "icon": "mdi:coffee-to-go"}, True
    if bev_key in BEVERAGES:
        # Return a copy so callers can mutate it safely.
        return {"name": BEVERAGES[bev_key]["name"], "icon": BEVERAGES[bev_key]["icon"]}, True
    return {
        "name": bev_key.replace("_", " ").title(),
        "icon": "mdi:coffee",
    }, False

# Power on / wake up command (Request ID 132, contents 0x02 0x01)
POWER_ON_CMD: Final = bytes.fromhex("0d07840f02015512")

# Power off / standby command (Request ID 132, contents 0x01 0x01)
# CRC-16/SPI-FUJITSU checksum. Credit: MattG-K (framagit.org/mattgk/dlghiot)
POWER_OFF_CMD: Final = bytes.fromhex("0d07840f01010041")

# Monitor V2 alarm bit definitions (32-bit word from bytes[7], [8], [12], [13])
ALARMS: Final[dict[int, dict[str, Any]]] = {
    0: {"name": "Water Tank Empty", "icon": "mdi:water-off"},
    1: {"name": "Grounds Container Full", "icon": "mdi:delete-alert"},
    2: {"name": "Descale Needed", "icon": "mdi:water-alert"},
    3: {"name": "Replace Water Filter", "icon": "mdi:filter-remove"},
    4: {"name": "Coffee Ground Too Fine", "icon": "mdi:grain"},
    5: {"name": "Coffee Beans Empty", "icon": "mdi:seed-off"},
    6: {"name": "Machine Service Required", "icon": "mdi:wrench-clock"},
    7: {"name": "Heater Probe Failure", "icon": "mdi:thermometer-alert"},
    8: {"name": "Too Much Coffee", "icon": "mdi:coffee-off"},
    9: {"name": "Infuser Motor Failure", "icon": "mdi:engine-off"},
    10: {"name": "Steamer Probe Failure", "icon": "mdi:thermometer-alert"},
    11: {"name": "Drip Tray Missing", "icon": "mdi:tray-alert"},
    12: {"name": "Hydraulic Problem", "icon": "mdi:pipe-leak"},
    13: {"name": "Water Tank Missing", "icon": "mdi:water-off", "inverted": True},
    14: {"name": "Clean Milk Knob", "icon": "mdi:broom"},
    15: {"name": "Coffee Beans Empty 2", "icon": "mdi:seed-off"},
    16: {"name": "Cleaning Needed", "icon": "mdi:spray-bottle"},
    17: {"name": "Bean Hopper Absent", "icon": "mdi:tray-remove"},
    18: {"name": "Grid Missing", "icon": "mdi:grid", "inverted": True},
}

# Machine state values (from MonitorDataV2.f() -> byte[9])
# OEM model → friendly name mapping (from MachinesModels.json in Coffee Link APK)
MODEL_NAMES: Final[dict[str, str]] = {
    "DL-striker-cb": "Eletta Explore",
    "DL-striker-best": "Eletta Explore",
    "DL-pd-soul": "PrimaDonna Soul",
    "DL-pd-soul-better": "PrimaDonna Soul",
    "DL-pd-class-better": "PrimaDonna Class",
    "DL-pd-class-top": "PrimaDonna Class",
    "DL-pd-elite-better": "PrimaDonna Elite",
    "DL-pd-elite-mid": "PrimaDonna Elite",
    "DL-pd-elite-multi": "PrimaDonna Elite",
    "DL-pd-elite-top": "PrimaDonna Elite",
    "DL-pd-s-restyle": "PrimaDonna S",
    "DL-dinamica-plus": "Dinamica Plus",
    "DL-maestosa-best": "Maestosa",
    "DL-maestosa-good": "Maestosa",
}

# OEM model → TranscodeTable appModelId mapping
# Credit: TranscodeTable approach from FrozenGalaxy/PyDeLonghiAPI
OEM_TO_APP_MODEL: Final[dict[str, str]] = {
    "DL-striker-cb": "STRIKER_COLD-BREW",
    "DL-striker-best": "STRIKER_BEST",
    "DL-pd-soul": "PD_SOUL",
    "DL-pd-soul-better": "PD_SOUL_BETTER",
    "DL-pd-class-better": "PD_CLASS_BETTER_INT",
    "DL-pd-class-top": "PD_CLASS_TOP_INT",
    "DL-pd-elite-better": "PD_ELITE_BETTER_EX1",
    "DL-pd-elite-mid": "PD_ELITE_MID_INT",
    "DL-pd-elite-multi": "PD_ELITE_MULTI_INT",
    "DL-pd-s-restyle": "PD_S_RESTYLE_INT",
    "DL-dinamica-plus": "DINAMICA_PLUS",
    "DL-maestosa-best": "MAESTOSA_BEST",
    "DL-maestosa-good": "MAESTOSA_GOOD",
}

# TranscodeTable API endpoint (De'Longhi backend)
TRANSCODE_TABLE_URL: Final = "https://delonghibe.reply.it/api/getTranscodeTable.sr"

MACHINE_STATES: Final[dict[int, str]] = {
    0: "Off",
    1: "Turning On",
    2: "Idle",
    3: "Brewing",
    4: "Error",
    5: "Descaling",
    6: "Heating",
    7: "Ready",
    8: "Rinsing",
    9: "Going to sleep",
}
