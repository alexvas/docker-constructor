# Phase 3 — replay-free continuation / ownership gate

**Status: PASS for P3 only.** Delayed activation behavior, ownership/audits,
configured types and the final full suite pass. The registry cleanup race was
reproduced deterministically and fixed with explicit user authorization; see
`registry_cleanup.md`. The failed run remains historical, not accepted evidence.
Overall progress is 44/72, P3 14/14. P4–P6 remain pending. See
`phase3_delayed_activation.md` for the production fix and focused checks.

## Fixed policy and implementation

The approved requirement now mandates withholding a source-order-dependent
URL marker when an earlier unresolved literal secret prefix crosses its
recognized separator. With secret `Xhttps://host`, first feed `Xhttps://ho`
returns no output. Matching continuation produces `<secret><redacted> done`,
mismatching continuation produces `X<redacted> done`, and clean EOF without
continuation produces `X<redacted>`. Neither clean-EOF non-match output nor
marker delivery authorizes metadata without existing source validation and
secret-coverage checks. Abort and overflow remain fail-closed.

`_drain_coordinated` resolves that crossing prefix as a non-match only for
clean finalization with an already-detected URL and an earlier literal prefix.
It still applies completed secret records; unrelated and partial-separator
prefixes keep incomplete-token handling. No source is reconstructed or re-fed.

The coordinator owns one raw UTF-8-accounted deque and one persistent
recognizer. Consumption retires coordinate outcomes, preserving automata,
pending decoding units and overlapping secret/URL continuation. Display,
raw ownership, component settlement and validated facts have separate
frontiers. Recognition counters survive all commits and recovery. The pending
limit is 8 KiB for the entire deque, not a read-size assumption. Overflow
abandons bounded metadata and observes raw recovery boundaries using the same
recognizer; synthetic line-overflow output bypasses suppressed source.

An explicit safe selector replaces the opaque formatter (breaking replacement
was approved). Host-path and SDK redacted products share one projector/raw
owner; only the selected representation feeds the retained failure tail.
Exact local delivery remains terminal-safe source and is tested unchanged.

## RED → GREEN

`phase3_red.log` preserves the original pre-revision RED: 28 failed, 4 passed,
exit 1. Twenty-seven methods report the missing lifetime interface; one reports
the missing collector selector integration. Four low-level continuation tests
already passed due to P2. This demonstrates missing integration, not independent
behavioral failure of every later assertion.

`phase3_fixed_spec_red.log` records the behavioral RED for the revised policy:
2 failed, 34 passed, 2 subtests passed, exit 1. Failures are specifically the
clean-EOF crossing-prefix non-match and its partition result. Matching and
mismatching continuations, mandatory withholding, abort and overflow already
passed. A development off-by-one in the new overflow fixture was corrected
before this RED: `Xhttps://` has nine bytes, and the test now derives its size.
No existing stress fixture was reduced.

The current suite has 41 methods and 11 passing subtests. It checks marker
withholding on the first feed, prompt delivery at secret resolution before a
terminator, clean EOF, abort, overflow, other completed secret coverage,
partial-separator exceptions, and SDK output in all three local modes.
Partition comparisons include ordered fingerprints using one ephemeral key
per test/audit run; neither fingerprints nor keys enter evidence logs.

## Final-state verification

All pytest commands use `.venv/bin/python -m pytest ... -n 8 --dist loadfile
--tb=short -q`. Full stdout/stderr and the actual exit status are in each log.
The complete suite has no exclusions or warning suppression.

| Command / evidence | Result | Exit |
| --- | --- | --- |
| pytest `tests/test_diagnostic_recognizer_phase3.py`; `phase3_current.log` | 41 passed, 11 subtests passed | 0 |
| pytest `tests`; `phase3_validation.log` | 6352 passed, 13 skipped, 11 warnings, 17535 subtests passed | 0 |
| `./scripts/check-types`; `registry_cleanup_types.log` | All checks passed | 0 |
| `PYTHONPATH="$PWD" .venv/bin/python verification/phase3_incremental_bounds_probe.py`; `phase3_incremental_bounds_probe.log` | One owner / all-instance lifetime work audit passed | 0 |
| `PYTHONPATH="$PWD" .venv/bin/python verification/phase3_projector_differential.py`; `phase3_projector_differential.log` | 33558 partition comparisons: text, hostnames, host/path facts, ordered fingerprints | 0 |
| `PYTHONPATH="$PWD" .venv/bin/python verification/phase1a_core_audit.py`; `phase3_core_audit.log` | P1A core audit passed | 0 |
| `PYTHONPATH="$PWD" .venv/bin/python verification/phase2_allocation_audit.py`; `phase3_predecessor_audit.log` | P2 allocation audit passed | 0 |

Audit script paths in the table are relative to this change directory; actual
commands use `openspec/changes/linearize-diagnostic-url-detection/verification/`.
The full suite includes revised P1, P1A, P2 and P3 plus affected projection,
collector, lifecycle, presentation and all remaining repository tests. Its
existing multiprocessing/fork DeprecationWarnings are retained in the log.
Configured production checks were reviewed in `pyproject.toml` and
`.github/workflows/validation.yml`: typecheck uses ty and Python 3.14. There is
no configured Python lint/build check; Dockerfile lint is unrelated to these
unchanged Dockerfiles. Final diff-check / OpenSpec results are recorded in
`phase3_planning_validation.log`.

## Task-to-evidence review

| Tasks | Executable evidence |
| --- | --- |
| 3.1 | Exact retention / independent prefix delivery; supported percent/separator cases and whole-deque byte accounting |
| 3.2 | Detected-but-unterminated outcomes, completed URL / ambiguous suffix, TOKENht overlap, commits before/after raw terminators, ordered facts |
| 3.3 | Every split and UTF-8 byte partitions in P3 tests and 33558-comparison corpus, including ordered fingerprints |
| 3.4 | Feed-by-feed redacted and host/path output; all three crossing-secret outcomes; abort/token/line overflow; partial separator and prompt secret commitment |
| 3.5 | Lifetime work, all `_Pipeline` constructions, unchanged long-run fixtures, repeated overflow recovery, fixed-state ownership audit and input doubling |
| 3.6 | Original missing-interface RED and new clean-EOF behavioral RED, explicitly distinguished above |
| 3.7 | Persistent recognizer identity, absence of `_tail`, unchanged cursors/pending units after consumption, legacy continuations without batch replay |
| 3.8 | Cached byte accounting, append/pop/encoding/materialization/provenance counters, whole-pending UTF-8 limit, independent single-owner audit |
| 3.9 | Leftmost/longest overlap selection, TOKENht, literal cross-terminator coverage and URL-only decoded secrets |
| 3.10 | Single percent finalization/idempotence, independent component settlement, irreversible output, clean-EOF non-match and unchanged abort rules |
| 3.11 | Repeated bounded raw-boundary recovery, no discarded secret suffix, one token/remainder marker and independent line marker |
| 3.12 | Coordinator / projector API documentation and `phase3_incremental_audit.md` explain ownership, frontiers and finalization exceptions |
| 3.13 | Final-state ownership / cumulative audit and migration review resolve historical replay / duplicate-payload claims without unrestricted suffix equivalence |
| 3.14 | Final full suite and types pass after deterministic registry cleanup race fix; focused P3 and predecessor/ownership/partition audits pass on unchanged production code |

## Scope and migration

`phase3_test_migration.md` maps the old scenario families to executable revised
coverage and records intentionally superseded UNKNOWN hiding / arbitrary suffix
batch equivalence. Requested temporary `.py.bak` backups were reviewed and
removed before this session; old logs remain historical, not an acceptance
substitute. No archive, sync, staging, scheduler, shutdown budget, AGENTS.md,
configuration, persistence, argv or existing stress-input changes were made.
The separately authorized registry-harness cleanup fix removes a racing loop
submission without changing its existing join timeout or production behavior.
Apply-time specification edits are completion boxes only. Recognition bounds
are fixed-registration / fixed-depth / fixed-pending-budget, output-sensitive;
no whole-pipeline linearity claim is made.
