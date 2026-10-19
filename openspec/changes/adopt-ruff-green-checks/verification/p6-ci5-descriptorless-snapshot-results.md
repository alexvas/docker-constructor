# CI run 39305142322 — settled descriptor-less release diagnostics

## Source state and environment

Base: `767886e6b353f9e811fe417da7e9160517789b3a`, initially clean.
Final source is this base plus changes to these two files (SHA-256):

- `tests/test_process_adapter_compatibility.py`:
  `2e2473eeb9c8829a547c4db77838c7d98631b5a29c86a15d57d15f20ca35a9b4`
- `tests/enforcement/test_ci_lifecycle_scheduling.py`:
  `dbe0ad0fb023b17be43d877559615c8ebaaf95d9da6254d617ee775491e8e1de`

Fingerprint command: `sha256sum tests/test_process_adapter_compatibility.py tests/enforcement/test_ci_lifecycle_scheduling.py`, exit 0.
Production code, cleanup/operation budgets, skips, inventory, specifications and
completion boxes are unchanged. No stage, unstage, reset or commit commands.
Historical reports apply only to their checked source states.

Linux container; CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**,
pytest **9.1.1**, pytest-xdist **3.8.0**, Node **24.18.0**, npm **11.16.0**.
Version commands (exit 0):
`.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'`,
`node --version`, `npm --version`.

Reported artifact:
https://github.com/alexvas/docker-constructor/actions/runs/39305142322/artifacts/12212669250
The user-supplied excerpt reports **1 failed, 8175 passed, 9 skips,
18028 subtests, 11 warnings**, CPython **3.14.8**, 235.89 s. Artifact metadata
command `gh api repos/alexvas/docker-constructor/actions/artifacts/12212669250`
exited **4** (GitHub CLI authentication unavailable). No artifact download or
additional hosted-log inspection is claimed. Diagnosis is based on the excerpt,
source inspection and controlled local reproduction. Hosted acceptance of this
source and CPython 3.14.8 verification remain outstanding.

## Diagnosis and correction

The failing descriptor-less fixture retained the immutable observation returned
by `shutdown(primary=primary)`, awaited settlement with `wait_cleanup(1)`, then
searched the earlier observation for a late close failure. A bounded shutdown
may legitimately return incomplete before that failure is registered. Waiting
on the same owner does not mutate an earlier result. In the reported case,
the earlier result can contain only the primary KeyboardInterrupt, not the
independent KeyboardInterrupt raised later by stderr close.

The fixture now obtains its diagnostic result from the existing
`wait_cleanup(1)` call. All original completion, exact error identity, priority,
independent stream close, single-close, repeat-shutdown and final cleanup
assertions remain. There is no acceptance of a missing failure and no enlarged
observation or production budget. No new descriptor, stream or process owner,
transfer, acquisition or production cleanup branch was introduced.

The 24 new controlled cases cover both pipe positions, three primary choices
(None/ValueError/KeyboardInterrupt) and four close choices
(None/OSError/RuntimeError/KeyboardInterrupt). A gate holds the selected close
before it records its error; a coordination-wait seam returns an incomplete
initial observation. The existing explicit cleanup observation releases the
gate and waits through the same owner. Assertions verify the original fixture,
that the retained first observation stays incomplete and lacks the late error,
that the settled result contains that exact error once, and that both pipes
close exactly once. The no-close-error cases also verify incomplete-to-complete
observation without requiring a diagnostic error.

## Reproduction and fresh verification

Complete stdout/stderr was captured temporarily and each runner's exit status
preserved. No failure filtering, exclusions, warning suppression or added skips.

Before the fixture correction, with only the new regression added to the base:

`.venv/bin/python -m pytest tests/enforcement/test_ci_lifecycle_scheduling.py -k descriptorless_fixture --tb=short -q`

Exit **1**: **18 failed, 6 passed, 121 deselected**, 1.91 s. Every close-error
combination failed; no-close-error controls passed. This includes the exact
reported stderr/primary-KeyboardInterrupt/close-KeyboardInterrupt combination.
The regression source was unchanged after this RED run.

Fresh final-source checks:

| Exact command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/ruff check tests/enforcement/test_ci_lifecycle_scheduling.py tests/test_process_adapter_compatibility.py` | 0 | All checks passed |
| `.venv/bin/ruff format --check tests/enforcement/test_ci_lifecycle_scheduling.py tests/test_process_adapter_compatibility.py` | 0 | Both files already formatted |
| `.venv/bin/python -m pytest tests/test_process_adapter_compatibility.py tests/enforcement/test_ci_lifecycle_scheduling.py -n 8 --dist loadfile --tb=short -q` | 0 | **224 passed**, 8.47 s |
| `scripts/check-green` | 0 | **8196 passed, 13 skips, 18028 subtests, 11 warnings**, 54.49 s |
| `taskset -c 0,1 scripts/check-green` | 0 | **8196 passed, 13 skips, 18028 subtests, 11 warnings**, 121.71 s |
| `git diff --check` | 0 | No whitespace errors |
| `git diff --cached --check` | 0 | No staged whitespace errors |

Both gates independently passed lint, formatting, compilation and types, and
ran the unchanged full-suite command:
`.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
Eight workers were retained on both normal and two-CPU runs. The 13 local skips
are three docker-dev prerequisites, one missing sudo and nine Docker opt-ins;
hosted CI has four fewer privilege-related skips. All 11 multiprocessing/fork
DeprecationWarnings remain visible. Dockerfile is unchanged; Hadolint was not
run and is not applicable to this test-only correction.

Additional final-source two-CPU repeats: **240 controlled cases passed**, exit
**0**, with the original fixture and new ownership/error/snapshot assertions:

```bash
taskset -c 0,1 .venv/bin/python - <<'PY'
from tests.enforcement.test_ci_lifecycle_scheduling import test_descriptorless_fixture_observes_late_release_failure as case
for _ in range(10):
    for position in (0, 1):
        for close in (None, OSError, RuntimeError, KeyboardInterrupt):
            for primary in (None, ValueError, KeyboardInterrupt):
                case(primary, close, position)
print('240 controlled descriptor-less cleanup cases passed on two CPUs')
PY
```

This is a focused correction of the reproduced test contract, not proof that
all scheduling races or other stale-snapshot assertions are absent. No P7 work
or architecture migration is included; successful hosted verification is still
required for Phase 6 acceptance.
