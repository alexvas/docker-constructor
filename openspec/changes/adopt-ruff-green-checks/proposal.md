# Proposal

## Why

The repository has type and lifecycle checks but no shared Python formatting/lint policy or complete pre-commit green-check contract. Introduce reproducible Ruff checks and independent CI verification without mixing mechanical formatting with defect fixes or weakening lifecycle enforcement.

## What Changes

- Declare Ruff exactly `0.17.1` as a development dependency and document reproducible local/CI installation, without changing the image's managed tool inventory or runtime dependencies.
- Cover product/test Python under `docker/`, `tests/`, and Python scripts under `scripts/`, including extensionless scripts. Enable `E4`, `E7`, `E9`, `F`, `I`, `B`, `SIM115`, and `RUF100`.
- Separate adoption into tool setup and enforcement-test preparation, a formatting-only commit, safe `ruff check --fix`, and manual resolution of remaining diagnostics. No unsafe bulk fixes.
- Permit only specific `noqa` codes with explanations of why the construct is necessary; remove obsolete suppressions. Fix real defects with tests and simplify awkward constructs equivalently.
- Preserve `tests/enforcement/` contracts. Audit overlap with Ruff and brittle source-position assertions; remove checks only with demonstrated equivalent coverage, including negative fixtures.
- Add mandatory green-check guidance in `AGENTS.md`, with commands and links in maintained READMEs: after each major change and before committing, Ruff and ty diagnostics must be resolved, all scoped Python must compile, and the complete test suite must pass except justified skips.
- Extend the existing GitHub Validation workflow on push and pull requests with non-mutating Ruff checks, compilation, current type checks and full pytest. Preserve complete check output and actual failure statuses.

## Capabilities

### New Capabilities

None. This is development tooling and contributor-process work, not a new product capability.

### Modified Capabilities

None. `skip_specs: true` deliberately omits delta specs; existing runtime, descriptor, process and domain behavior remains authoritative and unchanged.

## Impact

Affected surfaces: `pyproject.toml`, local check entry points, `pi-green-loop.json`, `.github/workflows/validation.yml`, `AGENTS.md`, maintained README files, and scoped Python sources. Historical OpenSpec verification artifacts are excluded from mass formatting and linting. No dependency-manager migration, runtime tool inventory extension, pre-commit hook, branch-protection configuration or automatic commit is included.

`eliminate-unsafe-direct-descriptor-closes` is complete and archived at `openspec/changes/archive/2026-10-18-eliminate-unsafe-direct-descriptor-closes/`; its completion is not an outstanding prerequisite. Planning creates no implementation edits or commits. Future commits require explicit authorization.
