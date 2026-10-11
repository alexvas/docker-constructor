# P6 — one-off before/after performance and allocation evidence (6.3)

Experiments run outside tests/CI using the same project CPython 3.14.7.
Before: isolated `git archive 50f9ebc` export (pre-implementation). After:
current production checkout. `phase6_experiment_environment.log` records
the full revision/export location; JSON records commands/platform/CPU/repeats.
No working-tree reset, staging or baseline production fallback was used.

Five samples per median; timing runs are unprofiled/untraced and call GC
before each repeat. Raw samples and full output/statuses are preserved in
`phase6_before.json`, `phase6_after.json`, and their `*_experiment.log` files.
Inputs are the historical 1/2/4/8/16 KiB runs, separated eight-character
tokens, `a * N + " end"`, and the unchanged phase1 profile sample. Profiles
are 200 calls of `project_tail` per mode, with 25 cumulative entries.

## Isolated uninterrupted recognition

Before times the old `_find_url_start`; after times a new production
`IncrementalRecognizer` including construction, feed and finalization.
This is not an identical API benchmark: the new call does more work.
Historical `phase1_baseline.log` is corroborated by a fresh before run.
The old detector has no continuation API, so no invented old chunked
detector is compared; the common projector API below covers chunked before/after.

| KiB | Before single feed (s) | After single feed (s) | After 1024-char feeds (s) | After one-char feeds (s) |
| --- | --- | --- | --- | --- |
| 1 | 0.013233 | 0.008143 | 0.008143 | 0.008441 |
| 2 | 0.052937 | 0.016215 | 0.016215 | 0.016900 |
| 4 | 0.211751 | 0.032872 | 0.032717 | 0.034117 |
| 8 | 0.857589 | 0.065573 | 0.065357 | 0.068110 |
| 16 | 3.430774 | 0.130417 | 0.130440 | 0.136148 |

## Same projector API, unchanged synthetic input

| KiB | Before single (s) | After single (s) | Before 1024-char chunks (s) | After 1024-char chunks (s) |
| --- | --- | --- | --- | --- |
| 1 | 0.013427 | 0.014444 | 0.026818 | 0.014093 |
| 2 | 0.053210 | 0.027505 | 0.120128 | 0.027425 |
| 4 | 0.413056 | 0.054760 | 0.614830 | 0.055815 |
| 8 | 1.068979 | 0.109022 | 2.737301 | 0.108903 |
| 16 | 1.064023 | 0.213083 | 2.720812 | 0.215167 |

The old arbitrary-scheme pending token overflows at 8 KiB, so its
8/16 KiB projector timings plateau; the supported-only implementation
treats this `a` run as ordinary text. Rendering differences are intentional,
not a reason to compare old and new marker output as equivalent.

## Capture and allocator trade-offs (not all workloads improve)

| Mode | Before median capture (s) | After median capture (s) | Before traced peak bytes | After traced peak bytes |
| --- | --- | --- | --- | --- |
| redacted | 0.000169 | 0.003577 | 25475 | 68340 |
| host-path | 0.000173 | 0.003587 | 25475 | 68726 |
| exact | 0.000076 | 0.000081 | 25475 | 25475 |

Safe-mode short capture is slower and has higher transient allocation:
the persistent fixed-depth Python recognizer and compiled secret registration
do more work than the old short-string regex path. Exact capture is close.
Separated-run medians, complete profile call stacks and traced remaining
allocation sites are included in the JSON; these regressions are not hidden.
No absolute throughput/allocation acceptance threshold was specified.
This change proves removal of quadratic candidate search, not a speedup
for every input, lower total allocations or whole-pipeline linearity.

Tracemalloc samples are separate from timing runs, with remaining and peak
bytes/top retained sites. Input-doubling projection allocator samples are
also recorded. These are descriptive allocator data, not bounded-live-state
proof; the recursive state/counter audits supply that independent evidence.

## Unchanged historical oversized/retained-tail fixtures

The exact `phase1_oversized_fixture_baseline.py` is used for both versions,
five repeats for all fourteen scenarios. No fixtures, sizes, chunking or
stress counts are reduced. Full observable outcomes and raw timings are in
`phase6_before_fixtures.log` / `phase6_after_fixtures.log` (both exit 0).

| Fixture | Before median (s) | After median (s) |
| --- | --- | --- |
| projector: candidate past byte limit | 0.003166 | 0.088140 |
| projector: multibyte unterminated, 512-char feeds | 0.005143 | 0.150018 |
| projector: multibyte whole feed, drain-entry peak | 0.004514 | 0.149724 |
| projector: 7-byte feeds of 4x-limit token | 0.527515 | 0.303558 |
| projector: oversized token then recovery URL | 0.004472 | 0.155055 |
| projector: oversized token finalized at EOF | 0.003454 | 0.154651 |
| host-path: oversized URL token, no fact | 0.002073 | 0.083549 |
| collector: 16 KiB URL, 256-byte feeds | 0.022775 | 0.162349 |
| collector: newline-free line past 64 KiB | 0.044138 | 1.041356 |
| collector: recovery after safe boundary | 0.057205 | 1.032703 |
| collector: input keeps draining after overflow | 0.048071 | 0.939125 |
| collector: oversized URL-heavy line, metadata + recovery | 0.042597 | 0.925087 |
| retained-tail: bounded + mode-selected text (x-run past 2x TAIL_BYTES) | 7.809668 | 2.598597 |
| retained-tail: bounded text + JSON (x-run past 2x TAIL_BYTES) | 7.768604 | 2.626063 |

The two original retained-tail bottlenecks improve from about 7.8 s to
2.6 s with their `TAIL_BYTES * 2` uninterrupted payloads unchanged. Some
already-fast URL and ordinary line cases become slower. No optimization
of fetch long-authority parsing or unrelated rendering is claimed.

The historical `_DrainEntryProbe._drain` hook no longer runs in the
coordinated projector; its new-version zero observation is **not** used
as a bound proof. Current tests use `_drain_coordinated` and the separate
`phase6_ownership_work.log` audit checks the whole pending buffer on every
feed. Historical oversized-marker observations likewise are not assertions
of the revised early-commit marker contract.

## Deterministic work / ownership reconciliation

`phase6_core_audit.log`, `phase6_recognizer_audit.log`, and
`phase6_ownership_work.log` rerun final-state immutable-registration,
bounded provenance/speculation, output-link/maintenance and all-instance
cumulative work/one-raw-owner audits. `phase6_differential.log` reruns
33558 partition comparisons of text and ordered permitted facts. The full
suite additionally covers every phase and current policy/display products.
No fingerprints or presentation-session keys are written into this evidence.

The measured isolated scan approximately doubles per input doubling rather
than quadrupling. Deterministic lifetime counters, not elapsed-time tests,
establish replay-free fixed-registration/fixed-depth recognition bounds.
All experiment/audit processes exited 0. No timing assertions were added
to automated tests or CI.
