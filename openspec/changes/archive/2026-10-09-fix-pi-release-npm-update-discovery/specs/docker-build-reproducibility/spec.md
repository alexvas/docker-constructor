## MODIFIED Requirements

### Requirement: Discover dependency updates explicitly
The version helper SHALL provide an explicit best-effort `check-updates` operation, including the dedicated `uv-python` provider for uv-managed CPython. The reviewed Pi entry SHALL use its npm package identity for version discovery while retaining its dedicated Pi release source metadata for authoritative installation assets. Normal builds, launches, validation, and runtime setup SHALL NOT invoke update-provider APIs. Text-mode reporting and interactive progress SHALL conform to the `update-check-reporting` capability rather than printing a serialized Python data structure. Discovery, applicability, suggestions, structured results, and exit policy remain owned by this capability.

#### Scenario: Checking for stable updates
- **WHEN** `./docker/docker-constructor.py check-updates` runs
- **THEN** it SHALL query each configured provider for stable candidates
- **AND** SHALL report current, outdated, skipped, unavailable, or incomplete status per dependency
- **AND** its text output SHALL summarize result statuses and present every dependency according to the selected compact or detailed report defined by `update-check-reporting`
- **AND** default execution SHALL not fail solely because an update exists or a provider is unavailable

#### Scenario: Discovering a Pi update through npm metadata
- **WHEN** the reviewed Pi entry has a dedicated Pi release source, an npm package identity, and an npm update provider
- **AND** npm registry metadata contains a newer stable package version
- **THEN** `check-updates` SHALL report that Pi version as an outdated candidate rather than skipping it for source-type incompatibility
- **AND** SHALL retain the reviewed Pi release repository and tag-prefix metadata used for authoritative installation assets and replacement suggestions

#### Scenario: Requesting detailed diagnostic output
- **WHEN** `./docker/docker-constructor.py check-updates --details` runs in text mode
- **THEN** it SHALL select the detailed text report defined by `update-check-reporting`
- **AND** the option SHALL affect presentation only and SHALL NOT change discovery, classification, suggestions, structured results, result ordering, or exit policies

#### Scenario: Reporting publication time
- **WHEN** the selected candidate has an authoritative release/version publication time from its provider
- **THEN** text presentation SHALL format it according to the selected report defined by `update-check-reporting`
- **AND** the structured JSON result SHALL retain the complete value as an additive optional field when it uses the supported UTC RFC 3339 profile `YYYY-MM-DDTHH:MM:SS[.fraction](Z|+00:00)`, where `.fraction`, when present, contains one through six decimal digits
- **AND** text presentation SHALL show `-` when the provider has no authoritative release/version publication time or supplies a malformed or unsupported timestamp; lowercase `t`/`z` forms and leap seconds are unsupported
- **AND** SHALL NOT infer publication time from response, cache, Git commit, or later supplemental artifact-upload timestamps

#### Scenario: Checking release applicability
- **WHEN** a provider reports a newer prebuilt release
- **THEN** the helper SHALL verify required architecture assets and checksum metadata are available before marking it directly applicable

#### Scenario: Distinguishing a base digest refresh
- **WHEN** the selected Docker base tag resolves to a different manifest digest
- **THEN** the helper SHALL report a digest refresh separately from a major or channel upgrade
