## ADDED Requirements

### Requirement: Expose authoritative Pi release asset acquisition activity
Host-side acquisition of the authoritative Pi release metadata SHALL expose presentation-neutral start, success, and failure activity separately for the closed logical assets `SHA256SUMS`, `pi-coding-agent-install-package.json`, and `pi-coding-agent-install-package-lock.json`. Whenever the existing acquisition boundary yields body chunks without an additional request, buffering, probe, or semantic change, each yielded chunk SHALL update that asset's cumulative received-byte progress and latest transport activity for the next heartbeat. Byte progress MAY be omitted only for a compatible transport that cannot expose chunk observation without changing its semantics. Each asset failure SHALL identify only its safe logical asset name and SHALL apply the shared safe network diagnostic policy without exposing a release URL path, query, user information, proxy detail, credential, or exception message.

This observability SHALL NOT change authoritative release-base URL derivation, required asset names, request effects, acquisition or checksum-verification order, checksum interpretation, atomic publication, partial-download cleanup, or the requirement that both installation files match their entries in the acquired `SHA256SUMS` before use. Acquisition SHALL NOT require content length and SHALL NOT infer a percentage, transfer rate, or total size when unavailable.

#### Scenario: Authoritative Pi assets expose distinct activity
- **WHEN** Constructor acquires `SHA256SUMS`, `pi-coding-agent-install-package.json`, and `pi-coding-agent-install-package-lock.json` for a reviewed Pi release
- **THEN** each acquisition SHALL expose start and terminal activity under its distinct closed logical asset name
- **AND** activity SHALL preserve the authoritative acquisition and checksum-verification order
- **AND** observability SHALL add no request, eager buffering, or duplicate acquisition

#### Scenario: Streaming Pi asset acquisition exposes received bytes
- **WHEN** an authoritative Pi asset acquisition yields body chunks through the existing streaming boundary
- **THEN** each yielded chunk SHALL update that asset's cumulative received bytes and latest transport activity for the next heartbeat
- **AND** progress SHALL identify the corresponding closed logical asset
- **AND** SHALL NOT require content length or claim a percentage, transfer rate, or total size

#### Scenario: Compatible Pi transport cannot expose chunks
- **WHEN** an injected compatible transport cannot expose chunk observation without an additional request, buffering, probe, or changed transport semantics
- **THEN** acquisition SHALL continue without byte-progress events
- **AND** SHALL still expose distinct logical start and terminal activity for each acquired Pi asset
- **AND** authoritative acquisition and verification behavior SHALL remain unchanged

#### Scenario: Authoritative Pi asset acquisition fails
- **WHEN** transport fails while acquiring one of the three authoritative Pi release assets
- **THEN** failure activity SHALL identify that asset by its closed logical name
- **AND** SHALL apply the shared safe network diagnostic policy
- **AND** SHALL expose no release URL path, query, user information, proxy detail, credential, or exception message
- **AND** existing partial-download cleanup and prohibition on using unverified installation files SHALL remain unchanged
