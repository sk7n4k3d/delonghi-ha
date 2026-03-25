"""Constants for De'Longhi Coffee integration."""

from typing import Final

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

# Ayla Networks (IoT cloud platform)
AYLA_APP_ID: Final = "DLonghiCoffeeIdKit-sQ-id"
AYLA_APP_SECRET: Final = "DLonghiCoffeeIdKit-HT6b0VNd4y6CSha9ivM5k8navLw"

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

# Scan interval
SCAN_INTERVAL_SECONDS: Final = 30

# HTTP timeouts (connect, read) in seconds
REQUEST_TIMEOUT: Final = (5, 15)

# Retry configuration
RETRY_COUNT: Final = 3
RETRY_DELAY: Final = 2  # seconds

# Beverage profiles — mapped from captured data
BEVERAGES: Final[dict[str, dict[str, str]]] = {
    "espresso": {"name": "Espresso", "icon": "mdi:coffee"},
    "regular": {"name": "Coffee", "icon": "mdi:coffee"},
    "long_coffee": {"name": "Long Coffee", "icon": "mdi:coffee-outline"},
    "2x_espresso": {"name": "Double Espresso", "icon": "mdi:coffee"},
    "doppio_pl": {"name": "Doppio+", "icon": "mdi:coffee"},
    "americano": {"name": "Americano", "icon": "mdi:coffee-outline"},
    "cappuccino": {"name": "Cappuccino", "icon": "mdi:coffee-maker-outline"},
    "latte_macch": {"name": "Latte Macchiato", "icon": "mdi:glass-mug-variant"},
    "caffelatte": {"name": "Caffe Latte", "icon": "mdi:glass-mug-variant"},
    "flat_white": {"name": "Flat White", "icon": "mdi:coffee"},
    "espr_macch": {"name": "Espresso Macchiato", "icon": "mdi:coffee"},
    "hot_milk": {"name": "Hot Milk", "icon": "mdi:cup"},
    "capp_doppio_pl": {"name": "Cappuccino Doppio+", "icon": "mdi:coffee-maker-outline"},
    "capp_reverse": {"name": "Cappuccino Mix", "icon": "mdi:coffee-maker-outline"},
    "hot_water": {"name": "Hot Water", "icon": "mdi:water-boiler"},
    "tea": {"name": "Tea", "icon": "mdi:tea"},
    "coffee_pot": {"name": "Coffee Pot", "icon": "mdi:coffee-maker"},
    "cortado": {"name": "Cortado", "icon": "mdi:coffee"},
    "brew_over_ice": {"name": "Brew Over Ice", "icon": "mdi:snowflake"},
}

# Captured MITM brew commands — verified working, byte-for-byte exact
CAPTURED_BREWS: Final[dict[str, bytes]] = {
    "espresso": bytes.fromhex("0d1383f0010308000100281b01020527010651d7"),
    "hot_water": bytes.fromhex("0d1183f010031c010f00961b012701061189"),
    "tea": bytes.fromhex("0d1383f016031c010f00961b010d01270106b65b"),
    "i_americano": bytes.fromhex("0d1083f032031b0102011f00270106ade8"),
}

# Legacy alias
CAPTURED_BREW_ESPRESSO: Final = CAPTURED_BREWS["espresso"]

# Power on / wake up command (Request ID 132, contents 0x02 0x01)
POWER_ON_CMD: Final = bytes.fromhex("0d07840f02015512")

# Power off / standby command (Request ID 132, contents 0x01 0x01)
# CRC-16/SPI-FUJITSU checksum. Credit: MattG-K (framagit.org/mattgk/dlghiot)
POWER_OFF_CMD: Final = bytes.fromhex("0d07840f01010041")

# Monitor V2 alarm bit definitions (32-bit word from bytes[7], [8], [12], [13])
ALARMS: Final[dict[int, dict[str, str]]] = {
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
    13: {"name": "Tank In Position", "icon": "mdi:check-circle"},
    14: {"name": "Clean Milk Knob", "icon": "mdi:broom"},
    15: {"name": "Coffee Beans Empty 2", "icon": "mdi:seed-off"},
    16: {"name": "Cleaning Needed", "icon": "mdi:spray-bottle"},
    17: {"name": "Bean Hopper Absent", "icon": "mdi:tray-remove"},
    18: {"name": "Grid Present", "icon": "mdi:grid"},
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

MACHINE_STATES: Final[dict[int, str]] = {
    0: "Off",
    1: "Turning On",
    2: "Idle",
    3: "Brewing",
    4: "Error",
    5: "Descaling",
    6: "Heating",
    7: "Ready",
}
