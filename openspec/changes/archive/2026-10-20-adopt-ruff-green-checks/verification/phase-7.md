# Phase 7 — final local verification

**Current status: ACCEPTED.** See [final acceptance](p7-final-acceptance.md) for the user's successful hosted CI confirmation, unchanged-source verification and completion of 7.13/7.14. The original local report below is historical; its BLOCKED decision has been superseded.

## Historical local report

**Status at initial report: BLOCKED at 7.13; 7.14 remained unchecked.** Tasks 7.1–7.12 are accepted. P1–P6 exits remain accepted historical evidence. No earlier result is represented as a fresh final-state run.

## Acceptance index and boundary review (7.1–7.5)

[p7-contract-matrix.md](p7-contract-matrix.md) maps every required outcome, predecessor deliverable and gate. No predecessor deliverable is missing. P7's provisioned CI-equivalent run and final acceptance are explicitly unresolved, not deferred predecessor work.

Checked revision: `9c7c5a8ffac68d2a645339d65bd863ea590a093c`. Worktree was clean on entry. Only P7 evidence and task completion boxes were changed; no implementation repair was necessary.

Read-only history review (`git log --oneline -18`) identifies separate preparation (`6b24592`), tooling (`d938527`), formatter (`25085a1`), automatic (`97b9816`), manual (`5ca7307`) and permanent-control (`66ccbc2` onward) boundaries. Existing phase reports retain actual failures, rejected automatic fixes, work orders and repairs. No commit or index manipulation was needed to establish these boundaries.

Fresh boundary verification, exit 0:

```bash
v=openspec/changes/adopt-ruff-green-checks/verification
for pair in 'p3-source-before.sha256 d938527' 'p3-source-after.sha256 25085a1' 'p4-source-after.sha256 97b9816' 'p5-source-after.sha256 5ca7307'; do
 read -r manifest revision <<<"$pair"
 count=0
 while read -r expected path; do
   actual=$(git show "$revision:$path" | sha256sum)
   test "${actual%% *}" = "$expected" || exit 1
   count=$((count+1))
 done <"$v/$manifest"
 printf '%s %s: %s hashes matched\n' "$revision" "$manifest" "$count"
done
```

Results: **396/396**, **396/396**, **396/396**, **397/397** hashes matched respectively. Both `diff -q` comparisons below exited 0:

```bash
diff -q "$v/p3-source-after.sha256" "$v/p4-source-before.sha256"
diff -q "$v/p4-source-after.sha256" "$v/p5-source-before.sha256"
```

The P3 purity/AST/idempotence reports establish formatter-only reproduction; P4's 317-target ledger establishes retained automatic changes; P5's corrected 614-row ledger covers all 667 original findings. These are historical transformation proofs, not fresh test runs.

Final scope review: `git diff --exit-code a573bb9 HEAD -- Dockerfile docker-constructor.toml openspec/changes/archive` exited 0 with no changes. Runtime image inventory and archived verification were not rewritten. P1–P5 reviews retain stress construction, APIs, source fixtures and lifecycle/architecture enforcement. P6's subsequent production delta is confined to `docker/npm_environment/streaming.py`; its explicitly requested worker-start/join ownership repairs and regression evidence are documented in the bootstrap/live-join ledgers/results, not disguised as formatter or lint output. The full retained enforcement suite executes in the fresh final gate. No new scope reduction, skip, xfail, suppression or lifecycle policy change was introduced here.

All predecessor test runs are historical. The source/control manifests below equal the P6 exit fingerprint, but final lint/format/compile/types/full pytest were nevertheless rerun. P7's CI prerequisite gap is not filled by P6's user-reported hosted acceptance, which lacked an exact run SHA/URL.

## Final source binding

| Manifest | Entries | SHA-256 of manifest |
| --- | --- | --- |
| [p7-source-after.sha256](p7-source-after.sha256) | 408 selected Python files | `889e41489789013b6f525550c43161cea48b9d78637d4ca869bbc2ae6bfa7ff8` |
| [p7-controls-after.sha256](p7-controls-after.sha256) | 13 control/config/doc files | `c01d6d53c5b04b6d2b4eafa5f4e52a258d26924e7d4a1b301bce7e563ec64ec9` |

Exact generation commands (exit 0; variables denote temporary diagnostics, not durable links):

```bash
paths=$(mktemp)
.venv/bin/python scripts/python_checks.py list >"$paths"
xargs -d '\n' sha256sum <"$paths" >"$v/p7-source-after.sha256"
sha256sum .github/workflows/validation.yml AGENTS.md README.md README.en.md README.zh.md docs/python-checks.md docs/testing.md pyproject.toml scripts/check-green scripts/check-types scripts/check-python-lint scripts/check-python-format scripts/check-python-compile >"$v/p7-controls-after.sha256"
sha256sum "$v/p7-source-after.sha256" "$v/p7-controls-after.sha256"
```

Final `sha256sum -c` for both manifests exited 0. `git diff --exit-code HEAD -- docker tests scripts .github/workflows/validation.yml AGENTS.md README.md README.en.md README.zh.md docs/python-checks.md docs/testing.md pyproject.toml` exited 0. Evidence/task checkboxes are outside these manifests.

## Environment and fresh verification (7.6–7.12)

Linux container, UID/GID 1000 (`dev`), CPython **3.14.7**, Ruff **0.17.1**, ty **0.0.73**, pytest **9.1.1**, pytest-xdist **3.8.0**, Node **v24.18.0**, npm **11.16.0**. Version commands exited 0:

```bash
.venv/bin/python -c 'import sys; from importlib.metadata import version; print(sys.version); print({n: version(n) for n in ("ruff", "ty", "pytest", "pytest-xdist")})'
node --version
npm --version
id
```

Complete gate stdout/stderr was retained temporarily. Actual gate status was saved before displaying its tail; no filter supplied the verification status:

```bash
log=$(mktemp /tmp/p7-green.XXXXXX.log)
scripts/check-green >"$log" 2>&1
status=$?
printf 'LOG=%s\nSTATUS=%s\n' "$log" "$status"
tail -n 65 "$log"
exit "$status"
```

| Exact command | Exit | Result |
| --- | --- | --- |
| `scripts/check-green` | 0 | All five checks passed |
| `scripts/check-python-lint` (inside gate) | 0 | All checks passed; zero unresolved findings |
| `scripts/check-python-format` (inside gate) | 0 | 408 files already formatted |
| `scripts/check-python-compile` (inside gate) | 0 | Complete shared inventory compiles |
| E2 `scripts/check-types` (inside gate) | 0 | All checks passed; docker and tests/typing retained |
| E1 `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` (inside gate) | 0 | **8226 passed, 13 skipped, 11 warnings, 18028 subtests passed**, 54.54 s |
| E4 `scripts/check-dockerfile` | Not run / not applicable | Dockerfile unchanged across adoption; no Hadolint success claimed |
| `git diff --check`; `git diff --cached --check` | 0 each | Read-only whitespace checks |

This executes every unconditional command in both manifest sections. No separate production ty command is configured; no pi-green-loop configuration was added. The direct repository gate is authoritative rather than the skill's generic auto-detection/narrowed-check suggestions.

Formatter idempotence (7.8), each command exit 0:

```bash
mapfile -t files <"$paths"
.venv/bin/ruff format --no-cache -- "${files[@]}"
.venv/bin/ruff format --no-cache -- "${files[@]}"
sha256sum -c "$v/p7-source-after.sha256"
```

Both passes reported **408 files left unchanged**; all 408 hashes matched. These write-mode verification passes were local only; no workflow mutation command was introduced. No source edit invalidated the final gate.

All **11 warnings** are unsuppressed multiprocessing/fork DeprecationWarnings in build generation lock parity, build transactions and transaction locking. No unknown skips, xfails/xpasses, runner errors or unresolved Ruff/ty diagnostics occurred.

The gate printed all 13 exact skipped IDs and reasons. They match [skip-inventory.md](skip-inventory.md), docs/python-checks.md and the existing policy:

- Three exact foreign-owner cases (constructor project, snapshot hard link, two-build maintenance): missing docker-dev account, permitted locally only.
- Differing-UID private-snapshot traversal: sudo executable absent, permitted locally only.
- Seven registry acceptance IDs and one host deadline ID: standalone hermetic runners not selected.
- Real pinned Node container smoke: NPM_ENV_REAL_DOCKER=1 not selected.

Counts are observations, not a numeric allowance. No source/skip-policy/test-collection change occurred, and the full suite matches the P6 exit counts.

## Blocker P7-CI-ENV (7.13)

The workflow sets **PRIVILEGED_HELPERS=1**, provisions sudo/runuser/docker-dev/nobody, independently runs `scripts/check-types`, then executes the complete gate with **GREEN_CHECKS_WORKERS=2**. Here `command -v sudo` and `getent passwd docker-dev` each returned **1**, `/usr/sbin/runuser` exists, `id` reports unprivileged dev, and PRIVILEGED_HELPERS is unset. The local gate legitimately skipped four privilege cases that CI must execute. Existing Python/Node/npm tools are provisioned, but the complete CI privilege environment is not.

No CI-equivalent success is claimed. Merely changing worker count cannot satisfy this gap. No failing CI-mode run was manufactured after identifying the missing prerequisites. No remote GitHub run was executed or independently observed; historical user-reported P6 runs do not prove this final revision. Full hosted provisioning cannot be inferred from local success.

**Required next step:** provide a provisioned environment meeting the workflow prerequisites, or current hosted execution evidence bound to the candidate revision, then execute/verify the CI-equivalent type and full-gate commands. Preserve actual statuses and review the nine expected opt-in skips rather than accepting missing privilege prerequisites. Ask the user before changing the agreed acceptance scope.

## Decision (7.14)

**BLOCKED: P7-CI-ENV.** Local final G and tasks 7.1–7.12 pass for the recorded source/control fingerprint. Task 7.13 and final acceptance 7.14 remain unchecked; overall progress is **115/117**. This is not final migration acceptance or archive authorization.

No staging, unstaging, reset, restore, commit, branch change or archive was performed by this session. Read-only history/diff inspection did not modify the index. Historical reports were not edited.
