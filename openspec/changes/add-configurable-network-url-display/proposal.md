## Why

URL-free npm diagnostics currently make high-volume successful fetches look identical while still emitting every latency variant, producing distracting terminal noise without revealing which resources differ. Users need a local presentation policy that can either summarize that noise, expose safe host/path distinctions, or preserve third-party output as exactly as terminal safety permits.

## What Changes

- **BREAKING** Replace the local `[output].show_network_hosts` boolean with required-policy enum `[output].network_url_display = "redacted" | "host-path" | "exact"`; there is no compatibility alias and `redacted` remains the default.
- In `redacted` mode, recognize consecutive successful npm HTTP fetch diagnostics, remove URL and millisecond latency from their visible identity, preserve an optional `attempt #N`, and present a request-count aggregate only while the attempt number or its absence remains the same. Interactive text updates one mutable line immediately as counts grow; durable `lines` output emits the first line immediately and a counted summary at the existing fixed window or an earlier group boundary.
- In `host-path` mode, display a terminal-safe normalized hostname plus safe path without scheme, userinfo, port, query, or fragment, so distinct fetched resources remain visible; aggregate consecutive successful npm fetch lines for the same hostname/path and the same optional `attempt #N` when only millisecond latency changes, while keeping different resources or attempts in separate groups.
- In `exact` mode, preserve the complete original third-party diagnostic content, including URL components and caller-provided secrets, except that terminal control sequences remain neutralized and existing line/size bounds remain enforced; disable diagnostic aggregation.
- Resolve the selected policy in the facade before assembly and propagate the typed enum through host-side materialization and assembly requests to stream collection solely as a presentation input. Apply it consistently to interactive text, noninteractive `lines`, text failure context, and JSON `host_failure.tail`; keep external SDK diagnostics safe and URL-free, and keep execution, verification evidence, assembler/input/output identities, cache semantics, and publication unaffected.
- Document that application content redaction is a user-selected noise/presentation facility: it is mandatory within `redacted` and `host-path`, disabled by `exact`, and is not a substitute for controlling third-party output or sensitive input values. Terminal safety and output bounds remain mandatory in every mode.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `docker-build-output`: replace the existing hostname-display local output setting with the closed three-mode network URL display policy, and apply it consistently across live text, retained text failure reporting, and structured JSON failure tails while preserving structured-output isolation and safe SDK events.
- `locked-npm-environment-assembly`: accept the typed host-side presentation selector with a `redacted` default for direct callers, and define redacted successful-fetch aggregation, host/path projection, exact bounded output, terminal-control neutralization, and channel-specific retention behavior.

## Impact

Affected areas include local configuration parsing/modeling and examples; typed presentation-only propagation through host materialization and assembly requests; npm stream collection and bounded retained tails; URL projection and terminal-control handling; structured diagnostic DTOs and SDK boundaries; diagnostic identity/coalescing and presentation rendering; text and JSON failure formatting; documentation and focused configuration, propagation, projection, grouping, presentation, failure, identity, and acceptance tests. No new external dependency is expected. The setting is intentionally breaking for local companions that currently declare `show_network_hosts`.
