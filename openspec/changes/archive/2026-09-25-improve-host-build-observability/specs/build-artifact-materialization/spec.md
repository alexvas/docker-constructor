## ADDED Requirements

### Requirement: Report safe reviewed-artifact acquisition activity
Host-side reviewed build-artifact acquisition SHALL expose presentation-neutral start, success, and failure activity associated with the artifact's safe logical name. Whenever the existing acquisition boundary yields body chunks without an additional probe, buffering, or semantic change, each yielded chunk SHALL update cumulative received-byte progress and latest transport activity for the next heartbeat. Byte progress MAY be omitted only for a compatible transport that cannot expose chunk observation without changing its semantics. Acquisition SHALL NOT require a known total size and SHALL NOT infer percentage or transfer rate when those values are unavailable.

#### Scenario: Streaming acquisition exposes received bytes
- **WHEN** a cache-miss artifact download yields body chunks through the existing streaming boundary
- **THEN** each yielded chunk SHALL update cumulative received-byte progress and latest transport activity for the next heartbeat
- **AND** SHALL identify transport progress and last byte activity without reporting diagnostic silence
- **AND** SHALL NOT require content length or claim a completion percentage

#### Scenario: Compatible transport cannot expose chunks without semantic change
- **WHEN** an injected compatible transport cannot expose chunk observation without an additional network operation, buffering, or changed transport semantics
- **THEN** acquisition SHALL continue without byte-progress events
- **AND** SHALL still report logical start and terminal activity

#### Scenario: Reviewed artifact transport fails
- **WHEN** transport fails while acquiring rustup, uv, rtk, or fd
- **THEN** the failure SHALL identify that reviewed logical artifact
- **AND** SHALL apply the shared safe network diagnostic presentation policy
- **AND** partial bytes SHALL remain subject to existing cleanup guarantees

#### Scenario: Artifact is a verified cache hit
- **WHEN** a selected artifact is safely reused from the verified cache
- **THEN** activity SHALL identify cache reuse
- **AND** SHALL NOT report network byte progress
