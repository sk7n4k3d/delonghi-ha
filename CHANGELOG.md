# Changelog

All notable changes to this integration are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [1.6.0-beta.16] — 2026-05-08

### Fixed

- **`Switch: monitor confirmed Off state` logged after a turn_off that
  never reached the machine**. Live trace 2026-05-08 on Bastien's Eletta
  Explore: `cloud=SYNC` (MQTT keepalive dead, machine WiFi module
  zombie), monitor frozen 961 min on `machine_state="Ready"`. Coordinator
  was rebranding `Ready → Off` purely on monitor staleness, regardless of
  cloud session health. A `turn_off` request fired POWER_OFF (HTTP 201
  accepted by Ayla cloud, never relayed via MQTT), the next refresh
  matched the fabricated "Off" against `_last_commanded_on=False`, and
  the switch logged `monitor confirmed Off state` — a flat lie, the
  machine was still physically running. Fix gates the "Off" rebrand on
  `cloud_status == "RUN"`. With `cloud=SYNC` the last known monitor
  ("Ready" frozen pre-zombie) wins, and the switch's existing
  `_monitor_stale_count` machinery correctly flips to `assumed_state`
  after 3 polls instead of inventing a confirmation. Locked by 2 new
  regression tests in `TestMonitorStaleness`:
  `test_stale_monitor_with_cloud_sync_does_not_force_off` (the fix, with
  the quoted live monitor raw for traceability) +
  `test_stale_monitor_with_cloud_run_still_forces_off` (explicit
  positive case for the gate).

## [1.6.0-beta.15] — 2026-05-08

### Fixed

- **Firmware recipe templates leaked into beverage discovery**. The cloud
  surfaces `default`, `default_1..7` and `bs_recipe` under the same
  `dXXX_rec_<key>` naming as real drinks, so `parse_available_beverages`
  added them to `coordinator.beverages` and the button platform created
  phantom brew buttons for them. Pressing one would fire `brew_beverage`
  against a template payload — undefined firmware behaviour (silent
  reject, off-recipe brew, or service mode entry). Side effect: removes
  the recurring `Unknown beverage keys [...]` log warning several testers
  (Bastien on Eletta Explore, @JanKraslice on ECAM610.74) hit on every
  full refresh. New `const.TEMPLATE_BEVERAGE_KEYS` exposes the canonical
  set so other surfaces (diagnostics, button platform warnings) can
  reuse it without duplicating the list. Filter is exact-match — a real
  drink starting with `default` (none today, hedging) is not falsely
  excluded. Locked by 7 regression tests in
  `TestFirmwareTemplateKeysFiltered`, covering Eletta `dXXX_rec_<P>_<K>`,
  PrimaDonna Soul `dXXX_<P>_rec_<K>` and the profile-less `dXXX_rec_<K>`
  layouts.

### Internals

- `cryptography` minimum bumped from `>=47.0.0` to `>=48.0.0`
  (Dependabot #26, automated tests green).

## [1.6.0-beta.14] — 2026-05-01

### Fixed

- **Profile sensor & select reported the wrong active profile**.
  `sensor.<dsn>_active_profile` previously preferred the monitor `profile`
  byte (60s polling) over the cloud `active_profile` property (10min
  refresh). The monitor byte can lag the user's selection because it
  tracks the *last brewed* profile, not the *currently selected* one;
  observed on Bastien's machine 2026-05-01 with `state="sk7n4k3d"` while
  `active_profile_id=3` ("fuck claud") was the actual selection on the
  machine UI. Both the sensor and `select.<dsn>_profile` now resolve the
  active profile id with priority `active_profile` (cloud, authoritative)
  → `profile` (monitor, fallback) → `1` (default), and the sensor
  exposes `monitor_profile_id` as a separate attribute so support reports
  can detect drift between the two sources.

- **UTF-16 decoder leaked adjacent struct fields into name strings**.
  `_decode_utf16` stripped null bytes globally instead of stopping at the
  first 2-byte-aligned NUL terminator, so fixed-width name slots
  containing `name1\\x00\\x00name2…` decoded as `name1name2…`. Bastien's
  bean profile names landed as `'PrédéfiniDémarrer\\x01Ā'`,
  `'Grains 1ఁ̀ā'`, `'Grains café 6Φλιτζά'` (Greek leaking into a French
  name). The decoder now truncates at the first aligned NUL pair before
  decoding and strips C0 control characters that leak from length-prefix
  bytes between fields. Locked by 7 regression tests in
  `TestDecodeUTF16FixedWidthBuffer`, including the exact pattern observed
  on the machine.

- **Brew button presses left automations blind during the cycle**.
  The 60s default poll interval missed every transient `machine_state`
  value (Pre-brewing, Brewing, Frothing milk, Rinsing, Steaming) because
  each lasts ~10-30s. The coordinator now exposes
  `request_fast_poll(duration_s, interval_s)` and `DeLonghiBrewButton`
  invokes it (`90s` window @ `5s` interval) immediately after sending the
  brew command, so watchers actually see the state flow. The fast-poll
  window expires automatically on the next cycle past its deadline,
  reverting to 60s. 5 tests in `TestFastPollWindow`.

- **`DeLonghiPowerSwitch._retry_task` polluted HA stage 2 boot**.
  `hass.async_create_task(self._retry_power_on())` registered the
  background sleeper in HA's "wait-for-this-to-finish" set, producing
  `Setup timed out for stage 2 waiting on …_retry_power_on()` warnings
  on every restart. Switched to `async_create_background_task` with the
  explicit name `delonghi_power_retry_<dsn>` so HA recognises it as a
  long-running background task and stops blocking boot on it.

## [1.6.0-beta.13] — 2026-05-01

### Fixed
- **Blocking alarms sensor — translation key rendered as `_3` suffix**.
  In beta.12 the new `DeLonghiBlockingAlarmsSensor` shipped with
  `_attr_translation_key = "blocking_alarms"` but no translation entry
  in any of the 16 language files, so HA fell back to the device name
  ("De'Longhi Eletta Explore") for friendly_name and produced
  `sensor.de_longhi_eletta_explore_3` because the device name was
  already taken. The 16 translation files now all carry an explicit
  `blocking_alarms.name` key, giving the sensor a stable
  `sensor.<dsn>_blocking_alarms` entity_id and a localized friendly
  name (en: Blocking Alarms, fr: Alarmes bloquantes, de: Blockierende
  Alarme, es: Alarmas bloqueantes, it: Allarmi bloccanti, nl: Blokkerende
  alarmen, pt: Alarmes de bloqueio, ru: Блокирующие тревоги, ja:
  ブロッキングアラーム, ko: 차단 알람, zh-Hans: 阻止性警报, pl: Alarmy
  blokujące, sv: Blockerande larm, da: Blokerende alarmer, nb:
  Blokkerende alarmer, uk: Блокувальні тривоги).

## [1.6.0-beta.12] — 2026-05-01

### Added
- **Blocking alarms surfaced — UX visibility for "machine stuck Turning On"**.
  When a blocking alarm is active (Water Tank Empty, Hydraulic Problem,
  Drip Tray Missing, Heater/Steamer Probe Failure, Infuser Motor Failure,
  Bean Hopper Absent, Grid Missing, Water Tank Missing, Grounds Container
  Full, Machine Service Required), the De'Longhi firmware accepts the
  POWER_ON sequence but never reaches Ready — the machine sits in
  `Turning On` indefinitely with no visible reason. Two complementary
  surfaces now make the cause obvious:
  - New `sensor.<dsn>_blocking_alarms` aggregator. State = number of
    blocking alarms currently active. Attributes:
    `blocking_alarms` (list of names), `blocking_bits` (raw alarm bits,
    useful for automations), `is_blocking_power_on` (bool),
    `all_active_alarms` (every active alarm, blocking or not).
  - `async_turn_on` now logs a WARNING with the explicit alarm names
    *and* schedules a `persistent_notification` in the HA UI so the
    user sees the issue in their notification tray instead of buried in
    logs. Power-on still proceeds (some alarm states clear themselves
    once the wake sequence runs); the announcement is purely
    informational and never aborts the command.
- `BLOCKING_ALARM_BITS: frozenset[int]` constant in `const.py`, derived
  once from the `blocking: True` flag now carried by each entry of
  `ALARMS`. Advisory alarms (Descale Needed, Cleaning Needed, Replace
  Water Filter, Coffee Beans Empty…) are *not* in the blocking set —
  the machine still operates with those active.
- 10 regression tests:
  `TestBlockingAlarmsSensor::test_zero_when_no_alarms`,
  `…test_zero_when_only_non_blocking_alarms`,
  `…test_counts_blocking_alarms`,
  `…test_handles_missing_alarms_key`,
  `…test_handles_alarm_without_name_falls_back_to_bit`,
  `TestBlockingAlarmsAnnouncement::test_no_blocking_alarms_is_silent`,
  `…test_only_advisory_alarms_is_silent`,
  `…test_water_tank_empty_warns_and_notifies`,
  `…test_multiple_blocking_alarms_listed`,
  `…test_announce_never_aborts_turn_on`. Suite total: 862 passed.

## [1.6.0-beta.11] — 2026-05-01

### Fixed
- **Power switch — turn_off cancels pending power-on retry, retry guards on
  user intent**. Sequence reproduced 2026-05-01 11:18→11:21 in HA log: a quick
  `turn_on` followed ~4s later by `turn_off` left the background `_retry_task`
  scheduled by `turn_on` armed. Three minutes after the original `turn_on`,
  the retry observed `machine_state=Off` (because `turn_off` had since
  succeeded), classified the original wake as failed, and re-sent
  `POWER_ON_CMD`. The machine, dutifully, switched back on against the user's
  explicit intent. Two complementary guards now prevent the regression:
  - `async_turn_off` cancels `_retry_task` if pending — the user just changed
    their mind, no point in racing them.
  - `_retry_power_on` aborts early when `_last_commanded_on is not True` — if
    a `turn_off` slipped through between the `turn_on` and the retry firing,
    the desired state is Off, not "wake again".
  Locked by `test_turn_off_cancels_pending_power_on_retry`,
  `test_turn_off_handles_no_pending_retry`,
  `test_retry_aborts_after_user_turned_off`,
  `test_retry_proceeds_when_last_commanded_still_on` (4 tests, all green).

## [1.6.0-beta.10] — 2026-04-29

### Fixed
- **PrimaDonna Soul "millcore" handshake — accept non-wallclock `time_1`**
  (#10). Reported by @dalodzik on `AC000W040821014`: the LAN handshake was
  rejected with `clock_skew` because the firmware ships `time_1` as a
  free-running monotonic counter (uptime nanoseconds, ≈1.4×10¹⁴ after a
  couple of days), not as UNIX seconds. The skew check now applies only
  when `time_1` looks like a wall-clock value (< 5×10⁹, year ≈2128); above
  that threshold the value is treated as a monotonic counter and the
  ratchet on `_last_handshake_time1` is the sole anti-replay control.
  Cremalink-style firmwares (Eletta Explore, PrimaDonna early FW) keep
  the original wall-clock guarantees. Locks the exact lodzen value with
  `test_handshake_accepts_primadonna_soul_uptime_time1`,
  `test_handshake_uptime_time1_still_enforces_anti_replay` and
  `test_handshake_still_rejects_wallclock_skew_replay`. Without this fix
  every PD Soul session fell back to the cloud, which never wakes the
  machine — power on/off and brewing all silently failed.
- **PrimaDonna Soul "millcore" model recognition** (#10). Decoder for
  `d270_serialnumber` already handled the binary envelope correctly
  (PR #19), but `_detect_contentstack_pattern` only scanned for
  `ECAM\d+` in the raw base64 string and didn't know about
  `DL-millcore`. The detector now consumes the decoded SKU through a
  new `SKU_TO_ECAM_PATTERN` map (currently `217055 → ECAM61075`), and
  the OEM map gained `DL-pd-soul`, `DL-pd-soul-better`, `DL-millcore`
  → `ECAM61075`. The user no longer sees `ContentStack: cannot
  determine ECAM model`; the downstream short-circuit on unindexed
  families still applies, so we don't spam HTTP for ECAM61.

### Security / CI
- **Release pipeline — verify tag matches manifest version** (#10).
  `release.yaml` now runs two cross-checks before publishing:
  the `manifest.json` at the tagged commit must declare the same
  version as the tag (`vX.Y.Z`), and the produced zip artefact must
  carry that exact manifest. A stale checkout, a forgotten bump, or a
  mistyped tag now fails the workflow loudly instead of shipping a
  release whose code doesn't match its name. Direct response to
  @dalodzik's "how do you test that your release commit is matching the
  expected release tag?".

### `delonghi_daedalus` 0.2.0 — security & diagnostics
This component is shipped from the same repository but is not bundled in the
HACS zip; install via manual clone until a separate HACS release pipeline is
ready.

#### Security
- **Drop password from entry data + add reauth flow** — the user's password
  is no longer persisted in `.storage/core.config_entries`. Runtime auth uses
  the long-lived Gigya session token (LAN AUTH frame uses the JWT). When the
  session token is revoked the coordinator now raises
  `ConfigEntryAuthFailed`, triggering HA's native reauth notification — the
  user is asked for the password only at that point.
- **LAN host validation** — `connect_lan` now rejects any host that isn't a
  numeric IP in a private / loopback / link-local range. The Daedalus
  firmware presents a self-signed cert, so the integration uses
  `verify_mode=CERT_NONE`. Without this guard, a public IP or hostname would
  let any on-path attacker MITM the WebSocket handshake and steal the JWT
  carried in the AUTH frame.

#### Added
- **Diagnostics endpoint** — HA-native "Download diagnostics" with secret
  scrubbing (jwt, session_token, session_secret, AuthToken, apiKey, host,
  serial_number, …). Safe to upload to a public GitHub issue.
- **Auto-probe Gigya pools on 403005** — already merged in PR #22, now
  locked by 7 unit tests covering preferred-pool / fallback / all-fail /
  short-circuit / ordering / log line.

#### Internal
- Sensors marked `EntityCategory.DIAGNOSTIC` to keep wiring-state out of
  default dashboards.
- Dropped dead `JWT_REFRESH_THRESHOLD_SECONDS` constant (refresh has always
  been reactive on auth failure, never proactive).
- 41/41 daedalus tests passing.

## [1.6.0-beta.9] — 2026-04-29

### Added
- **`delonghi_daedalus` companion integration** — separate component scaffolded for "My Coffee Lounge" / Eletta Ultra machines (package `com.delonghigroup.daedalus`). Independent config flow + auth path; the legacy `delonghi_coffee` component is unchanged for Coffee Link / Ayla machines. (#20)
- **Gigya pool selector** — daedalus config flow exposes the three documented Gigya pools (`EU`, `EU_US`, `CN`) so users on regional accounts can pick the right one explicitly instead of hard-failing on `403005`. Detailed auth/connect logging now surfaces the exact `errorCode` and `errorMessage` from Gigya in the HA log. (#21)
- **9 static beverages** — `drip_style`, `caffe_crema`, `red_eye`, `black_eye`, `espresso_intenso`, `ristretto_napoletano`, `babyccino`, `milchkaffee`, `koffie_verkeerd` — registered with localized labels in every shipped locale.

### Fixed
- **Custom slot wire-key mismatch** — custom drinks were registered under `custom_bev_1..6` while the wire format reports them as `custom_1..6`. The integration now uses the wire keys (matching `coordinator.custom_recipe_names`), eliminating the recurring `Unknown beverage keys — buttons created with default name/icon: ['custom_1', ...]` warning. The brew button also now falls back to the user's custom name when set, so e.g. a slot renamed "Booster" wins over the localized "Custom Drink 1" label. Locks the contract with `TestCustomSlotsUseWireKey` so it can't regress.
- **Binary-encoded device serial** — `api.py` now decodes base64-encoded binary serials reported by PrimaDonna Soul and other modern firmwares, instead of treating them as opaque strings. (#19)
- **Coordinator unexpected-exception path** — log the exception before re-raising as `UpdateFailed` so the underlying error is visible in HA logs instead of being silently wrapped.

### Internals
- **Timing constants extracted** — `MONITOR_STALE_TIMEOUT`, `POWER_WAKE_DELAY`, `POWER_RETRY_DELAY`, `POWER_STALE_THRESHOLD` moved to `const.py` (legacy `_WAKE_DELAY` etc. kept as aliases so private callers still resolve).
- **BLE001 rationale comments** added on the broad `except` in `config_flow`.
- **812 tests** passing (was 769 at beta.8), ruff clean.

## [1.6.0-beta.8] — 2026-04-17

### Security
- **LAN server hardening** — bind defaults to `127.0.0.1` instead of `0.0.0.0`, peer allowlist enforced on every handler (handshake, command poll, property push, status), clock-skew guard rejects handshakes more than 30 min from wall-clock, anti-replay on `time_1` blocks attackers from rolling the device back with a captured handshake. Coordinator wires the actual LAN IP into `allowed_peers` automatically.
- **Canonicalize base64 random values** — `_handle_handshake` and `run_lan_diagnostic` now strip trailing `=` padding on received `random_1` / `random_2`, so a non-conforming peer can't make the two sides derive different session material.

### Added
- **Keepalive backoff escalation** — coordinator escalates WARNING → ERROR after 3 consecutive ping failures and fires ERROR every 5 subsequent attempts, with an INFO line on recovery. Stops quiet degradation going unnoticed in logs.
- **LAN diagnostic hard timeout** — `run_lan_diagnostic` now wraps the whole pipeline in a 30 s `asyncio.timeout`, reports `teardown_ok` / `teardown_error` in the result details, and uses `asyncio.shield` for server teardown so cancellation doesn't leak a runner.

### Fixed
- **Button availability (`brew`, `sync_recipes`)** — now also gate on machine state `Off` / `Sleep`. Pressing a button while the machine was asleep used to silently drop the command.
- **Service registration** — HA services are now registered once per HA instance (not once per entry) via a `hass.services.has_service(...)` guard. Handlers resolve `(api, coord, dsn)` lazily from `call.data["config_entry_id"]`, so multiple coffee machines no longer fight over the same handler closure.
- **Diagnostic button except** — narrowed from bare `Exception` to `(DeLonghiApiError, DeLonghiAuthError, requests.RequestException, TimeoutError, ValueError, KeyError)`; programmer errors (AttributeError, TypeError) now crash loudly instead of being silently wrapped as "cloud fetch failed".

### Internals
- **46 new tests** — `tests/test_config_flow.py` (12 cases: user step happy/error branches, reauth, options flow), `tests/test_diagnostics.py` (7 cases: every `REDACT_KEYS` entry scrubbed, triage fields surface, missing entry tolerated), `tests/test_button.py` (+19 cases covering availability + dispatch + error mapping on all four button types), `tests/test_select.py` (11 cases covering options / current_option / select_option), plus canonicalization regression in `tests/test_lan.py`. Suite total 466, ruff clean.

## [1.6.0-beta.7] — 2026-04-17

### Fixed
- **LAN concurrency** — `_handle_handshake` used to swap `self._session` outside the lock while a concurrent `_handle_command_poll` could already have read the old pointer and bumped `self._seq` against the new counter, producing desynced IV chains the device silently rejected. Both paths now take the same lock when touching session/seq; derive/encrypt stay off-lock. Regression tests hammer handlers with `asyncio.gather` to keep the invariant enforced.

### Notes
- `v1.6.0-beta.6` was cut from `master` before the fix landed — prerelease channel users should skip straight to beta.7.

## [1.6.0-beta.6] — 2026-04-14

### Added
- **Diagnostics download** — HA-native "Download diagnostics" for faster bug triage (secrets scrubbed).

### Fixed
- **Manifest** — declare `cryptography>=41.0.0` requirement now that LAN crypto is wired.
- **Services** — `write_bean_profile` profile index bounds aligned with API (1-5, not 1-4).
- **API token refresh** — thread-safe; races could previously submit a stale token once per reload.
- **Button listener leak** — coordinator subscription is now cleaned up across entry reload.
- **Release workflow** — SemVer-strict prerelease detection (`vX.Y.Z-<tag>`), stops promoting beta tags to stable.

### Changed
- **FR translations** — missing options entries added.
- **Coverage** — stop hiding 8 files from coverage; expose the real number.

## [1.6.0-beta.5] — 2026-04-11

### Added
- **LAN Phase 2** — cremalink-compatible crypto (AES-256-CBC + HMAC-SHA256), IV chaining, embedded `aiohttp` server on port 10280, endpoints `/local_reg.json`, `/local_lan/key_exchange.json`, `/local_lan/commands.json`, `/local_lan/property/datapoint.json`. Observe-mode wiring into coordinator behind an opt-in flag.
- **LAN diagnostic button** — on-demand handshake + round-trip check with dedicated debug logger namespace.

### Fixed
- **Counters** — PrimaDonna Soul integer layout `d733..d748` now decoded correctly (fixes #3).
- **LAN config keys** — honor `lan_enabled` / `lanip_key` / `lan_ip` from options as documented.

## [1.5.x] and earlier

See the [GitHub releases](https://github.com/sk7n4k3d/delonghi-ha/releases) page.

[Unreleased]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.9...HEAD
[1.6.0-beta.9]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.8...v1.6.0-beta.9
[1.6.0-beta.8]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.7...v1.6.0-beta.8
[1.6.0-beta.7]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.6...v1.6.0-beta.7
[1.6.0-beta.6]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.5...v1.6.0-beta.6
[1.6.0-beta.5]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.5.10...v1.6.0-beta.5
