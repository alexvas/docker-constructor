# Phase 10 — Rollout and complete validation

**Change:** `add-durable-filesystem-transactions`

## RED and GREEN evidence (10.1–10.5)

- `python -m unittest tests.test_transactions_phase10_rollout` initially failed with three `FileNotFoundError` errors because the approved cutover document did not exist.
- Published `docs/development-build-cache-cutover.md` with one project-resolved Python instruction. It resolves `ProjectState.build_artifacts_root`, checks the exact `build-artifacts` basename, and removes only that subtree. The document explicitly rejects wildcard, shared-cache-root, XDG-root, and project-namespace removal.
- The focused documentation suite then passed: 3 tests.
- Existing final legacy and generation tests passed, including legacy-name namespace exclusion, no-open behavior, and preservation during generation publication.
- Existing final orchestration tests passed, including operational exit code 4, all-failure diagnostics, recovery-before-materialization/Docker ordering, and post-commit image/generation preservation.
- No Phase 10 production wiring was required; the asserted behavior was already connected by Phase 5 and remained within the approved L0–L3 boundaries.

## Complete-change review (10.6)

Reviewed production legacy-name references, generation/cleanup authority, stale journal/envelope assumptions, compatibility paths, task/spec/design alignment, and unfinished markers.

Findings:

- Production references to `committed-build.json` are limited to documentation and the `LEGACY_MANIFEST_NAME` declaration in `build_generations.py`; discovery classifies only the hyphenated immutable-generation namespace. No runtime migration, adoption, rejection, or deletion path exists.
- Build cleanup derives candidates from validated `previous - current`, validates retained descriptors before unlink, batches directory synchronization, and retains predecessor authority until candidate durability completes.
- No generic journal/envelope, generic VFS, or generic transaction authority was introduced.
- No `TODO`, `FIXME`, or `NotImplemented` marker remains in the reviewed transaction/build-generation/build-cleanup/orchestration modules.
- Phase 10 required documentation and tests agree with the proposal, design, and specifications. No production-code finding required correction.

## Final validation (10.7)

All required configured checks passed:

- `npx pi-green-loop detect` — detected the repository's configured production type check and complete unittest suite.
- `npx pi-green-loop check` — PASS: `ty check docker --python-version 3.14 --output-format concise` (114 ms); PASS: `python -m unittest discover -s tests -p 'test_*.py'` (102.073 s).
- `openspec validate "add-durable-filesystem-transactions" --type change --strict --json` — valid, zero issues.
- `git diff --check` — clean.
- Focused Phase 10 suite — 3 tests, PASS.

No formatter or lint tool is configured for this repository; the configured validation surface consists of `ty` and unittest, consistent with prior phase evidence and `pi-green-loop.json`.

## Development cutover and representative flow (10.8)

Executed the documented instruction from this project root. It resolved and removed exactly:

`/home/dev/.cache/docker-constructor/projects/docker-constructor.git-a65ee5d7729e0445/build-artifacts`

The basename guard passed and removal completed successfully. The shared cache root and project namespace were not selected.

Representative restart flow then passed:

- first/successful build returns to one stable generation;
- changed-build post-commit aggregate failure preserves the successful image/new generation and recovery evidence;
- restart recovery completes a two-generation state to one stable generation.

Command: focused three-test unittest run; result: 3 tests, PASS. The change is ready for archive after final status confirmation.
