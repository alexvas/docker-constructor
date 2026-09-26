## Context

See `proposal.md` for motivation. The current collector performs secret redaction and URL projection before either live structured events or retained tails exist. It preserves normalized hostnames and ephemeral URL fingerprints but intentionally discards paths and source URL text. The facade later selects interactive/lines presentation and owns grouping. Consequently, `host-path` and `exact` cannot be implemented solely in the renderer, while exposing source diagnostics through the existing external SDK DTO would unintentionally broaden that API.

The archived `improve-host-build-observability` change established a single presentation actor, bounded ordered admission, independently bounded retained diagnostics, exact-repeat/numeric grouping, terminal/failure ownership, and best-effort live delivery. This change extends those boundaries rather than replacing their concurrency model.

## Goals / Non-Goals

**Goals:**

- Carry only the minimum bounded representations needed to render all three local modes and their failure tails.
- Preserve immediate mutable interactive feedback for redacted npm fetch aggregation.
- Keep terminal control neutralization mandatory even when content redaction is disabled.
- Keep external SDK events and evidence on the existing safe URL-free contract.
- Keep display policy out of assembler and assembled-output identities.

**Non-Goals:**

- Treating `exact` as a confidentiality boundary or detecting every possible secret.
- Reconstructing dropped request multiplicity or promising source-level request counts under mailbox saturation.
- Parsing arbitrary third-party HTTP log grammars beyond the accepted npm fetch form.
- Changing Docker/BuildKit native output, npm logging level, network behavior, cache behavior, or assembly deadlines.
- Adding a hostname-only compatibility mode or accepting `show_network_hosts`.

## Decisions

### Use one closed display enum and reject the old field

The local output model will use a closed `NetworkUrlDisplay` value with `redacted`, `host-path`, and `exact`. `redacted` is the absence default for local configuration and for direct assembler, SDK, or injected callers that omit the selector. The aggregate local schema admits only the declared `[output]` fields and rejects every other field through ordinary closed-schema validation. A closed enum avoids contradictory booleans and makes exact disclosure explicit.

Alternative: preserve `show_network_hosts` as an alias. Rejected because compatibility was explicitly excluded and the boolean cannot express the new policy.

### Propagate one presentation-only selector to collection

The facade resolves `network_url_display` before host materialization or assembly starts. The same `NetworkUrlDisplay` enum field, never a string or boolean surrogate, follows this path:

```text
local config parsing
  -> facade resolution
  -> host materialization request
  -> Pi assembly request
  -> assembler execution request
  -> stream collector configuration
```

The selector is a non-semantic host-side presentation input. It is permitted in these host-side request DTOs only so the assembler can select live diagnostics and exactly one retained representation before consuming stdout/stderr. Semantic assembler inputs describe what is executed or produced, including lockfile and dependency roots, image/tool versions, platform, and other assembly content. `network_url_display` is not a semantic assembler input and SHALL be excluded from `AssemblerIdentity`, `AssemblerInputIdentity`, assembled-output identity, cache keys and equality, verification and assembler evidence, and publication decisions.

Changing only this selector cannot change npm or Docker argv, environment or network behavior, timeouts or deadlines, lifecycle-event production, verification, publication, cache lookup or reuse, cleanup, or the primary assembly result. Request propagation therefore carries presentation intent without making it part of assembly semantics. Direct assembler, SDK, and injected entry points default an omitted selector to `redacted` and validate supplied values against the closed enum before stream collection.

Alternative: keep the selector only in facade formatting. Rejected because mode-selected collection and retention must be configured before assembler stream capture. Treating it as a semantic assembly input is also rejected because presentation must not invalidate or distinguish equivalent assembled outputs.

### Retain one mode-selected representation

The resolved local policy is fixed before collection starts. After incremental UTF-8 decoding, line bounding, and terminal-control neutralization, collection will retain exactly one bounded per-stream tail:

```text
npm bytes
   |
   v
incremental decode + line bound + terminal-control neutralizer
   |
   +--> source-safe line ---------------------------> exact live + retained tail
   |
   +--> secret redaction + URL projector
            |                         |
            |                         +-------------> URL-free external SDK event
            |
            +--> redacted render -------------------> redacted live + retained tail
            |
            +--> safe host/path render -------------> host-path live + retained tail
```

Only the branch selected by `network_url_display` enters the single retained-tail buffer and internal presentation actor. Non-selected local representations are not retained. In `exact`, the projected-safe form is still computed transiently when required for external SDK delivery, but it is not placed in another retained tail. In `redacted` and `host-path`, source-safe content is not retained. "Source-safe" means content-exact except for terminal-control neutralization and existing overflow markers; it is not secret-safe. The projector remains the only source for external SDK diagnostics, fingerprints, safe host/path facts, and evidence-safe data.

Alternative: retain source-safe and projected-safe tails simultaneously, or add a third host-path tail. Rejected because the immutable policy is known before collection, parallel retention duplicates byte accounting and memory, and retaining non-selected exact content would unnecessarily preserve credentials and secrets. Passing raw text through the existing diagnostic DTO is also rejected because it would expose source URLs to external callbacks and make sink behavior part of the security boundary.

### Carry a dedicated safe host/path presentation fact

The projector will derive a bounded presentation value containing normalized hostname and canonical safe encoded path. It excludes scheme, userinfo, explicit port, query, and fragment. Percent-encoded control/ambiguous content is not decoded into active text. If path safety cannot be established within the existing token bound, the value uses a fixed `<redacted-path>` marker. This fact is local-presentation metadata, not evidence or public SDK output.

Alternative: reconstruct the path from the URL fingerprint. Rejected because fingerprints are intentionally one-way and session-ephemeral.

### Select representations before facade formatting without changing execution facts

The facade-owned selector passed through the host-side request chain selects which bounded diagnostic representation enters the internal presentation actor and the single retained tail used by text/JSON failure formatting. The assembler fixes the selector in stream collector configuration before capture starts, and it cannot change during the operation. Domain activity observation still happens before projection and remains policy-independent. External event sinks always receive a transient projected-safe event and never the selected retained representation. The selection changes only live presentation payload and retained failure content, not npm/Docker commands, network behavior, lifecycle, heartbeat, deadline, verification, publication, cache, evidence, identity, or result semantics.

### Add a policy-aware npm fetch group

A conservative parser recognizes only complete npm 11.16.0 source lines with the full successful HTTP fetch grammar: `npm http fetch <METHOD> <STATUS> <URL> <ASCII digits>ms [attempt #<ASCII digits>] [(cache <OUTCOME>)]`. The `npm http fetch` prefix is part of the grammar and is consumed explicitly rather than being assumed to have been stripped upstream. The parser extracts method, exact 2xx status, cache outcome, projected resource identity, integer millisecond latency, and the optional attempt number. The dedicated `[0-9]+ms` field is independent of generic numeric-token boundary matching; other units are not accepted. The representative research fixture `npm http fetch GET 200 https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)` must be recognized through the real collection/projection path. Lines with unrelated or malformed prefixes, non-2xx status, retry/failure shape, or malformed fields remain ordinary diagnostics. The distinct `npm http cache ...` form also remains an ordinary diagnostic and is not in this parser's scope.

In `redacted`, the group key is method, exact status, optional attempt number or its absence, and cache outcome; URL identity and latency are omitted from both key and visible canonical text. In `host-path`, the group key additionally includes normalized hostname and safe path, so latency variants for one visible resource and attempt aggregate while different resources or attempts remain separate. Both visible canonical forms omit latency and preserve `attempt #N` when present.

The group count is the number admitted to the actor, including the first. Interactive mode writes the canonical first line into the existing mutable diagnostic slot immediately and replaces that slot at the normal at-most-one-second refresh (or sooner if the renderer already does so) with ` — N requests` as events arrive; no different event is required. Group finalization durably preserves only its latest count under existing interactive ordering. Lines mode follows the existing one-second summary window: first line immediately, counted summary on deadline or earlier boundary.

`exact` bypasses all coalescing and numeric replacement. Ordinary exact-repeat behavior remains available outside recognized fetch groups in `redacted` and `host-path`.

Alternative: generalize all numeric variants to increment counts. Rejected because it would aggregate versions, retries, progress values, and unrelated numeric diagnostics.

### Neutralize terminal controls before every selectable representation

The source-safe branch recognizes complete ANSI CSI, OSC, and DCS/control-string forms and renders controls as visible inert escapes. It also renders CR, backspace, NUL, and remaining C0/C1 terminal-affecting controls inertly; newline remains the record boundary and ordinary tabs are retained. Incomplete or oversized control sequences fail closed to a fixed inert marker under a bounded incremental state. JSON serialization then performs normal JSON escaping over the same terminal-safe retained text.

Alternative: strip controls silently. Rejected because visible escapes preserve more of the exact diagnostic and aid debugging.

### Scope persistence deliberately

The selected representation applies to live text, explicit noninteractive lines, text failure reports, and JSON `host_failure.tail`. It does not alter external SDK event payloads, verification evidence, assembler evidence, or any semantic identity. Documentation will state that exact mode can disclose credentials and secrets and that operators must control third-party output and input values.

## Risks / Trade-offs

- **[Exact mode discloses credentials and secrets]** → Require explicit local opt-in, document the disclosure prominently, keep it out of SDK/evidence, and retain terminal safety and bounds.
- **[Mode-specific retention could diverge from live rendering]** → Derive both from the same selected bounded representation and test text/JSON failure tails in every mode.
- **[npm changes its log grammar]** → Parse conservatively; unmatched lines remain visible and unaggregated.
- **[A count understates producer events after admission drops]** → Count admitted requests only and preserve existing omission notices; do not claim exact producer multiplicity.
- **[Path display leaks private package names]** → Make `host-path` explicit opt-in, exclude sensitive URL components, and fail closed for unsafe paths.
- **[Terminal escape parser misses an exotic sequence]** → Test fragmented CSI/OSC/DCS/C0/C1 forms and use fail-closed bounded incomplete-sequence handling.
- **[Breaking local configuration surprises users]** → Let ordinary closed-schema validation reject unrecognized fields and update this project's configuration and the documentation in the same change.

## Deployment Plan

1. Introduce the closed enum and change the local schema atomically with collector/presentation support.
2. Update this project's local configuration once, replacing `show_network_hosts` with `network_url_display = "host-path"`, and update the corresponding example and documentation.
3. Do not migrate, rewrite, or reinterpret any other user configuration. Unrecognized fields remain subject to ordinary closed-schema validation; selecting `redacted`, `host-path`, or `exact` elsewhere is an explicit configuration decision rather than a migration.
4. Validate all three modes before release, including failures and JSON tails. Rollback requires restoring the previous binary and this project's former local setting; no reviewed state, cache, or assembled artifact migration is required.
