# Security Policy

## Reporting a vulnerability

If you find a vulnerability that affects users of this integration —
e.g. a way to leak Ayla session tokens, hijack the LAN session, or
extract credentials from a diagnostics dump — **please do not open a
public issue**.

Use one of:

- GitHub Private Vulnerability Reporting (the green "Report a
  vulnerability" button on the repo's Security tab), or
- Email **sk7n4k3d@gmail.com** with `[delonghi-ha security]` in the
  subject.

Please include:

- A description of the issue and which component is affected
  (`delonghi_coffee` and/or `delonghi_daedalus`).
- Reproduction steps or a minimal PoC if you have one.
- The integration version (`manifest.json:version`) you observed it on.
- Whether the issue is exploitable today, requires LAN access, or
  requires Ayla-cloud privileges.

## Supported versions

Only the latest tagged release on master is supported. Both stable and
prerelease (`v1.x-beta.N`) tags qualify, but security fixes will land
on master first and roll into the next prerelease.

## Out of scope

- Bugs in upstream `cremalink` / Ayla / Gigya / De'Longhi cloud — please
  report those upstream. We will document workarounds when relevant.
- Bugs in third-party HA core APIs we consume (`async_redact_data`,
  `aiohttp`, `cryptography`).
- Hardcoded Gigya / Ayla / ContentStack credentials — these are
  documented public OEM credentials extracted from the official APK
  manifest, equivalent to OAuth client IDs. They ship in every install
  of the official Coffee Link / My Coffee Lounge app.

## What you can expect

- Acknowledgement within 7 days.
- A coordinated disclosure timeline once we have triaged severity.
- Credit in the CHANGELOG and release notes if you want it (or
  anonymous if you prefer).
