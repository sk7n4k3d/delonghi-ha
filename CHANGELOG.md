# Changelog

All notable changes to this integration are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow [SemVer](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.7...HEAD
[1.6.0-beta.7]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.6...v1.6.0-beta.7
[1.6.0-beta.6]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.6.0-beta.5...v1.6.0-beta.6
[1.6.0-beta.5]: https://github.com/sk7n4k3d/delonghi-ha/compare/v1.5.10...v1.6.0-beta.5
