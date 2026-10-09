## Why

`check-updates` currently skips the Pi target because the npm provider rejects its intentional `PiReleaseSource`, even though Pi uses npm package metadata to discover versions and GitHub release metadata to obtain reviewed installation assets. This breaks the documented focused Pi update workflow and prevents newer Pi versions from being suggested.

## What Changes

- Allow npm version discovery for the Pi release source while retaining the dedicated `pi-release` installation contract.
- Keep ordinary npm sources and Pi release sources type-safe and reject unrelated source types.
- Add provider and end-to-end update-discovery regression coverage proving that Pi is classified normally instead of being skipped.
- Preserve the existing npm registry request, stable-version filtering, publication timestamp, reporting, and non-mutating suggestion behavior.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `docker-build-reproducibility`: Clarify that explicit update discovery for the reviewed Pi entry uses its npm package identity while preserving its authoritative GitHub release installation metadata.

## Impact

Affected code is limited primarily to `docker/versioning/providers/npm.py` and update-discovery tests. The reviewed inventory schema, Pi materialization path, CLI/JSON contracts, dependencies, and installation source remain unchanged.
