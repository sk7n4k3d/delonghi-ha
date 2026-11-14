"""Known AWS shadow field mapping for Daedalus cloud telemetry (issue #18).

The ``MachineStatus`` / ``MachineCapabilities`` shadow documents arrive as
flattened raw keys (``cloud.flatten_shadow``). mimasek71's Eletta Ultra
entity snapshot (2026-11-14) and the anonymised raw shadow JSON he posted
the same day revealed the field names the cloud actually uses; this table
maps them to a stable id, a human-readable name (EN/FR via the platform
translations) and the platform that should render them:

- ``status``     → ``sensor`` on the main dashboard (operating state, codes)
- ``alarm``      → ``binary_sensor`` with ``device_class=problem``
- ``bool``       → plain on/off ``binary_sensor`` (peripherals, accessories)
- ``capability`` → ``sensor`` in the DIAGNOSTIC category (static limits)

Matching is on the LAST dot-separated segment of the flattened key,
normalised to lowercase alphanumerics (``alarms.Descale`` → ``descale``);
an alias containing a dot matches the FULL path instead — used where the
same segment appears under two parents (``alarms.TankPresence`` vs
``peripherals.TankPresence``, ``accessories.None``/``Water``). Aliases
marked ``# confirmed`` are real keys from mimasek71's raw dump; the
remaining guesses stay as fallbacks for other Daedalus models. Unknown
keys must keep the v1.8.0 fallback: a raw ``Cloud <key>`` sensor. Keys in
``SENSITIVE_KEYS`` (BLE pairing code, MAC, serial) are never exposed as
entities at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KIND_STATUS = "status"
KIND_ALARM = "alarm"
KIND_BOOL = "bool"
KIND_CAPABILITY = "capability"


@dataclass(frozen=True)
class CloudKey:
    """One known shadow field: stable id + rendering metadata.

    Aliases must already be normalised (lowercase alphanumerics only, or
    dotted full paths); ``key`` is always matched as an alias too.
    """

    key: str
    kind: str
    en: str
    fr: str
    aliases: tuple[str, ...] = ()

    @property
    def binary(self) -> bool:
        return self.kind in (KIND_ALARM, KIND_BOOL)


# Field names in mimasek71's raw dump order. Old guessed aliases are kept
# alongside the confirmed ones: other Daedalus models (Eletta Ultra firmware
# variants, future machines) may spell the same concept differently.
_TABLE: tuple[CloudKey, ...] = (
    # --- Status ----------------------------------------------------------
    CloudKey(
        "operating_state",
        KIND_STATUS,
        "Operating state",
        "État de la machine",
        ("operatingstate", "machinestate", "devicestate", "status"),
    ),  # status confirmed
    CloudKey(
        "current_mode",
        KIND_STATUS,
        "Current mode (raw code)",
        "Mode courant (code brut)",
        ("currentmode", "actualmode", "modecode", "currmode"),
    ),  # currmode confirmed
    CloudKey(
        "function_progress",
        KIND_STATUS,
        "Current function progress (raw code)",
        "Progression de la fonction en cours (code brut)",
        ("functionprogress", "currentfunction", "progresscode", "currfuncprogress"),
    ),  # currfuncprogress confirmed
    CloudKey(
        "dispensing_value",
        KIND_STATUS,
        "Dispensing value (raw)",
        "Valeur de distribution (brute)",
        ("dispensingvalue", "pourvalue", "beveragedispensing"),
    ),  # beveragedispensing confirmed
    CloudKey(
        "dispensing_error",
        KIND_ALARM,
        "Dispensing error",
        "Erreur de distribution",
        ("dispensingerror", "pourerror", "exitbeveragewitherror"),
    ),  # exitbeveragewitherror confirmed
    CloudKey(
        "step_description", KIND_STATUS, "Step description", "Description de l'étape", ("stepdescr", "stepdescription")
    ),  # confirmed
    # --- Alarms ----------------------------------------------------------
    CloudKey("broken_grinder", KIND_ALARM, "Broken grinder", "Broyeur cassé", ("brokengrinder",)),  # confirmed
    CloudKey("bubble", KIND_ALARM, "Air bubble", "Bulle d'air", ("bubble",)),  # confirmed
    CloudKey(
        "no_coffee_beans",
        KIND_ALARM,
        "No coffee beans",
        "Réservoir à grains vide",
        ("nocoffeebeans", "beansempty", "nocoffee"),
    ),  # nocoffee confirmed below
    CloudKey(
        "brew_unit_cleaning",
        KIND_ALARM,
        "Brew unit cleaning required",
        "Nettoyage du groupe infuseur requis",
        ("brewunitcleaning", "brewunitclean"),
    ),  # brewunitclean confirmed
    CloudKey("cleaning", KIND_ALARM, "Cleaning required", "Nettoyage requis", ("cleaning", "clean")),  # clean confirmed
    CloudKey("descale", KIND_ALARM, "Descaling required", "Détartrage requis", ("descale", "descaling")),  # confirmed
    CloudKey("drain", KIND_ALARM, "Drain required", "Purge requise", ("drain",)),  # confirmed
    CloudKey("fan", KIND_ALARM, "Fan", "Ventilateur", ("fan",)),  # confirmed
    CloudKey("fridge", KIND_ALARM, "Fridge", "Réfrigérateur à lait", ("fridge",)),  # confirmed
    CloudKey("ground_fine", KIND_ALARM, "Grind too fine", "Mouture trop fine", ("groundfine",)),  # confirmed
    CloudKey("heater_ntc", KIND_ALARM, "Heater NTC fault", "Sonde de chauffe (NTC) HS", ("heaterntc",)),  # confirmed
    CloudKey(
        "tank_level", KIND_ALARM, "Water tank level", "Niveau du réservoir d'eau", ("tanklevel", "waterlevel")
    ),  # tanklevel confirmed
    CloudKey("imbocco", KIND_ALARM, "Spout clog (imbocco)", "Buse bouchée (imbocco)", ("imbocco",)),  # confirmed
    CloudKey("infuser", KIND_ALARM, "Infuser", "Infuseur", ("infuser",)),  # confirmed
    CloudKey("motor_pos", KIND_ALARM, "Motor position fault", "Défaut de position moteur", ("motorpos",)),  # confirmed
    CloudKey(
        "general_fault", KIND_ALARM, "General fault", "Défaut général", ("generalfault", "generalerror", "general")
    ),  # general confirmed
    # TankPresence appears under BOTH alarms and peripherals in the raw dump;
    # full-path aliases keep the two entities distinct.
    CloudKey(
        "tank_presence",
        KIND_ALARM,
        "Water tank missing",
        "Réservoir d'eau absent",
        ("tankpresence", "tankmissing", "alarms.tankpresence"),
    ),  # confirmed
    CloudKey(
        "peripheral_tank_presence",
        KIND_BOOL,
        "Water tank present",
        "Réservoir d'eau présent",
        ("peripherals.tankpresence",),
    ),  # confirmed
    CloudKey("ui_memory", KIND_ALARM, "UI memory error", "Erreur mémoire de l'interface", ("uimemory",)),  # confirmed
    CloudKey(
        "water_filter", KIND_ALARM, "Replace water filter", "Filtre à eau à remplacer", ("waterfilter", "filter")
    ),  # filter confirmed
    CloudKey("wifi_down", KIND_ALARM, "Wi-Fi disconnected", "Wi-Fi déconnecté", ("wifidown",)),  # confirmed
    CloudKey(
        "grounds_container",
        KIND_ALARM,
        "Grounds container full",
        "Conteneur à marc plein",
        ("groundscontainer", "groundsfull", "waste"),
    ),  # waste confirmed
    # --- Peripherals -------------------------------------------------------
    CloudKey("grinder1_zsw", KIND_BOOL, "Grinder 1 switch", "Contact broyeur 1", ("grinder1zsw",)),  # confirmed
    CloudKey("hot_jug", KIND_BOOL, "Hot milk jug", "Pichet lait chaud", ("hotjug",)),  # confirmed
    CloudKey("jug", KIND_BOOL, "Milk jug", "Pichet à lait", ("jug",)),  # confirmed
    CloudKey("motor_down_switch", KIND_BOOL, "Brew unit down switch", "Fin de course bas", ("motdwsw",)),  # confirmed
    CloudKey("motor_up_switch", KIND_BOOL, "Brew unit up switch", "Fin de course haut", ("motupsw",)),  # confirmed
    CloudKey("low_water", KIND_BOOL, "Low water level", "Niveau d'eau bas", ("lowwater", "watertanklow")),  # confirmed
    CloudKey(
        "drip_tray_presence",
        KIND_BOOL,
        "Drip tray present",
        "Bac d'égouttage présent",
        ("driptray", "driptraypresence", "drippresence"),
    ),  # drippresence confirmed
    CloudKey(
        "spout_presence",
        KIND_BOOL,
        "Water spout present",
        "Bec d'eau présent",
        ("spoutpresence", "waterspoutpresence", "waterspout"),
    ),  # waterspout confirmed
    CloudKey("switch", KIND_BOOL, "Switch", "Interrupteur", ("switch",)),  # confirmed
    # --- Accessories -------------------------------------------------------
    # "None"/"Water" only exist under the accessories parent — registered as
    # full-path aliases so a bare "water" segment can never swallow an
    # unrelated key (e.g. a future tanks.water level field).
    CloudKey("clean_cold", KIND_BOOL, "Cold cleaning cycle", "Cycle de nettoyage à froid", ("cleancold",)),  # confirmed
    CloudKey("clean_hot", KIND_BOOL, "Hot cleaning cycle", "Cycle de nettoyage à chaud", ("cleanhot",)),  # confirmed
    CloudKey("idf_cold", KIND_BOOL, "Cold milk carafe (IDF)", "Carafe lait froid (IDF)", ("idfcold",)),  # confirmed
    CloudKey("idf_hot", KIND_BOOL, "Hot milk system (IDF)", "Système lait chaud (IDF)", ("idfhot",)),  # confirmed
    CloudKey("steam_wand", KIND_BOOL, "Steam wand", "Buse vapeur", ("steamwand",)),  # confirmed
    CloudKey("accessory_none", KIND_BOOL, "No accessory", "Aucun accessoire", ("accessories.none",)),  # confirmed
    CloudKey("accessory_water", KIND_BOOL, "Water accessory", "Accessoire eau", ("accessories.water",)),  # confirmed
    # --- Capabilities / diagnostics -----------------------------------------
    CloudKey(
        "display_firmware",
        KIND_CAPABILITY,
        "Display firmware",
        "Micrologiciel de l'afficheur",
        ("displayfirmware", "fwdisplay", "fwuiversion"),
    ),  # fwuiversion confirmed
    CloudKey(
        "mainboard_firmware",
        KIND_CAPABILITY,
        "Mainboard firmware",
        "Micrologiciel de la carte mère",
        ("mainboardfirmware", "mainboardfw", "fwmainboardversion"),
    ),  # fwmainboardversion confirmed
    CloudKey(
        "wifi_firmware", KIND_CAPABILITY, "Wi-Fi firmware", "Micrologiciel Wi-Fi", ("fwwifiversion",)
    ),  # confirmed
    CloudKey(
        "cloud_agent_version", KIND_CAPABILITY, "Cloud agent version", "Version agent cloud", ("dlawsagentversion",)
    ),  # confirmed
    CloudKey(
        "machine_model",
        KIND_CAPABILITY,
        "Machine model",
        "Modèle de la machine",
        ("machinemodel", "model", "machinemodelui"),
    ),  # machinemodelui confirmed
    CloudKey(
        "machine_model_main", KIND_CAPABILITY, "Mainboard model", "Modèle carte mère", ("machinemodelmain",)
    ),  # confirmed
    CloudKey("languages", KIND_CAPABILITY, "Languages", "Langues", ("languages",)),  # confirmed
    CloudKey(
        "grind_levels",
        KIND_CAPABILITY,
        "Grind levels",
        "Niveaux de mouture",
        ("grindlevels", "grindinglevels", "granulometries"),
    ),  # granulometries confirmed
    CloudKey(
        "drink_groups", KIND_CAPABILITY, "Drink groups", "Groupes de boissons", ("drinkgroups", "drinkclusters")
    ),  # drinkclusters confirmed
    CloudKey(
        "custom_drinks",
        KIND_CAPABILITY,
        "Custom drinks capacity",
        "Capacité de boissons personnalisées",
        ("customdrinks", "customizeddrinks"),
    ),  # confirmed
    CloudKey(
        "bean_adapt_type",
        KIND_CAPABILITY,
        "Bean Adapt type",
        "Type Bean Adapt",
        ("beanadapt", "beanadapttype", "typebeanadapt"),
    ),  # beanadapttype confirmed
    CloudKey(
        "display_type", KIND_CAPABILITY, "Display type", "Type d'afficheur", ("displaytype", "typedisplay")
    ),  # displaytype confirmed
    CloudKey(
        "grinder_type", KIND_CAPABILITY, "Grinder type", "Type de broyeur", ("grindertype", "typegrinder", "grinder")
    ),  # grinder confirmed
    CloudKey(
        "bean_hopper_type",
        KIND_CAPABILITY,
        "Bean hopper type",
        "Type de trémie à grains",
        ("beanhopper", "hopptype", "hopper"),
    ),  # hopper confirmed
    CloudKey(
        "graphics_version",
        KIND_CAPABILITY,
        "Graphics version",
        "Version des graphismes",
        ("graphicsversion", "gfxversion"),
    ),  # gfxversion confirmed
    CloudKey(
        "water_hardness_options",
        KIND_CAPABILITY,
        "Water hardness options",
        "Options de dureté de l'eau",
        ("waterhardness", "waterhardnessoptions"),
    ),  # waterhardness confirmed
    CloudKey(
        "eco_mode_options",
        KIND_CAPABILITY,
        "Eco mode options",
        "Options du mode éco",
        ("ecocomode", "economodeoptions", "energysavingmode"),
    ),  # energysavingmode confirmed
    CloudKey(
        "sound_options", KIND_CAPABILITY, "Sound options", "Options sonores", ("soundoptions", "soundenable")
    ),  # soundenable confirmed
    CloudKey("dictionaries", KIND_CAPABILITY, "Dictionaries", "Dictionnaires", ("dictionaries",)),  # confirmed
    CloudKey(
        "statistics_types",
        KIND_CAPABILITY,
        "Statistics types",
        "Types de statistiques",
        ("statisticstypes", "machinestatistictypes"),
    ),  # machinestatistictypes confirmed
    CloudKey(
        "profile_colors",
        KIND_CAPABILITY,
        "Profile colors",
        "Couleurs de profil",
        ("profilecolors", "machineuserprofilecolours"),
    ),  # confirmed
    CloudKey(
        "recipe_name_length",
        KIND_CAPABILITY,
        "Recipe name length",
        "Longueur des noms de recettes",
        ("recipenamelength", "customrecipenamelen"),
    ),  # customrecipenamelen confirmed
    CloudKey(
        "user_profile_name_length",
        KIND_CAPABILITY,
        "User profile name length",
        "Longueur des noms de profils utilisateur",
        ("userprofilenamelength", "userprofilenamelen"),
    ),  # userprofilenamelen confirmed
    CloudKey(
        "bean_profile_name_length",
        KIND_CAPABILITY,
        "Bean profile name length",
        "Longueur des noms de profils grains",
        ("beanprofilenamelength", "beansystemnamelen"),
    ),  # beansystemnamelen confirmed
    CloudKey(
        "supported_accessories",
        KIND_CAPABILITY,
        "Supported accessories",
        "Accessoires pris en charge",
        ("supportedaccessories",),
    ),  # confirmed
    CloudKey(
        "coffee_moments",
        KIND_CAPABILITY,
        "Coffee Moments support",
        "Prise en charge Coffee Moments",
        ("coffeemoments",),
    ),  # confirmed
    CloudKey(
        "extra_shot", KIND_CAPABILITY, "Extra Shot support", "Prise en charge Extra Shot", ("extrashot",)
    ),  # confirmed
    CloudKey("mug_to_go", KIND_CAPABILITY, "Mug To Go support", "Prise en charge Mug To Go", ("mugtogo",)),  # confirmed
    CloudKey(
        "water_drain_support",
        KIND_CAPABILITY,
        "Water drain support",
        "Prise en charge de la purge d'eau",
        ("waterdrain", "drainwater"),
    ),  # drainwater confirmed
    CloudKey(
        "machine_user_profiles", KIND_CAPABILITY, "User profiles", "Profils utilisateur", ("machineuserprofiles",)
    ),  # confirmed
    CloudKey(
        "user_profile_icons",
        KIND_CAPABILITY,
        "User profile icons",
        "Icônes de profil utilisateur",
        ("machineuserprofileicons",),
    ),  # confirmed
    CloudKey(
        "coffee_temperatures", KIND_CAPABILITY, "Coffee temperatures", "Températures café", ("coffeetemperatures",)
    ),  # confirmed
    CloudKey(
        "auto_turn_off_options",
        KIND_CAPABILITY,
        "Auto turn-off times (min)",
        "Extinction automatique (min)",
        ("autoturnofftimeinminutes",),
    ),  # confirmed
)

CLOUD_KEYS: tuple[CloudKey, ...] = _TABLE

# Shadow keys that must NEVER become entities: the BLE pairing code is a
# credential and MAC/SN are device identifiers — the raw fallback would
# persist them in the HA recorder and in exported diagnostics.
SENSITIVE_KEYS: frozenset[str] = frozenset({"blepasscode", "mac", "sn"})


def normalize(name: object) -> str:
    """Last dot-segment of a flattened key, lowercase alphanumerics only."""
    last = str(name).rsplit(".", 1)[-1]
    return "".join(ch for ch in last.lower() if ch.isascii() and ch.isalnum())


def normalize_path(name: object) -> str:
    """Full dotted path, lowercase alphanumerics only (dots stripped)."""
    return "".join(ch for ch in str(name).lower() if ch.isascii() and ch.isalnum())


def _build_alias_index() -> dict[str, CloudKey]:
    index: dict[str, CloudKey] = {}
    for meta in _TABLE:
        for alias in dict.fromkeys((meta.key, *meta.aliases)):
            norm = normalize_path(alias) if "." in alias else normalize(alias)
            # First registration wins; test_cloud_keys asserts no alias maps
            # to two different entries, so this never silently shadows.
            index.setdefault(norm, meta)
    return index


_ALIAS_INDEX: dict[str, CloudKey] = _build_alias_index()


def lookup_cloud_key(flat_key: str) -> CloudKey | None:
    """Known-field metadata for a flattened shadow key, or None (raw fallback).

    Full-path aliases win over last-segment ones so ``peripherals.TankPresence``
    and ``alarms.TankPresence`` can map to distinct entries.
    """
    return _ALIAS_INDEX.get(normalize_path(flat_key)) or _ALIAS_INDEX.get(normalize(flat_key))


# The two coordinator cloud sources, in dashboard order.
CLOUD_SOURCES: tuple[str, ...] = ("status_flat", "capabilities_flat")
# Defense in depth: flatten_shadow already caps each document at 80 keys;
# re-check here so a stale cached snapshot can never blow up entity count.
_MAX_PER_SOURCE = 80


def plan_cloud_entities(cloud: dict[str, Any], *, want_binary: bool) -> list[tuple[str, str, CloudKey | None]]:
    """(source, raw key, meta) for every shadow key one platform should own.

    Shared by sensor.py and binary_sensor.py so the two platforms partition
    the shadow keys exactly: ``want_binary=True`` takes mapped alarm/bool
    keys, ``False`` takes everything else (unknown keys keep the raw
    fallback). Audit #18 (E1) guards live here: shadow keys are
    cloud-controlled — blank ones are dropped and the display length is
    capped so a hostile key cannot build a colliding or oversized entity id.
    ``SENSITIVE_KEYS`` (BLE passcode, MAC, SN) are dropped entirely.
    """
    plan: list[tuple[str, str, CloudKey | None]] = []
    for source in CLOUD_SOURCES:
        flat = cloud.get(source) or {}
        count = 0
        for key in flat:
            display = str(key).strip()[:64]
            if not display or normalize(key) in SENSITIVE_KEYS:
                continue
            meta = lookup_cloud_key(key)
            if bool(meta is not None and meta.binary) != want_binary:
                continue
            count += 1
            if count > _MAX_PER_SOURCE:
                break
            plan.append((source, key, meta))
    return plan


# "ano"/"ne" are Czech yes/no — mimasek71's fork surfaced those strings, and a
# regionalised firmware could plausibly send them as raw values.
_TRUE_TOKENS = frozenset({"true", "1", "yes", "on", "ano"})
_FALSE_TOKENS = frozenset({"false", "0", "no", "off", "ne"})


def coerce_bool(value: object) -> bool | None:
    """Best-effort bool for a raw shadow value; None = unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
    return None
