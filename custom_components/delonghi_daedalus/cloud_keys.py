"""Known AWS shadow field mapping for Daedalus cloud telemetry (issue #18).

The ``MachineStatus`` / ``MachineCapabilities`` shadow documents arrive as
flattened raw keys (``cloud.flatten_shadow``). mimasek71's Eletta Ultra
entity snapshot (ECAM470.75.TXB, posted 2026-11-14) revealed the field
names the cloud actually uses; this table maps them to a stable id, a
human-readable name (EN/FR via the platform translations) and the platform
that should render them:

- ``status``     → ``sensor`` on the main dashboard (operating state, codes)
- ``alarm``      → ``binary_sensor`` with ``device_class=problem``
- ``bool``       → plain on/off ``binary_sensor`` (peripherals, accessories)
- ``capability`` → ``sensor`` in the DIAGNOSTIC category (static limits)

Matching is on the LAST dot-separated segment of the flattened key,
normalised to lowercase alphanumerics (``alarms.Descale`` → ``descale``).
Aliases marked ``# guessed`` are inferred from mimasek71's English display
labels, not from a raw shadow dump — the anonymised raw JSON was requested
on #18 to complete this table. Unknown keys must keep the v1.8.0 fallback:
a raw ``Cloud <key>`` sensor.
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

    ``aliases`` must already be normalised (lowercase alphanumerics only);
    ``key`` is always matched as an alias too.
    """

    key: str
    kind: str
    en: str
    fr: str
    aliases: tuple[str, ...] = ()

    @property
    def binary(self) -> bool:
        return self.kind in (KIND_ALARM, KIND_BOOL)


# Field names in mimasek71's snapshot order (Status, Alarms, Peripherals,
# Accessories, Capabilities). The Czech entity ids in his file are his own
# local naming and carry no information — only the English labels do.
_TABLE: tuple[CloudKey, ...] = (
    # --- Status ----------------------------------------------------------
    CloudKey(
        "operating_state",
        KIND_STATUS,
        "Operating state",
        "État de la machine",
        ("operatingstate", "machinestate", "devicestate"),
    ),  # guessed
    CloudKey(
        "current_mode",
        KIND_STATUS,
        "Current mode (raw code)",
        "Mode courant (code brut)",
        ("currentmode", "actualmode", "modecode"),
    ),  # guessed
    CloudKey(
        "function_progress",
        KIND_STATUS,
        "Current function progress (raw code)",
        "Progression de la fonction en cours (code brut)",
        ("functionprogress", "currentfunction", "progresscode"),
    ),  # guessed
    CloudKey(
        "dispensing_value",
        KIND_STATUS,
        "Dispensing value (raw)",
        "Valeur de distribution (brute)",
        ("dispensingvalue", "pourvalue"),
    ),  # guessed
    CloudKey(
        "dispensing_error", KIND_ALARM, "Dispensing error", "Erreur de distribution", ("dispensingerror", "pourerror")
    ),  # guessed
    # --- Alarms ----------------------------------------------------------
    CloudKey("broken_grinder", KIND_ALARM, "Broken grinder", "Broyeur cassé", ("brokengrinder",)),
    CloudKey("bubble", KIND_ALARM, "Air bubble", "Bulle d'air", ("bubble",)),
    CloudKey(
        "no_coffee_beans",
        KIND_ALARM,
        "No coffee beans",
        "Réservoir à grains vide",
        ("nocoffeebeans", "beansempty", "nocoffee"),
    ),  # guessed
    CloudKey(
        "brew_unit_cleaning",
        KIND_ALARM,
        "Brew unit cleaning required",
        "Nettoyage du groupe infuseur requis",
        ("brewunitcleaning", "brewunitclean"),
    ),  # guessed
    CloudKey("cleaning", KIND_ALARM, "Cleaning required", "Nettoyage requis", ("cleaning",)),
    CloudKey("descale", KIND_ALARM, "Descaling required", "Détartrage requis", ("descale", "descaling")),
    CloudKey("drain", KIND_ALARM, "Drain required", "Purge requise", ("drain",)),
    CloudKey("fan", KIND_ALARM, "Fan", "Ventilateur", ("fan",)),
    CloudKey("fridge", KIND_ALARM, "Fridge", "Réfrigérateur à lait", ("fridge",)),
    CloudKey("ground_fine", KIND_ALARM, "Grind too fine", "Mouture trop fine", ("groundfine",)),
    CloudKey("heater_ntc", KIND_ALARM, "Heater NTC fault", "Sonde de chauffe (NTC) HS", ("heaterntc",)),
    CloudKey(
        "tank_level", KIND_ALARM, "Water tank level", "Niveau du réservoir d'eau", ("tanklevel", "waterlevel")
    ),  # guessed
    CloudKey("imbocco", KIND_ALARM, "Spout clog (imbocco)", "Buse bouchée (imbocco)", ("imbocco",)),
    CloudKey("infuser", KIND_ALARM, "Infuser", "Infuseur", ("infuser",)),
    CloudKey("motor_pos", KIND_ALARM, "Motor position fault", "Défaut de position moteur", ("motorpos",)),
    CloudKey(
        "general_fault", KIND_ALARM, "General fault", "Défaut général", ("generalfault", "generalerror")
    ),  # guessed
    # Appears in BOTH his alarms and peripherals sections — one entity, alarm
    # semantics (tank removed = problem).  # guessed
    CloudKey(
        "tank_presence", KIND_ALARM, "Water tank missing", "Réservoir d'eau absent", ("tankpresence", "tankmissing")
    ),
    CloudKey("ui_memory", KIND_ALARM, "UI memory error", "Erreur mémoire de l'interface", ("uimemory",)),
    CloudKey("water_filter", KIND_ALARM, "Replace water filter", "Filtre à eau à remplacer", ("waterfilter",)),
    CloudKey("wifi_down", KIND_ALARM, "Wi-Fi disconnected", "Wi-Fi déconnecté", ("wifidown",)),
    CloudKey(
        "grounds_container",
        KIND_ALARM,
        "Grounds container full",
        "Conteneur à marc plein",
        ("groundscontainer", "groundsfull"),
    ),  # guessed
    # --- Peripherals -------------------------------------------------------
    CloudKey("grinder1_zsw", KIND_BOOL, "Grinder 1 switch", "Contact broyeur 1", ("grinder1zsw",)),
    CloudKey("hot_jug", KIND_BOOL, "Hot milk jug", "Pichet lait chaud", ("hotjug",)),
    CloudKey("jug", KIND_BOOL, "Milk jug", "Pichet à lait", ("jug",)),
    CloudKey("motor_down_switch", KIND_BOOL, "Brew unit down switch", "Fin de course bas", ("motdwsw",)),
    CloudKey("motor_up_switch", KIND_BOOL, "Brew unit up switch", "Fin de course haut", ("motupsw",)),
    CloudKey("low_water", KIND_BOOL, "Low water level", "Niveau d'eau bas", ("lowwater",)),
    CloudKey(
        "drip_tray_presence",
        KIND_BOOL,
        "Drip tray present",
        "Bac d'égouttage présent",
        ("driptray", "driptraypresence"),
    ),  # guessed
    CloudKey(
        "spout_presence", KIND_BOOL, "Water spout present", "Bec d'eau présent", ("spoutpresence", "waterspoutpresence")
    ),  # guessed
    CloudKey("switch", KIND_BOOL, "Switch", "Interrupteur", ("switch",)),
    # --- Accessories -------------------------------------------------------
    # "None" and "Water" from his accessories section are deliberately NOT
    # mapped: as bare last segments they would swallow unrelated keys (e.g. a
    # ``tanks.water`` level field). They keep the raw fallback until the raw
    # shadow dump on #18 confirms their real paths.
    CloudKey("clean_cold", KIND_BOOL, "Cold cleaning cycle", "Cycle de nettoyage à froid", ("cleancold",)),
    CloudKey("clean_hot", KIND_BOOL, "Hot cleaning cycle", "Cycle de nettoyage à chaud", ("cleanhot",)),
    CloudKey("idf_cold", KIND_BOOL, "Cold milk carafe (IDF)", "Carafe lait froid (IDF)", ("idfcold",)),
    CloudKey("idf_hot", KIND_BOOL, "Hot milk system (IDF)", "Système lait chaud (IDF)", ("idfhot",)),
    CloudKey("steam_wand", KIND_BOOL, "Steam wand", "Buse vapeur", ("steamwand",)),
    # --- Capabilities / diagnostics -----------------------------------------
    CloudKey(
        "display_firmware",
        KIND_CAPABILITY,
        "Display firmware",
        "Micrologiciel de l'afficheur",
        ("displayfirmware", "fwdisplay"),
    ),  # guessed
    CloudKey(
        "mainboard_firmware",
        KIND_CAPABILITY,
        "Mainboard firmware",
        "Micrologiciel de la carte mère",
        ("mainboardfirmware", "mainboardfw"),
    ),  # guessed
    CloudKey(
        "machine_model", KIND_CAPABILITY, "Machine model", "Modèle de la machine", ("machinemodel", "model")
    ),  # guessed
    CloudKey("languages", KIND_CAPABILITY, "Languages", "Langues", ("languages",)),  # guessed
    CloudKey(
        "grind_levels", KIND_CAPABILITY, "Grind levels", "Niveaux de mouture", ("grindlevels", "grindinglevels")
    ),  # guessed
    CloudKey("drink_groups", KIND_CAPABILITY, "Drink groups", "Groupes de boissons", ("drinkgroups",)),  # guessed
    CloudKey(
        "custom_drinks",
        KIND_CAPABILITY,
        "Custom drinks capacity",
        "Capacité de boissons personnalisées",
        ("customdrinks",),
    ),  # guessed
    CloudKey(
        "bean_adapt_type",
        KIND_CAPABILITY,
        "Bean Adapt type",
        "Type Bean Adapt",
        ("beanadapt", "beanadapttype", "typebeanadapt"),
    ),  # guessed
    CloudKey(
        "display_type", KIND_CAPABILITY, "Display type", "Type d'afficheur", ("displaytype", "typedisplay")
    ),  # guessed
    CloudKey(
        "grinder_type", KIND_CAPABILITY, "Grinder type", "Type de broyeur", ("grindertype", "typegrinder")
    ),  # guessed
    CloudKey(
        "bean_hopper_type",
        KIND_CAPABILITY,
        "Bean hopper type",
        "Type de trémie à grains",
        ("beanhopper", "hopptype", "typebeanhopper"),
    ),  # guessed
    CloudKey(
        "graphics_version", KIND_CAPABILITY, "Graphics version", "Version des graphismes", ("graphicsversion",)
    ),  # guessed
    CloudKey(
        "water_hardness_options",
        KIND_CAPABILITY,
        "Water hardness options",
        "Options de dureté de l'eau",
        ("waterhardness", "waterhardnessoptions"),
    ),  # guessed
    CloudKey(
        "eco_mode_options",
        KIND_CAPABILITY,
        "Eco mode options",
        "Options du mode éco",
        ("ecocomode", "economodeoptions"),
    ),  # guessed
    CloudKey("sound_options", KIND_CAPABILITY, "Sound options", "Options sonores", ("soundoptions",)),  # guessed
    CloudKey("dictionaries", KIND_CAPABILITY, "Dictionaries", "Dictionnaires", ("dictionaries",)),  # guessed
    CloudKey(
        "statistics_types", KIND_CAPABILITY, "Statistics types", "Types de statistiques", ("statisticstypes",)
    ),  # guessed
    CloudKey("profile_colors", KIND_CAPABILITY, "Profile colors", "Couleurs de profil", ("profilecolors",)),  # guessed
    CloudKey(
        "recipe_name_length",
        KIND_CAPABILITY,
        "Recipe name length",
        "Longueur des noms de recettes",
        ("recipenamelength",),
    ),  # guessed
    CloudKey(
        "user_profile_name_length",
        KIND_CAPABILITY,
        "User profile name length",
        "Longueur des noms de profils utilisateur",
        ("userprofilenamelength",),
    ),  # guessed
    CloudKey(
        "bean_profile_name_length",
        KIND_CAPABILITY,
        "Bean profile name length",
        "Longueur des noms de profils grains",
        ("beanprofilenamelength",),
    ),  # guessed
    CloudKey(
        "supported_accessories",
        KIND_CAPABILITY,
        "Supported accessories",
        "Accessoires pris en charge",
        ("supportedaccessories",),
    ),  # guessed
    CloudKey(
        "coffee_moments",
        KIND_CAPABILITY,
        "Coffee Moments support",
        "Prise en charge Coffee Moments",
        ("coffeemoments",),
    ),  # guessed
    CloudKey(
        "extra_shot", KIND_CAPABILITY, "Extra Shot support", "Prise en charge Extra Shot", ("extrashot",)
    ),  # guessed
    CloudKey("mug_to_go", KIND_CAPABILITY, "Mug To Go support", "Prise en charge Mug To Go", ("mugtogo",)),  # guessed
    CloudKey(
        "water_drain_support",
        KIND_CAPABILITY,
        "Water drain support",
        "Prise en charge de la purge d'eau",
        ("waterdrain",),
    ),  # guessed
)

CLOUD_KEYS: tuple[CloudKey, ...] = _TABLE


def normalize(name: object) -> str:
    """Last dot-segment of a flattened key, lowercase alphanumerics only."""
    last = str(name).rsplit(".", 1)[-1]
    return "".join(ch for ch in last.lower() if ch.isascii() and ch.isalnum())


def _build_alias_index() -> dict[str, CloudKey]:
    index: dict[str, CloudKey] = {}
    for meta in _TABLE:
        for alias in dict.fromkeys(normalize(a) for a in (meta.key, *meta.aliases)):
            # First registration wins; test_cloud_keys asserts no alias maps
            # to two different entries, so this never silently shadows.
            index.setdefault(alias, meta)
    return index


_ALIAS_INDEX: dict[str, CloudKey] = _build_alias_index()


def lookup_cloud_key(flat_key: str) -> CloudKey | None:
    """Known-field metadata for a flattened shadow key, or None (raw fallback)."""
    return _ALIAS_INDEX.get(normalize(flat_key))


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
    """
    plan: list[tuple[str, str, CloudKey | None]] = []
    for source in CLOUD_SOURCES:
        flat = cloud.get(source) or {}
        count = 0
        for key in flat:
            display = str(key).strip()[:64]
            if not display:
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
