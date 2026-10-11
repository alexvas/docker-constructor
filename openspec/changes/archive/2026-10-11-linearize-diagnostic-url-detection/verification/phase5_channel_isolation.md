# Phase 5 — channel isolation and scope review (task 5.8)

**Scope reviewed:** the P5 diff only:

- `docker/versioning/diagnostic_coordinator.py` — records the rendered
  host/path display product at component settlement.
- `docker/versioning/diagnostic_projection.py` — exposes and drains that
  display product.
- `docker/versioning/npm_diagnostic_stream.py` — drains it per line and prefers
  it for the `host-path` fetch group key / canonical text.
- `docker/versioning/npm_fetch.py` — renders the `NpmFetchRecord` URL through
  the shared host-path projector instead of the validated fact.
- `docs/host-build-output.md` — user-visible policy text only.
- `tests/test_npm_fetch_phase4.py` and the new P5 suite — tests only.

## Channel isolation

Two independent products leave collection per finalized line and neither
displaces the other:

| Channel | Source | Content |
| --- | --- | --- |
| External SDK / structured | `StreamChunk.text`, `hostnames`, `url_fingerprints` | URL-free supported-scheme projection; masked hosts produce no hostname/fingerprint |
| Internal presentation | `StreamChunk.local_text` / `fetch_text` / `fetch_key` | mode-selected local rendering, including `<redacted-host>` / `/<redacted-path>` |
| Single retained tail | mode-selected local representation | exactly one representation, no parallel redacted/host-path/exact copy |

The new `_host_path_displays` list is in-memory, per-logical-line, drained by
`NpmDiagnosticStream._drain_retained_facts`, and cleared by
`_reset_line_facts` / `_resync_line_facts`. It is not a second tail, is never
written to the structured/SDK product, and is absent from every persistence,
evidence, identity and fingerprint path (`rg _host_path_displays docker` shows
only the coordinator, projector and collector). The existing validated
`_host_paths` fact gate is unchanged, so a secret-bearing host still supplies
neither a normalized-host fact nor a fingerprint.

## Confirmed unchanged

| Concern | Finding |
| --- | --- |
| Configuration options | No new option. `network_url_display` stays the same closed enum. No config/parse/schema file changed. |
| Persistence / stored data | No serialization, cache-key, or on-disk format touched. The display list is transient memory. |
| Assembly identity | `AssemblerIdentity` / `AssemblerInputIdentity` / output identity and cache hashing are untouched by the diff. |
| argv / environment | No npm/Docker argument or environment change; `npm_fetch` only renders an existing record. |
| Deadlines / silence | No timeout, heartbeat, or diagnostic-silence logic changed. |
| Shutdown budget | The five-second presentation budget and worker lifecycle are untouched. |
| Bounded storage | The display list holds at most the URLs settled in one logical line and is drained at every record boundary; raw payload ownership remains the projector's single deque. |
| Parser grammar | `parse_npm_fetch_line` and `NpmFetchRecognizer` are unchanged; `_rendered_host_path_text` never runs for an unrecognized fetch. |

## Masked-product correctness

`_settle_components` records `SafeHostPath(url.host, url.path)` where `url.host`
is the normalized hostname or `<redacted-host>` and `url.path` is the canonical
safe path or `/<redacted-path>`. This is the same pair the `host-path` local
text already emits, so local text, the single tail, the fetch group key and the
fetch canonical text can no longer diverge. For a secret-free resource the
display equals the validated fact; no previously eligible identity distinction
is lost (`phase5_green.log`, `phase5_affected.log`). For a masked resource two
different hidden hosts render the same key, so hidden identity cannot leak or
split a group. Unknown schemes leave the URL unchanged, `_rendered_host_path_text`
returns `None`, and the canonical text falls back to `<redacted>` with no
URL-derived group field.
