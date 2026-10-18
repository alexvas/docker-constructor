# Design

## Context

See proposal.md for motivation and the two delta specifications for acceptance behavior. Current ownership wrappers already provide terminal release and shared cleanup precedence, but application functions still exchange raw integers, `.fd` attributes and fd-backed file objects. The existing complete-tree gates and focused fault/real-pipe tests remain valuable; the cancelled Phase 7.2 universal event journal is not a prerequisite or a deliverable here.

This is a cross-cutting, explicitly breaking migration of internal Python ownership APIs. The current close-migration change can finish with its remaining gates independently. Its narrow-foundation API baseline remains valid for its own acceptance; this change explicitly authorizes a subsequent bounded expansion and updates that baseline with its consumers.

## Goals / Non-Goals

**Goals:** make raw-fd escape mechanically forbidden; express responsibility with a small number of composable forms; mechanically prove common normal/exceptional finite exits; expose honest coverage and review status; preserve observed domain semantics and existing lifecycle safety.

**Non-Goals:** prove arbitrary program termination, successful OS close, cleanup after abrupt process death, arbitrary thread cancellation, general Python linear typing, or reconstruct one global runtime descriptor history. Do not add a generic VFS, callback-with-fd escape, domain policy or process/reader coordinator to filesystem.

## Decisions

### 1. Close the raw-descriptor boundary, including acquisition

Extract the runtime into an independent Python library project in the same Git repository: `libs/resource-ownership/`, distribution `resource-ownership`, import package `resource_ownership`. Use explicit filesystem and process entry points and separate internal layers. This is a separate build/install/dependency boundary, not another subpackage of the application. Its filesystem layer supplies owners, factories, operations, borrowed views, transfer and cleanup; its process layer supplies the existing generic child/reader coordination. Move existing `docker.filesystem`/`docker.process` implementations into this boundary in migration batches; compatibility facades may delegate only approved public contracts. Transaction layers retain typed error translation and domain protocols. The architecture analyzer stays outside the runtime package, under enforcement/tooling, and is not a runtime dependency.

Application code passes resource owners or restricted borrowed views, never integers originating from descriptor acquisition/extraction. The application raw-fd argument/return/storage prohibition does not apply to calls between runtime implementation functions: those functions may pass and store low-level primitives. This is a deliberate trust boundary, not a claim that their lifecycles are proved by the application analyzer. The package's own fault/ownership/API tests verify internal acquisition, transfer and release.

A source-root/public-export manifest separates runtime implementation, public boundary, consumers, compatibility facades and tests. Origin/API/dependency checks reject raw exports, private consumer access, moving domain code inside the package to evade checking, or application acquisition hidden behind facade aliases. All production sources remain inventoried; runtime-internal sites have a separate classification rather than consumer escape violations. Test backends may inject raw integers without creating an application exemption. New application modules and aliases are scanned automatically.

Factories prepare fallible arguments before acquisition, immediately establish a sole owner internally and return that owner. Temporary creation returns an owner plus its pathname, not an fd tuple. Directory-child APIs return owners/capabilities; duplication returns a new owner of a distinct descriptor. No application-visible detach returns an integer. Low-level constructors/accessors remain available for runtime implementation and test interoperability; application callers must migrate away from them.

Alternative: allow application `os.open` followed immediately by adoption. Rejected as the target architecture because it retains a gap and makes every consumer participate in raw-fd validation.

### 2. Expose mechanisms, not domain decisions

Extend owner/capability operations with `read(size) -> bytes`, `write(data) -> int`, and `stat() -> os.stat_result`. Preserve short-read/write behavior; do not silently turn write into write-all. Inventory the existing need for descriptor-relative operations, synchronization, mode changes, locking mechanisms, duplication and readiness before fixing the remaining finite API catalog. Every added primitive must correspond to an inventoried use and have a documented signature, error contract and live-authority check. Additional domain behavior requires a scope decision, not an arbitrary callback escape.

Buffer wrappers are created and cleaned up inside filesystem with `closefd=False` over its retained descriptor owner. The outward wrapper capability offers data operations, not `fileno`, raw stream access, context callbacks receiving fds or independent release authority. Native already-fd-owning streams keep one stream owner, never a second raw-fd owner.

The process layer inside the runtime package keeps its coordinator, threads, clocks, policies and budgets. Its internal pipe fileno/nonblocking/readiness handling may use raw primitives under its own ownership/barrier tests; raw numbers must not escape the package's public boundary. Shared filesystem mechanisms may be factored into capabilities where useful, but the application escape rule does not require wrapping every internal syscall. A readiness timeout is a low-level operation parameter, not filesystem authority over shutdown deadlines or reader scheduling.

Alternative: move application validation/hashing/publication into filesystem to avoid fd exposure. Rejected: consumers compose operations and retain their domain decisions and error mappings.

### 3. Define responsibility and borrowing separately

Supported forms and the evidence expected from each:

| Form | Local evidence | Discharge |
| --- | --- | --- |
| lexical | approved context scope or recognized protected cleanup | release on each supported finite normal/exception exit |
| transferred | declared consumes/returns contract and explicit acceptance | source authority invalidated; receiver responsible |
| managed | exception-safe registration in a verified cleanup container | container attempts every independent release |
| deferred | known lifecycle engine retaining original authorities | incomplete observation until safe attempts/outcomes settle |
| unproven | unsupported structure with precise diagnostic | valid review record or CI failure |

Use explicit owning/borrowing interfaces and ownership-effect metadata for factory returns, consumed parameters and borrowed parameters. Ordinary Python type annotations alone are not evidence of transfer. Borrowed views carry no close, move, detach or descriptor-extraction API; they validate the original authority and cannot outlive release/transfer. Do not serialize every operation or promise general data-race freedom; existing process barriers remain responsible for concurrent reader safety.

An accepted transfer has one authority throughout. Failed receiver acceptance cannot strand a resource; successful transfer invalidates the source authority, including its aliases/views. Returning or passing the same mutable Python object without such a contract is not a mechanically proven move. Tests must cover receiver failure and alias use after transfer.

Managed storage supports constructor failure and registration failure, not just normal close. Its responsibility chain must reach a protected application root. Bare fields, class names, a close method and unreachable close calls are not proof. Resource collections must use verified container operations rather than arbitrary owner lists plus unprotected loops.

### 4. Use a bounded static checker, not a universal verifier

Build the enforcement extension under tests/enforcement, reusing existing origin/dependency inventories. Track resource-origin values and owner identities through supported imports, assignments, function contracts, fields and collection/closure/callback escapes. A bounded intraprocedural control-flow pass checks normal/exceptional exits, protected registration and transfer; summaries at declared call boundaries avoid exploring arbitrary whole-program execution.

Support explicit scope/try/finally/return/raise/branch patterns first, with loops validated conservatively at ownership boundaries rather than proving iteration termination. Unknown callees, dynamic dispatch, reflection, unresolved resource aliases and unsupported control flow become precise unproven findings, never inferred safety. They may be accepted only through the reviewed category below. Static proof assumes control reaches the relevant exit and relies on tested contracts of the small lifecycle library.

Adversarial fixtures must include newly added modules, alias chains, attribute/container/closure/generator/callback escapes, swallowed exceptions, close after fallible work, conditional cleanup, duplicate authorities, use after transfer and fake owner/container names. Positive fixtures exercise each accepted form. The target is complete inventory and honest classification, not zero reviewed cases at any cost.

Alternative: raw integer type detection alone. Rejected because most integers are not fds and aliases/containers hide resource origins. Alternative: universal global dynamic fd journal. Rejected because test execution cannot prove unexecuted paths and the instrumentation becomes a separate large system.

### 5. Keep reviewed acceptance explicit and change-sensitive

Generate a deterministic report with site, resource origin, ownership form, responsible root/receiver, evidence and status: automatic, library-owned, reviewed, unresolved or stale-review. Deferred is an ownership form, not automatic evidence of eventual release. Review is a distinct acceptance category, not another raw-close exemption.

Versioned review records include exact affected symbols, normalized source fingerprints, relied-on contract/dependency fingerprints, rationale, responsible cleanup root and focused tests, plus explicit reviewer approval. An LLM may prepare or assist a review; an unapproved LLM verdict is not acceptance. Changes to the source or relied-on ownership summaries invalidate the record. Do not use only line numbers, blanket package exemptions or a whole-repository hash that invalidates unrelated reviews. CI rejects unresolved and stale records while reporting valid reviewed cases separately from automatic proofs.

### 6. Preserve specialized runtime validation

Prove terminal ownership, ordinary failure precedence and independent cleanup in filesystem tests. Retain actual subprocess setup/binding, simultaneous pipe, real-reader stop, retained continuation, concurrent winner publication and fd-reuse tests in process. Add fault tests at every new acquisition/transfer/wrapper branch; static scope syntax alone does not prove these internals.

No mandatory global event journal is introduced. Focused event ledgers and synthetic oracle fixtures can remain useful tests, but are never represented as evidence that all real production acquisitions were observed.

### 7. Independently owned tests and change-based selection

Create the library's own test root, `libs/resource-ownership/tests/`, containing its unit, fault, concurrency, real-pipe and public contract tests. It must run without collecting consumer suites, importing application-domain fixtures, or depending on their private modules. Shared fakes live inside that test root or a neutral test-support layer, never in consumer test modules. Existing runtime tests migrate with their implementation. Consumer tests use only the public runtime API; tests of application integration and the analyzer remain separately owned.

Define and test a change-to-suite manifest/selector with these outcomes:

| Changed boundary | Required development checks |
| --- | --- |
| runtime implementation only; public contract unchanged | runtime's own suites, including its public contract tests; no unrelated consumer suites |
| one or more consumers only | affected consumer suites and their boundary checks; no runtime unit/fault suites |
| public runtime contract | runtime suites plus dependent consumer contract checks and directly affected integration suites |
| analyzer or selection tooling | its own suites plus affected architecture/selection fixtures; no implicit full runtime/consumer collection |
| cross-boundary or unclassifiable change | union of affected checks; conservatively include dependent contracts when compatibility is uncertain |
| release | full project integration and existing release gates regardless of selected development checks |

A contract includes exported names, types/signatures, exceptions, ownership/borrow/transfer effects and documented behavior. Maintain contract metadata and executable compatibility cases; an unchanged signature is not evidence of unchanged semantics. Explicit behavioral contract changes must trigger dependent checks, and ambiguous public-boundary changes default to the broader selection rather than trusting a missing version bump. This is bounded selection, not a proof that arbitrary implementation edits preserve behavior: the library's own public contract suite always runs for internal changes.

Verify both the planned selections and actual test collection, including no consumer imports/collection for runtime-only edits and no runtime unit collection for consumer-only edits. Selection tests cover implementation-only, consumer-only, public signature/error/effect/semantic changes, simultaneous changes, neutral support changes, unknown paths and release override. Keep full stdout/stderr and status for actual selected runs. Project virtual environment and eight pytest workers apply equally to scoped suites; explicit unittest discovery can target the runtime root during development and the full tests root at release.

Use this monorepository layout:

```text
libs/resource-ownership/
  pyproject.toml
  src/resource_ownership/
    filesystem/
    process/
  tests/
docker/                         # application, consumes installed library
tests/                          # application/integration/analyzer tests
```

The library owns its build backend configuration, dependency and supported-Python declarations, test/typecheck configuration and isolated environment. Build a wheel from the library project alone and run its tests against that wheel from outside the application checkout, without application imports or PYTHONPATH/source-tree injection. Check wheel contents and runtime imports, including the image layout. An sdist/build-input check must ensure the wheel can be reproduced without undeclared files from the parent repository. `src/` layout prevents accidental imports of an uninstalled sibling package.

Application installation declares and installs the library dependency through a reproducible repository-local workflow; editable installation is permitted for development, not the sole acceptance path. Root test collection must not recursively collect library tests, and library configuration must not inherit application conftest/plugins or private fixtures. Each project owns its environment and test dependencies; document both installation and independent commands. Keep version/metadata and any shared dependency inputs in the test-selection manifest so compatibility-affecting edits trigger dependent checks. No mandatory new workspace tool, PyPI publication, separate Git repository or independent release cadence is introduced.

Document dependency direction: application → installed public library API; internal process layer → internal filesystem layer; no reverse domain dependencies. The analyzer consumes source/contracts without being imported by the runtime. Architecture inventories explicitly include the new library source root; the application raw-fd ban does not apply inside it, while API/lifecycle/dependency checks still do.

### 8. Preserve independent delivery boundaries

Fully encapsulated `subprocess.run` stays library-owned. Caller-supplied streams still need an owner. Consume the native-file and HTTP/probe changes' actual interfaces where relevant; do not reinstate the rejected staged proxy or silently implement those changes here. If their contracts conflict, pause for reconciliation. No requirement to migrate synchronous subprocess clients or add a runtime-installer process dependency is introduced.

## Risks / Trade-offs

- [API migration broadens a lightweight library] → finite operation catalog tied to actual consumers, executable API/dependency checks, domain-policy negatives.
- [Static analysis appears stronger than it is] → explicit supported subset, automatic/reviewed separation and no termination claim.
- [Transfer metadata lies] → runtime source invalidation, failed-receiver tests and executable implementation checks for declared effects.
- [Managed close exists but is never rooted] → trace responsibility summaries to a protected or explicitly reviewed root.
- [Review records become a permanent escape hatch] → narrow source/dependency fingerprints, rationale/tests/approval and CI rejection of stale records.
- [Migration changes errors or data] → characterize exact domain failures/causes, bytes, paths, modes, ordering, lock scope, packaging and process budgets before edits.

## Migration Plan

1. Establish the independent library project's pyproject/src/test/build/environment boundary, application installation dependency, public-contract manifest and change-selection fixtures. Inventory actual primitive needs, raw boundary crossings, owning/borrowing contracts and roots across the full production tree, separating permitted runtime internals from application escapes. Preserve separate external-library and delegated-family classifications.
2. Add RED architecture/escape/finite-exit/review fixtures and baseline domain characterization. Publish the finite API/contract catalog before consumer edits.
3. Implement owner factories, operation and borrowed interfaces plus safe moves and managed storage. Test each fault and handoff boundary; keep old APIs only as temporary internal migration seams.
4. Migrate runtime implementations and their tests into the ownership package, then transactions/versioning, npm/runtime and process callers in explicit batches with no public raw-fd compatibility escape at final acceptance. Update package/API/example baselines together; keep internal raw primitive passing allowed, with internal lifecycle tests.
5. Generate complete proof/review coverage, document limitations, and validate root responsibility and specialized lifecycle tests. Remove obsolete application raw interfaces and reconcile guidance.
6. Verify scoped test selection and isolation for library-only, consumer-only and public-contract changes. At release explicitly run both projects' pytest suites with eight workers and unittest discovery roots: application-root `tests` no longer includes library tests. Typecheck both source roots, build/install/test the library wheel and sdist independently, then run application integration, isolated runtime-image imports, documentation examples, diff checks and strict OpenSpec validation. Store implementation evidence separately.

Rollback is source-only: revert consumer batches before removing new owner mechanisms, retaining earlier terminal-release fixes. Persistent formats and external command behavior do not change.
