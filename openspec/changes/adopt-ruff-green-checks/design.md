# Design

## Context

See proposal.md for motivation and scope. `pyproject.toml` currently declares Python >=3.14 and ty configuration, with no development dependency group. `scripts/check-types` checks `docker` and `tests/typing`; the green-loop configuration also names a production ty command. GitHub Validation currently runs type checking on push and pull_request. The descriptor/process migration is archived; its ownership contracts and enforcement tests remain binding.

This cross-cutting adoption needs a design because formatting, import fixes, source-inspecting tests and CI must migrate without concealing behavioral changes.

## Goals / Non-Goals

**Goals:** reproducible tool versions, identical source coverage locally and in CI, reviewable migration boundaries, and a green-check rule that cannot be satisfied by omitted files or suppressed failures.

**Non-Goals:** new runtime tools, inventory/schema changes, uv project/lockfile migration, unsafe automatic rewrites, new architectural lint rules, removal of lifecycle guarantees, automatic commits/hooks, and GitHub branch-protection administration.

## Decisions

### 1. Development-only pin and explicit policy

Declare `ruff==0.17.1` in `[dependency-groups].dev`, configure Ruff's required version to the same version and Python target `py314`. Explicitly select `E4`, `E7`, `E9`, `F`, `I`, `B`, `SIM115`, `RUF100`; do not enable ALL, preview or unsafe fixes. Use the stable formatter defaults (including 88-character target) rather than inventing a second style. The formatter's target is not an E501 requirement.

Use a documented project virtual environment and `uv pip install --python .venv/bin/python --group dev` for development installation; verify the command with the available uv. CI installs the declared group into its selected Python environment, alongside the existing pinned ty and explicitly declared test-runner prerequisites. Keep one Ruff version declaration authoritative for installations; do not introduce a separate drifting CI pin. Do not add Ruff to `docker-constructor.toml` or the Docker image inventory: project tooling is not an image capability.

Alternative: install an unrecorded global Ruff or migrate every dependency to a new lockfile. Both add drift or unrelated scope.

### 2. One shared source inventory

Discover all `.py`/`.pyi` files under `docker/`, `tests/`, `scripts/`, plus extensionless Python scripts identified by their Python shebang. Inventory any Python script not identifiable this way explicitly. Use the same deterministic inventory for formatting, linting and compilation, with coverage tests for an extensionless Python script, shell-script exclusion, nested Python files and filenames containing spaces. Pass extensionless files explicitly to Ruff; do not depend on its default recursive discovery. Newly added files in these trees must be included automatically or fail inventory validation.

Exclude `.venv`, caches, generated external content and `openspec/**` historical evidence; do not broadly exempt product/test files. The check runner must honor repository Ruff configuration and reject any discovery/exclusion mismatch rather than silently skipping inventoried files.

Compilation checks each selected file with its filename and proper source encoding, without importing/executing application code or leaving bytecode in source trees. `compileall` alone is insufficient for extensionless scripts. Compilation is a syntax check, not an import or behavioral proof.

### 3. Preserve semantic enforcement before formatting

Audit `tests/enforcement/` on the final archived migration baseline. Current rules do not replace ownership/dataflow, process-budget, public API, dependency DAG, import isolation, inventory or executable-documentation contracts. `I` sorts imports; it is not architectural dependency enforcement. SIM115 checks context-managed file opening, not ownership transfer or primary-error preservation; retain the corresponding lifecycle enforcement tests. Review its findings under the same fix/simplify/explained-suppression policy as other diagnostics. TID251, S603 and S607 are not enabled by this change.

Record an overlap table with concrete tests and Ruff rules. Delete a check only if equivalent coverage, including its negative fixtures, is demonstrated; otherwise retain it. No deletion quota exists. Replace line-number or textual-layout coupling (for example `descriptors.py:239` in secure-walker expectations) with equivalent structural checks in a preparation change, retaining mutation rejection and uniqueness guarantees. Preserve Python examples embedded in documents; update example expectations only where equivalent source presentation requires it, never by weakening lifecycle semantics.

### 4. Separate migration stages

`tasks.md` is the binding execution/acceptance contract. It decomposes these stages into seven phases: P1 baseline/enforcement preparation, P2 shared tooling, P3 formatting, P4 safe autofix, P5 manual resolution, P6 permanent control and P7 final acceptance. Their prerequisite DAG is the forward-only chain P1 → P2 → P3 → P4 → P5 → P6 → P7. Each phase owns RED → GREEN → INTROSPECT → VALIDATE work, named deliverables and an exit gate; missing evidence blocks closure. Data-dependent repairs use separate, individually checkable work-order instances with fixed single-target acceptance criteria, rather than a batch task to fix all findings. RED records actual gaps rather than injecting artificial failures. Earlier-phase repairs invalidate affected downstream evidence and require topological revalidation, not backward dependencies.

1. Tool/configuration setup, shared file coverage, baseline diagnostics/skips, and brittle-enforcement preparation. Run full pytest, existing type checks and compilation; Ruff findings are captured as migration backlog, not called green.
2. Run only `ruff format` on the inventory. This must be a separate formatting-only commit boundary: no lint fixes, import sorting, handwritten refactoring, test repair or configuration edits. Verify formatter idempotence, unchanged ASTs ignoring locations, full pytest, existing type checks and compilation. If source-sensitive tests fail, return to preparation rather than smuggling repairs into the formatting diff.
3.1 Run safe `ruff check --fix`, then reformat only as needed. Record the exact command and remaining diagnostics. Review changed imports for registration/side effects and review every semantic fix; run full pytest, type checks and compilation. A nonzero Ruff status from remaining findings is explicitly unresolved until 3.2, not successful verification.
3.2 Resolve each remaining diagnostic: fix defects with regression tests, simplify equivalently, or use specific `# noqa: CODE` with a rationale explaining the required construct. No blanket `noqa`, broad per-file/rule exclusions or disabling rules to manufacture success. RUF100 removes obsolete suppressions; human review checks explanations because RUF100 alone cannot enforce their quality. Reformat and run all checks.
4. Activate permanent guidance and CI after the backlog is zero. Keep setup, formatting, automatic lint fixes, manual fixes and permanent enforcement distinguishable in review. Do not stage or create commits without explicit permission.

### 5. Green-check contract and CI

Add the mandatory rule to AGENTS.md and link a concise contributor-check guide from all maintained READMEs. After every major change and before any commit, the final candidate state must have:

- zero unresolved Ruff diagnostics and `ruff format --check` success on the complete inventory;
- zero unresolved ty diagnostics in all current configured type-check commands; do not narrow their scope;
- successful syntax compilation of the complete inventory;
- complete `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` success, with only justified skips.

The check manifest separates P1-discovered `existing-required-checks`, mandatory throughout, from P2-added `new-adoption-checks` with explicit activation phases. Shared compilation must pass from P2 onward (P1 uses its one-off compilation command); full-inventory format checking must pass from P3 onward; full-inventory Ruff lint must pass from P5 onward. Earlier diagnostic failures are recorded with their actual statuses as migration backlog, never as passes. Phase validation executes all checks mandatory for that phase, not every manifest entry unconditionally; final acceptance executes every command in both sections. These activation rules preserve the phase order and do not permit postponing existing required checks. Full green acceptance is mandatory from manual resolution (P5) onward. Capture full pytest stdout/stderr temporarily for diagnosis and preserve the actual runner exit status. Durable evidence is a self-contained summary of source state, exact commands, environment, statuses, counts, significant warnings and limitations; permanent full logs are not required, and temporary container paths must not serve as durable evidence links. Any nonzero verification status blocks green acceptance. Run all existing configured checks as well; if unittest discovery remains a required configured check, run it rather than substituting pytest.

Inventory existing skips with reasons and conditions. A skip is acceptable only for documented environmental prerequisites or genuine non-applicability; no new skip/exclusion/xfail may hide a regression. Audit the full run's skip reasons locally and in CI, including unexpected increases; do not freeze a numeric count that varies legitimately by platform. Unexpected skip reasons block acceptance until explained.

Extend `.github/workflows/validation.yml` rather than creating a competing policy. Preserve push and pull_request triggers and existing type checks. Add non-mutating lint/format, compilation and the full pytest run using Python 3.14 and a reproducible test environment. Give checks clear statuses, retain diagnostic output temporarily or in retention-limited artifacts even on failure, and use always-run artifact upload where needed. Keep durable result summaries self-contained; do not require permanent full-log retention. CI must never run fix/format-write commands or commit changes. A push checks the pushed tip, not every local intermediate commit; duplicate push/PR runs remain acceptable initially. Required branch-protection checks are a separate repository setting, not promised by this change.

## Risks / Trade-offs

- Mass formatting obscures defects → dedicated format-only boundary, AST comparison and full tests.
- Import sorting changes registration or execution → inspect safe autofix diffs and exercise affected paths; use explained narrow suppressions when order is intentional.
- Ruff cannot validate explanation quality or ownership semantics → retain human review and enforcement tests.
- Test/CI dependencies or external prerequisites are incomplete → inventory them before activation; provision required prerequisites, document justified environment skips, never trim the suite.
- Historic source evidence becomes invalid → do not rewrite OpenSpec archives or verification artifacts.

## Migration Plan

Implementation may start on a separate explicit request; the predecessor is already archived. Complete the stages above in order, recording commands, statuses, skip inventory, diagnostics and enforcement-overlap evidence in separate verification files. At the end verify local/CI parity and demonstrate that intentional lint, format, syntax and type errors fail their respective gates. Revert only the relevant adoption changes if needed, preserving the archived lifecycle contracts; do not reset unrelated work. Commit boundaries describe review intent, not authorization to stage or commit.
