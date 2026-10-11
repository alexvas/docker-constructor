# Phase 5 — consumer channels and grouping

**Status: PASS for P5 only.**  The collector's three selected representations
(structured/SDK `text`, mode-selected `local_text`, single retained `tail`) and
the `host-path` fetch group key / canonical text now agree with the revised
policy, including the masked `<redacted-host>` / `/<redacted-path>` products.
Overall progress is 63/72; P5 is 9/9.  P6 remains pending.

## RED → GREEN

`phase5_red.log` records the first focused run: 5 failed, 14 passed, 12
subtests passed, exit 1.  Four failures were the real production gap and one
was a test-helper omission:

1. `test_host_path_masks_secret_host_in_all_channels`,
   `test_secret_bearing_fetches_share_the_masked_group`,
   `test_lines_mode_uses_the_masked_group` and
   `test_secret_bearing_host_fetch_is_recognized_and_masked` showed that a
   secret-bearing *host* produced `host_path=None` and canonical
   `<redacted>` for a fetch, even though the projector's `local_text` and the
   retained tail already rendered `<redacted-host>/public`.
2. `test_secret_bearing_paths_share_the_masked_group` failed only because the
   test helper did not pass `secrets` to `fetch_group_identity` /
   `canonical_fetch_text`; correcting the helper left it passing (a
   secret-bearing *path* already masks through the validated fact).

After the helper fix the RED was exactly the four secret-bearing-host cases.
They were all the P4-surfaced gap (see `phase-4.md`): rendering and validated
facts are separate products, and the fetch display had been deriving from the
*fact* stream, which omits a masked host.  `phase5_green.log` records the
corrected focused suite: **22 passed, 12 subtests passed, exit 0.**

### Fix follow-up: unsupported URL carrying a literal secret

`_rendered_host_path_text` originally returned the projector's *changed text*
as the `host-path` group field.  For an unsupported URL such as
`custom://user:TOKEN@internal.example/x` the projector emits no supported-URL
display, but its literal-secret rule still rewrites `TOKEN` to `<secret>`, so
the changed text was returned as a URL-derived group field
(`custom://user:<secret>@internal.example/x`).  The function now reads the
projector's host/path **display product**
(`DiagnosticProjector.take_host_path_displays`) directly and requires exactly
one display, so an unsupported URL contributes no host/path grouping field at
all -- even when it contains a literal secret.  `phase5_fix_red.log` runs the
two new regressions against the pre-fix body and shows the old value;
`phase5_green.log` shows them passing after the fix.  The streaming collector
path (`SafeFetchRecord.host_path` from the coordinator) was already correct and
is asserted alongside.

## Task-to-evidence review

| Task | Executable evidence |
| --- | --- |
| 5.1 | `TestCollectorChannelModes` (9 tests): redacted/host-path/exact local+tail+SDK, standalone `<secret>`, unknown-scheme text, single selected representation, partition invariance, SDK independence |
| 5.2 | `TestGroupingPolicy` (5 tests): unknown rendered text, masked-host and masked-path groups, eligible supported-URL distinction, `lines` window rule |
| 5.3 | `TestFetchSidePaths` (8 tests) and updated `tests/test_npm_fetch_phase4.py`; parser grammar unchanged |
| 5.4 | `phase5_red.log` with the four production failures distinguished from the one helper omission |
| 5.5 | `TestCollectorChannelModes` isolation tests; `phase5_channel_isolation.md` |
| 5.6 | Coordinator/projection/collector/fetch integration below; masked-host fetch assertions and the unsupported-URL-with-secret regression |
| 5.7 | `docs/host-build-output.md` "Supported schemes and unknown text" + policy/propagation text, checked against the focused tests |
| 5.8 | `phase5_channel_isolation.md` |
| 5.9 | `phase5_validation.log`, `phase5_types.log`, `phase5_affected.log` |

## Integration (5.5/5.6)

The single render/projection owner now records the rendered host/path display
at component settlement, separately from the validated fact:

- `diagnostic_coordinator._settle_components` appends
  `SafeHostPath(url.host, url.path)` — with `url.host` either the normalized
  hostname or `<redacted-host>` and `url.path` either the canonical safe path
  or `/<redacted-path>` — to a new `_host_path_displays` list.
- `DiagnosticProjector.take_host_path_displays` drains it.  The existing
  `host_paths` fact list is unchanged, so a secret-bearing host still yields no
  normalized-host fact and no fingerprint (P4 contract intact).
- `NpmDiagnosticStream` drains the display per logical line and prefers it over
  the fact when building the fetch record, so the group key and canonical text
  use the same masked product as `local_text` and the tail.
- `npm_fetch._rendered_host_path_text` reads the projector's host/path display product for the complete-line `NpmFetchRecord` path instead of reading only the fact or the changed text.  An unsupported URL emits no display product, so `None` is returned and the canonical text falls back to `<redacted>` with no URL-derived group field -- even when the URL contains a literal secret.

No second retained tail is created.  The display list is one entry per settled
URL in the current line, drained at each record boundary.

### Route reconciliation

| Route | Result |
| --- | --- |
| Redacted local + SDK + tail | supported URL → `<redacted>`; standalone secret → `<secret>`; unknown scheme stays ordinary text with no facts |
| Host-path local + tail | `<redacted-host>` / `/<redacted-path>`; safe components unchanged |
| Exact local + tail | source including secrets; SDK stays redacted |
| Structured/SDK `text` | identical across local modes; masked hosts carry no hostname/fingerprint |
| Fetch canonical/group key | masked host/path products; unknown scheme supplies no URL-derived field; eligible secret-free identities still distinguish groups |
| Ordinary grouping | compares rendered text and normal identity; no URL facts for unknown text |
| Lines/interactive | masked-host and masked-path latency variants share one group in both modes |

## Documentation (5.7)

`docs/host-build-output.md` now lists the exact supported-scheme set, states
that unknown schemes remain ordinary text outside the SDK URL-free guarantee,
documents `<redacted-host>`, `/<redacted-path>` and standalone `<secret>`, and
clarifies that external SDK events stay redacted under local `exact`.  Its two
worked examples (`https://TOKEN.example/public` → `<redacted-host>/public`;
`https://host.example/TOKEN/private` → `host.example/<redacted-path>`) are
asserted verbatim by `test_host_path_masks_secret_host_in_all_channels` and
`test_host_path_masks_secret_path_but_keeps_safe_host`.

## Scope and compatibility

The two pre-revision
`add-configurable-network-url-display` tests that locked the old `<redacted>`
fallback were renamed to `test_host_path_identity_masks_secret_host` and
`test_host_path_canonical_text_masks_secret_host` and now assert the revised
`<redacted-host>` rendering.  No configuration option, persistence format,
assembly identity, argv, deadline, scheduler or shutdown budget changed; the
full review is in `phase5_channel_isolation.md`.  No specification file was
edited except the `tasks.md` completion boxes.

## Final-state verification

All pytest commands use `.venv/bin/python -m pytest ... -n 8 --dist loadfile
--tb=short -q` from the repository root.  Full stdout/stderr and the actual
exit status are in each log.

| Command / evidence | Result | Exit |
| --- | --- | --- |
| pytest `tests/test_diagnostic_channels_phase5.py`; `phase5_green.log` | 22 passed, 12 subtests passed | 0 |
| affected collector/grouping/fetch/presentation tests; `phase5_affected.log` | 502 passed, 6676 subtests passed | 0 |
| pytest `tests`; `phase5_validation.log` | 6407 passed, 13 skipped, 11 warnings, 17579 subtests passed | 0 |
| `./scripts/check-types`; `phase5_types.log` | All checks passed | 0 |

The full suite includes the revised P1–P5 gates and every repository test; the
existing multiprocessing/fork `DeprecationWarning`s are retained in the log.
`phase5_red.log` is a pre-fix snapshot and predates the added partition test.
