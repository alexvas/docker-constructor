## Why

The aggregate contract for the host-side `docker-constructor.local.toml` companion currently lives in `runtime-host-access` even though it composes host-access, cache, and corporate-network concerns and is never itself a runtime input. Moving that contract behind a dedicated capability will clarify ownership without losing cache-path safety, fallback, isolation, or no-follow guarantees and will provide a stable prerequisite for later local configuration tables.

## What Changes

- Introduce `configuration-document-validation` as the shared TOML parsing, syntax-failure projection, document identity, and pre-effect validation boundary for both `docker-constructor.toml` and `docker-constructor.local.toml`.
- Introduce `local-project-configuration` as the owner of companion resolution, closed composition of the existing `[host-access]`, `[cache]`, `[corporate-trust]`, and `[network.proxy]` tables, domain validation, reviewed/local isolation, and host-only confinement.
- Keep each table's fields and behavior with its domain capability; the aggregate owner composes those closed schemas rather than redefining them.
- Remove aggregate companion ownership and cache behavior from `runtime-host-access`, retaining only local `[host-access]` consumption and the existing minimal derived launch values.
- Move every cache-root clause formerly embedded in `runtime-host-access` to cache-owning capabilities, including absolute-path validation, lexical normalization, dangerous root rejection, no-follow symlink inspection, default fallback, permissions, and failure-before-effects behavior.
- Preserve corporate trust and proxy independence from host-access policy.
- Make no user-visible configuration or runtime behavior change and add no new local table; this is an ownership-preserving prerequisite refactor.

## Capabilities

### New Capabilities

- `configuration-document-validation`: Define consistent TOML parsing, typed field-or-syntax errors that exclude raw parser/source data, caller-owned presentation with defense-in-depth redaction, and validation-before-effects for both fixed project configuration documents.
- `local-project-configuration`: Define the single host-only machine-local companion, its fixed resolution, closed domain-table composition, domain validation, reviewed-state isolation, and prohibition on exposing the aggregate file to build or runtime containers.

### Modified Capabilities

- `runtime-host-access`: Remove ownership of the aggregate companion and retain only domain-owned `[host-access]` state consumption and authorized derived runtime launch values.
- `user-cache-storage`: Consolidate the complete cache-root validation and safety contract, including lexical normalization, dangerous-root checks, no-follow inspection, and default-root behavior.
- `docker-build-reproducibility`: Route reviewed inventory through the shared configuration-document boundary and narrow its cache configuration requirement to reviewed/local source separation and projection exclusion while relying on `user-cache-storage` for root resolution and safety.

## Impact

- Affects shared reviewed/local TOML ingestion and diagnostics, local configuration model/parser ownership, cache-root resolution and validation boundaries, host-access planning inputs, and their regression tests.
- Does not change accepted configuration, defaults, Docker arguments, effective projections, cache locations, network behavior, or container-visible values.
- `docker-constructor.local.toml` remains a host-side input and is never copied, mounted, published, or exposed as an aggregate to build or runtime containers.
- `improve-host-build-observability` will depend on this change being implemented, synchronized, and archived before it adds the new local `[output]` table.
