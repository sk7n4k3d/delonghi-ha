# Changelog

All notable changes to this integration are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [SemVer](https://semver.org/).

## [Unreleased]

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
