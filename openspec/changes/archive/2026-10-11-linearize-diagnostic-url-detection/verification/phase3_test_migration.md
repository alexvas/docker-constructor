# P3 old-test migration review

The `.py.bak` versions were reviewed before deletion. They are not excluded
verification tests: the executable scenarios are migrated to the revised suite
and the affected integration tests. No stress input, scheduler, limit or
shutdown budget was reduced. The old P3 file had 59 test methods, many asserting
now-invalid arbitrary-scheme / retained-suffix contracts. The revised P3 file
has 32 migrated methods with parameterized fixture/partition coverage plus six
fixed-contract regression methods for mandatory crossing-prefix withholding,
matching/mismatching continuations, clean EOF, other secret coverage, SDK
products and unchanged abort/overflow, plus three delayed-activation
regressions for prompt host/path output, encoded raw coordinates and other
component-secret coverage. All 41 focused methods pass; the final full-suite
gate passes after the separately authorized registry cleanup race fix in
`registry_cleanup.md`. The standalone
partition corpus (including ordered fingerprints) and all-instance
ownership/work audit are separate evidence.

## Original P3 scenario families

| Original class | Revised coverage / deliberate contract changes |
| --- | --- |
| PartialSeparatorContinuationTests (6) | `test_exact_retention_and_independent_prefix_delivery`, supported prefix/percent cases in the P2 suite, `test_unknown_text_is_not_an_unbounded_candidate`, partition corpus. Unknown `custom+transport` prefixes no longer retain arbitrary runs. |
| DetectedButUnterminatedTests (6) | `test_complete_separator_is_distinct_from_pending_prefix`, `test_completed_url_precedes_ambiguous_tail`, commit/terminator tests. `x://a`, `y://b` and UNKNOWN candidate-kind obligations are superseded by supported-only detection, not silently treated as supported candidates. |
| ChunkPartitionInvarianceTests (3) | `test_every_split_and_utf8_byte_partition`, `test_host_path_partition_invariance`, and `phase3_projector_differential.py`. All seven original corpus strings and `token=supersecret https://a.example/x next` remain in that corpus. |
| FinalizationTests (7) | Existing projection EOF/abort tests (updated markers), `test_partial_separators_keep_fail_closed_rules`, early redacted/component EOF/abort/overflow tests, literal crossing tests. One early redacted marker replaces the old oversized/incomplete URL marker only after full detection; ordinary non-URL words no longer overflow just for being scheme-like. |
| IncrementalWorkTests (4) | Long-run 3000/6000 fixtures, all-search-instance instrumentation, before/after-terminator commits, legacy partial escape/separator tests. Consuming a pending scheme does not destroy its continuation: the old assertion that consuming `https` prevents later `://host` is explicitly superseded. No re-feed is permitted. |
| ProjectorBufferWorkTests (8) | `test_legacy_long_run_copy_and_encoding_workloads_are_linear`, cumulative overflow audit, UTF-8 whole-buffer limit/probe tests. Original z 2000/4000, URL-path a 2000/4000 and 50 é payloads remain. New counters measure append/pop/encoding/materialization honestly; they do not retain the old misleading zero-copied-char convention. |
| ProjectorIncrementalStateTests (6) | Persistent object identity, all-instance instrumentation, no `_tail`, fixed decoder/provenance-state inspections, cumulative input doubling, exact marker counts in repeated commits. |
| ConsumptionBoundaryTests (10) | `test_legacy_commit_fixtures_preserve_continuation_without_batch_replay`, `test_legacy_partial_escape_and_separator_continuations`, before/after-terminator tests, P2 committed decoder-secret regressions. Already-recorded outcomes retire; live cursors and pending units survive, so an overlapping future candidate may start before the commit. Unknown `a://h` candidate expectations are obsolete. |
| RetainedSuffixContinuationTests (9) | All eleven legacy fixture strings are exercised at every raw cut and multiple partitions, comparing persistent incremental outcomes and unchanged continuation state/work. Raw encoded offsets, nested encodings and leading commits remain covered. Arbitrary cuts no longer invent `ttps://`, `tps://`, `ps://` or `s://` supported candidates. Original-suffix batch equivalence / mandatory destruction of pending escapes was the defect, not a compatibility obligation. |

## Additional revised obligations

The new suite adds both Text/URL crossing directions, immediate `TOKENht`
commit and overlapping scheme continuation, three-pass decoded coverage,
leftmost/longest overlapping-pattern settlement, independent host/path display
commits, delayed facts/fingerprint order, a single percent finalization, repeated
recovery, all recognizer-instance accounting and two collector products sharing
one raw owner. The corpus includes depth-four separators, the pending-transducer
`%%25368ttps://host` case, negative incomplete four-pass scheme prefixes and
unsettled component-secret EOF. The formerly conflicting early-output test
now follows the explicitly approved mandatory crossing-secret delay and its
clean-EOF non-match outcome; no code-driven specification exception was made.

## Other backed-up test files

- `test_host_diagnostic_projection.py`: same fixtures, all chunk sizes and
  stress lengths; new assertions distinguish supported / unknown schemes,
  `<secret>` and irreversible early URL markers. The append-bound probe follows
  `_drain_coordinated` and cached byte accounting.
- `test_host_path_projection.py`: same validation, Unicode, unsafe-path and
  stress fixtures; incomplete/oversized detected URLs retain their early
  redacted marker without authorizing host/path facts.
- `test_npm_diagnostic_collection_phase8.py`: same reader failure, cancellation,
  provisional/final record, secret and fetch fixtures. Settled host output
  survives failure, SDK URL output retains its marker, arbitrary ordinary words
  commit promptly, standalone markers become `<secret>`. Substring-secret
  assertions distinguish fixed marker products from restored source (`sec`
  necessarily occurs in `<secret>`).
- `test_presentation_wiring_phase7.py`: same channel/rendering matrix and
  fixtures. Abort retains settled host output locally while SDK text stays
  `<redacted>` with no pending facts; standalone-secret marker expectations
  reflect the new prescribed marker.
- `test_retained_tail_phase6.py`: same capture/tail ownership and cancellation
  fixtures. The host-path alias now references the same projector, with exactly
  one pending raw deque and one selected retained tail. Early covered URL /
  component output is retained, not retracted on cancellation.

All scenarios outside intentional specification changes remain executable;
full-suite verification has no exclusions. Historical command evidence remains
under `historical-phase3/`, not an acceptance substitute. Temporary `.py.bak`
files were removed after this coverage review, as requested. The former
blocking scenario is now covered by the fixed specification's three explicit
outcomes, without retaining or excluding any old test file. See `phase-3.md`
for final-state full-suite and audit results.
