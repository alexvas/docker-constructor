# P7 final acceptance

**Status: ACCEPTED. Tasks 7.1–7.14 complete; overall 117/117.** The user explicitly confirmed successful CI acceptance and authorized completion of 7.13 and 7.14. No archive or commit is authorized or performed.

## Source binding and freshness

Current revision: `645b7c2576f4fc4e44793e72d7095bd0ecdeaa3c`. Worktree was clean before this evidence-only continuation. The final source/control manifests from [phase-7.md](phase-7.md) still match:

- 408 Python sources; manifest SHA-256 `889e41489789013b6f525550c43161cea48b9d78637d4ca869bbc2ae6bfa7ff8`.
- 13 controls/configuration/documentation files; manifest SHA-256 `c01d6d53c5b04b6d2b4eafa5f4e52a258d26924e7d4a1b301bce7e563ec64ec9`.

Fresh read-only commands:

```bash
git status --short
git rev-parse HEAD
git diff HEAD -- docker tests scripts .github/workflows/validation.yml pyproject.toml
sha256sum -c openspec/changes/adopt-ruff-green-checks/verification/p7-source-after.sha256
sha256sum -c openspec/changes/adopt-ruff-green-checks/verification/p7-controls-after.sha256
```

All exited 0; status and source diff were empty; every manifest entry matched. Full hash-check output was captured temporarily. No source edits invalidate P7's previously executed local G: **8226 passed, 13 reviewed skips, 11 warnings, 18028 subtests**, with successful lint, format, compile and types. That local run remains evidence for its recorded unchanged source state, not a newly executed run.

## Hosted acceptance (7.13)

The user reports successful CI acceptance and supplies the pytest output:

- CPython **3.14.8** on the hosted runner.
- **8230 passed, 9 skipped, 11 warnings, 18028 subtests passed** in **213.04 s**.
- Nine explicitly listed skips: seven registry standalone-runner cases, one host standalone-runner case and the real pinned Node-container opt-in. All match the reviewed applicability conditions; none is a privilege-prerequisite skip.
- Eleven unsuppressed multiprocessing/fork DeprecationWarnings: four in build generation integration, three in transaction locking and four in build transactions.

The four additional passes and four fewer skips relative to local P7 are consistent with executing the four privilege-dependent cases. No unknown skip, xfail/xpass or failure is reported in the supplied excerpt.

**Provenance and limits:** hosted success is user-reported, not independently retrieved by the assistant. The user explicitly accepts tasks 7.13/7.14; no run URL/ID or hosted checked SHA was supplied. The excerpt does not separately show lint/type/format/compile statuses or a numeric runner exit code; no such details are invented. The workflow's required commands remain documented in phase-7.md and the workflow itself. Current source/control equivalence is independently checked above; the hosted run's exact SHA association is not independently established.

## Final decision (7.14)

The user's explicit hosted acceptance resolves **P7-CI-ENV** for the remaining tasks. P1–P6 deliverables/exits are indexed in [p7-contract-matrix.md](p7-contract-matrix.md); final boundary/scope reviews, local G, formatter idempotence and skip review are recorded in phase-7.md for the matching fingerprint. All P1–P7 work orders/exits are accepted, with the hosted provenance limitation disclosed above. Earlier BLOCKED text is historical, not a current unresolved item.

Only evidence and task completion boxes change here. No staging, unstaging, reset, restore, commit, branch change or archive was performed. Archive remains a separate user decision.
