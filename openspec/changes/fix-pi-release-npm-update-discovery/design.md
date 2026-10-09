## Context

The inventory intentionally models Pi with `PiReleaseSource`: its npm package identifies the version-discovery namespace, while its GitHub repository and tag prefix define immutable installation assets. Provider dispatch selects the npm provider from `NpmUpdate`, but that provider currently accepts only `NpmSource` and therefore rejects the otherwise valid Pi pairing already enforced by inventory validation.

## Goals / Non-Goals

**Goals:**

- Make the npm provider consume package identity from both supported npm-discoverable source models.
- Preserve strict rejection of source models that do not define an npm package identity.
- Cover both provider behavior and the Pi target's user-visible update classification.

**Non-Goals:**

- Changing Pi installation or materialization to use npm tarballs.
- Changing the inventory schema, provider selection, suggestion format, or release-asset validation.
- Introducing a separate Pi update provider.

## Decisions

### Accept the two explicit npm-discoverable source types

The npm provider will accept `NpmSource` and `PiReleaseSource`, then use their common `package` field for the existing registry request and candidate selection path. An explicit union keeps the compatibility boundary narrow and makes unrelated source types continue to produce the existing skipped diagnostic.

A structural `hasattr(source, "package")` check was rejected because it would silently admit future source models without a deliberate compatibility decision. Converting Pi to `NpmSource` was rejected because that would discard release repository metadata required by the authoritative installation workflow.

### Reuse the existing npm discovery pipeline unchanged

After source admission, Pi will follow the same stable filtering, publication-time extraction, semantic comparison, and result construction as ordinary npm packages. Pi-specific release assets remain outside update discovery and continue to be derived and verified during materialization.

Creating a dedicated Pi provider was rejected as duplication: discovery uses the same npm endpoint and policy, while the special behavior belongs to installation rather than version selection.

### Test the compatibility seam at two levels

Provider tests will prove that a `PiReleaseSource` queries the encoded npm package and yields a candidate, while an unrelated source remains rejected. Update orchestration or CLI-facing tests will prove that the canonical Pi target is `outdated` and applicable, not `skipped`, and that suggestion rendering retains its `pi-release` metadata.

## Risks / Trade-offs

- [Future npm-discoverable source types still require an explicit code update] → This is intentional to preserve closed typed compatibility.
- [A discovered npm version may precede publication of matching GitHub installation assets] → Existing Pi materialization validation remains authoritative; this change does not claim or verify release-asset availability during npm discovery.

## Migration Plan

No configuration migration is required. Deploy the provider compatibility change with regression tests; rollback is a direct code revert and leaves existing inventories valid.
