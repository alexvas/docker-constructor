# Evidence collector owned-I/O correction

## Implementation and ordering evidence

- `scripts/collect-docker-cancellation-evidence.py` imports explicit filesystem
  submodules. It prepares paths/labels/owner slots before acquiring each file
  through `PosixDescriptorOps.openat` and immediately adopting it with
  `OwnedDescriptor`. Reads/writes and borrowed subprocess output descriptors
  remain under those owners; no Path.open/read_text/write_text or fdopen remains.
- `CleanupFailures(primary)` handles operation-plus-release failure, terminal
  exactly-once release and independent cleanup. Command JSON publication,
  attached-client finalization, per-ID/per-operation cleanup and summary
  publication use shared precedence rather than overriding an active failure
  in a finally block. Summary-publication failure cannot claim a successful
  process exit; the host guide explains incomplete/pre-publication summaries.
- Offline fault tests exercise ordinary operation plus close, sole close,
  write/decode operation faults, first/later interruption, unexpected close
  defect, repeated successful/failed release, second acquisition failure,
  independent sibling/operation cleanup, JSON/summary publication faults and
  rejection of unowned file methods. No test substitutes simulated daemon
  behavior for real host evidence.
- New pending task 3.10 places policy creation-race RED coverage before group-3
  GREEN implementation. Task 3.9 verifies 3.8/3.10, not group-4 task 4.8.
  Group-3 validation includes 3.10 before group 4 starts. Task 4.8 now tests
  consumer integration of those already-proved policy races. An executable
  task-order assertion checks this dependency boundary.

## Verification

- RED: `/tmp/evidence-owned-red.uYTIeo.log`, exit 1: 8 failed, 5 passed.
  These assertions failed before owned-I/O helpers existed.
- Focused GREEN: `/tmp/evidence-owned-focused.tECSwz.log`, exit 0:
  20 passed (before the final task-order assertion was added).
- Full final pytest with eight workers:
  `/tmp/docker-constructor-pytest.8MBkMU.log`, exit 0:
  6660 passed, 13 skipped, 11 warnings, 17775 subtests, 30.12 seconds.
  Includes all 21 collector/order tests.
- Configured unittest discovery:
  `/tmp/evidence-owned-unittest.9JEWaW.log`, exit 0:
  6426 tests, 13 skipped, OK, 140.846 seconds.
- Configured typecheck:
  `/tmp/evidence-owned-typecheck.A3NYOT.log`, exit 0. Its configured scope is
  `docker`; it does not typecheck the collector or tests.
- Strict change validation: `/tmp/evidence-owned-validate.json`, exit 0.
- `git diff --check` and standalone collector `--help`: exit 0.

Real Docker daemon results remain pending the user's host run. This correction
is evidence-tool work, not implementation/acceptance of the Docker wrapper.
No implementation or host-run task checkbox was marked complete; no Git index
operation was performed.
