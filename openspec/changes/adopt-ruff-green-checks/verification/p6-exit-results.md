# P6 exit — permanent local and GitHub control

**Status: ACCEPTED for Phase 6 only.** Tasks 6.1–6.24 were accepted before
this continuation; 6.11 was explicitly accepted by the user after reporting
that the last two GitHub Actions runs succeeded. This report completes 6.25
with fresh focused and G checks of the same current source/control state.
Phase 7 remains unstarted; this is not final migration acceptance or archive.

## Checked exit state

Revision: **`7423787953bce2d313e9cc7dccd39936505d771b`** (`CI runs OK`).
The worktree was clean before verification. No implementation, configuration,
workflow, source fixture, test or production file changed during this
continuation; only this evidence, the phase report pointer and task 6.25's
checkbox change afterwards. No index mutation or commit was performed.

The shared selector includes **408** Python sources. Exact fingerprint
commands (each exit 0):

```bash
.venv/bin/python scripts/python_checks.py list >"$paths" 2>"$selection_log"
xargs -d '\n' sha256sum <"$paths" >"$source_manifest"
sha256sum "$source_manifest"
wc -l "$source_manifest"
sha256sum .github/workflows/validation.yml AGENTS.md README.md README.en.md README.zh.md docs/python-checks.md docs/testing.md pyproject.toml scripts/check-green scripts/check-types scripts/check-python-lint scripts/check-python-format scripts/check-python-compile >"$control_manifest"
sha256sum "$control_manifest"
wc -l "$control_manifest"
sha256sum -c "$source_manifest"
```

The shell variables denote temporary diagnostic files, not durable evidence
links. Both generated manifest contents are reproducible from the recorded
revision; they contain normal `sha256sum` output, in the order above.

| Fingerprint | Entries | SHA-256 of manifest contents |
| --- | --- | --- |
| Selected Python source contents, sorted by selector | 408 | `889e41489789013b6f525550c43161cea48b9d78637d4ca869bbc2ae6bfa7ff8` |
| Workflow/doc/config/entry-point contents, command order above | 13 | `c01d6d53c5b04b6d2b4eafa5f4e52a258d26924e7d4a1b301bce7e563ec64ec9` |

The source manifest recheck after both full runs succeeded for every file.
This read-only source/control comparison also exited 0 with no differences:

```bash
git diff --exit-code HEAD -- docker tests scripts .github/workflows/validation.yml AGENTS.md README.md README.en.md README.zh.md docs/python-checks.md docs/testing.md pyproject.toml Dockerfile
git ls-files --others --exclude-standard -- docker tests scripts
```

The latter reported no untracked source files. `pi-green-loop.json` remains
absent at the user's request; `test ! -e pi-green-loop.json` exited 0. The
shared direct entry point is authoritative, not an auto-detected substitute.

## Environment

Linux container, UID/GID 1000 (`dev`), CPython **3.14.7**, Ruff **0.17.1**,
ty **0.0.73**, pytest **9.1.1**, pytest-xdist **3.8.0**, Node **24.18.0**,
npm **11.16.0**. Exact environment commands, each exit 0:

```bash
.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'
node --version
npm --version
id
```

The local environment lacks sudo and docker-dev; the ordinary suite does not
opt into standalone Docker acceptance. CI declares and provisions its
privilege prerequisites rather than accepting their absence as skips.

## Implemented policy and focused acceptance

- `AGENTS.md` requires G after every major change and before any commit.
  The three maintained READMEs link the shared contributor guide; existing
  ownership and evidence rules are retained.
- `scripts/check-green` runs lint, formatting, shared compilation, the existing
  type entry point and the complete pytest suite. Every independent check runs,
  and the first failure status survives. Owned process cleanup remains required;
  CLI narrowing/mutation arguments are rejected.
- Local pytest defaults to **eight workers**. The workflow explicitly sets
  **two workers**, as requested by the user for its two-core runner. Only
  parallelism differs: `tests --dist loadfile --tb=short -q` remains complete.
  `GREEN_CHECKS_WORKERS` accepts integer counts 1–1024, not arbitrary pytest
  options. Injected-runner tests exercise boundaries, errors and command/status
  preservation without nested full-suite launches.
- The workflow retains push/PR triggers, the independently required type step,
  Python 3.14, declared development-group installation, reviewed Node/npm and
  privilege provisioning. Ruff's single declared installation pin, rules,
  Python target and source inventory match the local checks.
- Check execution is non-mutating. Complete output and actual status are
  preserved; an `always()` upload with seven-day retention cannot turn a
  failed check green. No automatic commits, hooks or branch-protection changes
  are implied.
- Skip enforcement accepts only documented identity/reason/applicability
  combinations and rejects unknown skips or unexpected xfail/xpass, without
  freezing a platform-dependent skip count.

The focused run below covers local/workflow integration, gate status and
retention, skip policy, owned fixture reads, CI fixture isolation and the
subsequent lifecycle scheduling controls. Existing accepted RED/work-order
history remains in [p6-contract-ledger.md](p6-contract-ledger.md) and the
individual CI repair reports; no historical result substitutes for these
fresh exit checks.

## Fresh verification for this exit state

Complete stdout/stderr was captured temporarily for each runner and its actual
exit status preserved. Summaries were prepared only after command completion;
no failure/warning filtering, test exclusion or regression-hiding skip was used.

| Exact command | Exit | Result |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/enforcement/test_green_integration.py tests/enforcement/test_green_skip_policy.py tests/enforcement/test_green_fixture_lifecycle.py tests/enforcement/test_ci_fixture_isolation.py tests/enforcement/test_ci_lifecycle_scheduling.py tests/enforcement/test_ci_lifecycle_scheduling_followup.py -n 8 --dist loadfile --tb=short -q` | 0 | **232 passed**, 8.76 s |
| `scripts/check-green` | 0 | All five commands succeeded; **8226 passed, 13 skipped, 11 warnings, 18028 subtests passed**, 54.81 s |
| `GREEN_CHECKS_WORKERS=2 taskset -c 0,1 scripts/check-green` | 0 | Same complete gate and counts; **8226 passed, 13 skipped, 11 warnings, 18028 subtests passed**, 161.49 s |
| `sh -n scripts/check-green` | 0 | Shell wrapper syntax accepted |
| `git diff --check` and `git diff --cached --check` | 0 each | No whitespace diagnostics; read-only index inspection |
| `openspec instructions apply --change adopt-ruff-green-checks --json` | 0 | 103/117 complete; no remaining Phase 6 task |

Both full gates independently executed these commands successfully:

| Manifest entry / command | Status in each gate |
| --- | --- |
| `scripts/check-python-lint` | Exit 0, no unresolved findings |
| `scripts/check-python-format` | Exit 0, **408 files already formatted** |
| `scripts/check-python-compile` | Exit 0, complete shared inventory compiles |
| E2: `scripts/check-types` | Exit 0, no unresolved ty diagnostics; `docker` and `tests/typing` retained |
| E1: `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | Exit 0 in the local gate; the separately requested CI profile uses `-n 2`, also exit 0 |

This covers all unconditional existing/adoption commands in
[check-manifest.md](check-manifest.md). E4 (`scripts/check-dockerfile`) is
conditional on Dockerfile edits: Dockerfile is unchanged, so Hadolint is not
applicable and was not executed or claimed as passed.

All **11 warnings** remain visible CPython multiprocessing/fork
DeprecationWarnings in constructor generation lock parity, constructor build
transactions and namespace locking tests. There are no unknown skips,
xfails/xpasses or hidden runner errors.

## Exit skip review

Both full runs emitted the same 13 individually reviewed skips, matching
[skip-inventory.md](skip-inventory.md), `tests/green_skip_policy.py` and the
conditions in `docs/python-checks.md`:

- `ConstructorProjectForeignOwnership::test_foreign_owned_constructor_project_keeps_external_state_private`
  in `tests/test_constructor_build_cache_permissions.py`, and
  `TestArtifactSnapshot::test_foreign_owner_hard_link_is_rejected_without_adoption`
  plus `TwoBuildOwnershipMaintenanceRegression::test_two_builds_reuse_blobs_after_project_ownership_maintenance`
  in `tests/test_constructor_build_snapshot.py`: an existing docker-dev account
  is required; acceptable only outside CI/privileged-helper mode.
- `TestPhase4Lifecycle::test_differing_uid_cannot_traverse_private_snapshot` in
  `tests/test_constructor_phase4_named_context.py`: sudo executable absent;
  acceptable only outside CI/privileged-helper mode.
- All seven exact registry acceptance IDs enumerated in the skip inventory
  and policy: require `validate-bound-pi-assembly-execution-phase6-registry`;
  the standalone hermetic runner was not selected.
- `TestDockerBackedHostAcceptance::test_outer_deadline_reaps_container_and_removes_staging`
  in `tests/test_bound_pi_assembly_execution_phase6_host.py`: requires
  `validate-bound-pi-assembly-execution-phase6-host`, likewise not selected.
- `TestRealPinnedContainerSmoke::test_node_image_executes_assembler_script` in
  `tests/test_npm_environment_phase3_validate.py`: `NPM_ENV_REAL_DOCKER=1`
  opt-in not selected.

No skip policy or fixture changed in this continuation. The two-worker check
is a local CPU-constrained execution, not a hosted prerequisite simulation or
performance guarantee.

## Hosted evidence, provenance and limitations

The user reported **two successful recent GitHub Actions runs** and explicitly
authorized checking 6.11. Their supplied pytest excerpt reports Python
**3.14.8**, **8207 passed, 9 skipped, 11 warnings, 18028 subtests passed**, in
**142.20 s**. Its nine skips are the reviewed Docker opt-ins; it contains no
privilege-prerequisite skips.

This is **user-reported hosted acceptance**, not an independently retrieved
Actions observation. No run URL/ID or checked SHA accompanied that confirmation.
The supplied counts differ from current local counts and are not asserted to
belong to revision `7423787` or the new worker-range tests. No remote exact
command, numeric exit code, artifact download or SHA association is invented.
The current implemented local/CI policy, focused contracts and full G are
bound to one exit state by the fresh local results above. The user's explicit
6.11 acceptance is preserved without upgrading the excerpt into proof of a
particular hosted revision. Phase 7's separate final/CI evidence obligations
remain untouched.

## Exit decision

P6's controls, focused contracts, local/CI command-scope parity and G pass for
the recorded state; 6.11 has the user's explicit hosted provisioning acceptance.
**6.25 is accepted; Phase 6 is 25/25 complete.** The overall change is 103/117
complete, with all 14 Phase 7 tasks left unchecked. No architecture migration,
new lifecycle policy, dependency-inventory change or reduced stress construction
is authorized or claimed. No staging, unstaging, reset, commit or archive was
performed. Earlier partial P6 reports remain historical, not current blockers
or current verification substitutes.
