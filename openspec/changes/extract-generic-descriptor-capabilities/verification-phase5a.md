# Phase 5A Verification Record — Typed Error Preservation Across Consumer Boundaries

Date: 2026-10-07

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 5A only. Phase 6 and later remain unstarted.

## Deliverable

Transaction and lock errors remain typed across L2, cleanup, and domain
boundaries. A consumer preserves the typed failure unchanged when no domain
translation is required, and when a domain error is required it chains
directly from the typed failure (`raise DomainError(...) from exc`) rather than
from `exc.cause`. An executable cause-access inventory and an AST guard
distinguish legitimate cause inspection (absence, errno, no-follow safety,
domain-result selection) from cause replacement. Raw `OSError` remains
authoritative only before capability adoption and at directly injected POSIX
operations; the raw-contract exception inventory is empty.

## Production files changed

```text
docker/npm_environment/publication.py  # _identity_lock_failure preserves typed LockError;
                                        #   release accumulator classifies LockError directly
docker/versioning/artifact_cache.py     # _identity_lock_failure preserves typed LockError and
                                        #   typed TransactionError; release accumulator typed
docker/versioning/build_cache.py        # _raise_lock_failure / _raise_marker_failure /
                                        #   _validate_existing_blob preserve the typed wrapper;
                                        #   build-lock release accumulator typed; acquisition
                                        #   cleanup no longer demotes raw OSError, and the
                                        #   lock __exit__ treats only LockError / close-stage
                                        #   TransactionError as ordinary; absence
                                        #   inspection selects the cleanup outcome
docker/versioning/build_cleanup.py      # _open_algorithm_directory aggregates the typed failure
docker/versioning/project_state.py      # _publish_metadata chains the domain error from the
                                        #   typed construction/validation failure
docker/versioning/rendering.py          # _raise_effective_failure and the generated-directory
                                        #   factory chain the domain error from the typed failure
docker/versioning/effective.py          # create_runtime_projection wraps typed operational
                                        #   TransactionError failures in EffectiveConfigError;
                                        #   capability misuse / no-follow safety keep
                                        #   CapabilityError unchanged
```

No public CLI, cache layout, npm identity, publication format, lock policy, or
durability contract changed. No stage value changed and no new transaction
aggregate export was added.

## Test files changed

```text
tests/test_transactions_phase5a_typed_preservation.py   # new, 67 tests (tasks 5A.1-5A.7, 5A.12-5A.14)
tests/test_transactions_l1_error_boundary.py            # factory/lock mappers now preserve typed wrappers
tests/test_transactions_phase6_effective_projection.py  # domain wrapping + typed ownership split
tests/test_transactions_phase6_project_metadata.py      # typed publication cause chain
tests/test_transactions_phase7_runtime_projection.py    # EffectiveConfigError domain chaining
tests/test_transactions_phase7_runtime_lock.py          # typed lock + pre-adoption raw boundary
tests/test_transactions_phase8_npm_lock.py              # typed lock preservation
tests/test_transactions_phase9b_remap_boundaries.py     # no cause remapping in domain modules
tests/test_constructor_build_cleanup.py                 # typed aggregated validation failure
tests/test_constructor_build_generation_integration.py  # typed lock/marker/unlink expectations
tests/test_constructor_build_persistence.py             # typed replacement domain error
tests/test_constructor_runtime_lifecycle.py             # EffectiveConfigError on projection failure
```

## Executable cause-access inventory (task 5A.1)

Every detected `.cause`/`.__cause__`/`getattr(..., "cause")` site in
`docker/**` is classified. After the GREEN changes the detected inventory is:

| File | Function | Classification |
| --- | --- | --- |
| `docker/transactions/errors.py` | `TransactionError.__init__` | definition (Store, not a consumer) |
| `docker/transactions/capabilities.py` | `DirectoryCapability.from_secure_path` | typed-propagation (builds a new typed wrapper and chains the cause) |
| `docker/versioning/build_cache.py` | `_raise_lock_failure` | inspect-only (`exc.cause is None`) |
| `docker/versioning/build_cache.py` | `_validate_existing_blob` | inspect-only (`FileNotFoundError` absence result) |
| `docker/versioning/build_cache.py` | `_validate_existing_marker` | inspect-only (`FileNotFoundError` absence result) |
| `docker/versioning/build_orchestration.py` | `_exception_chain` | inspect-only (chain walk) |
| `docker/versioning/build_orchestration.py` | `_format_build_cleanup_diagnostic` | inspect-only (chain walk) |
| `docker/versioning/host_progress.py` | `_FailureContextRegistry.lookup` | inspect-only (chain walk) |
| `docker/versioning/diagnostic_projection.py` | `_exception_type_chain` | inspect-only (chain walk) |

Definition-only assignments (`self.cause` in `TransactionError.__init__`,
`domain_error.__cause__ = exc` in `build_snapshot.py`) are Stores and are not
consumer cause accesses; they never replace an exception.

## AST guard (task 5A.12)

`CauseUnwrappingGuardTests` walks every production function and rejects any
site where a raw cause *escapes* the typed wrapper. The detector tracks
cause-derived names transitively (plain and annotated assignments, so
`cause = exc.cause`, `cause: BaseException = exc.cause`, and `result = cause`
are all cause-derived) and flags:

- a bare raw re-raise (`raise cause`, `raise exc.cause`);
- a cause chained into any new exception, including a domain error
  (`raise DomainError() from exc.cause`, `raise DomainError() from cause`);
- a raw cause returned directly (`return exc.cause`);
- a returned aggregate built from the cause (`return AggregateFailure(cause)`);
- a constructor/function call that aggregates the cause as a direct argument
  (`CleanupFailures(exc.cause)`, `errors.append(cause)`, `TransactionError(...)`
  with `cause=cause`, `carry_secondary_diagnostics(cause, exc)`).

The guard deliberately does **not** flag cause usage that only inspects or
renders the value: `isinstance(exc.cause, FileNotFoundError)`,
`exc.cause.errno == errno.ENOENT`, `type(...)`/`str`/`repr`/`getattr`
inspection, truthiness, comparisons, and attribute reads used for policy or
diagnostics.

Only one escape is permitted, and the exemption is expression-specific: at a
`typed-propagation` site, `raise <approved wrapper>(..., cause=cause) from
cause` (or the equivalent alias form, where the alias was assigned an approved
wrapper construction that references the same cause) is allowed. The approved
wrappers are explicit (`TransactionError`, `CapabilityError`); `TransactionError`
must store the raw cause through an explicit `cause=` keyword. A bare
`raise cause`, a `return cause`, an aggregation (`CleanupFailures(cause)`,
`errors.append(cause)`), or a `raise DomainError() from cause` is still rejected
inside the same `typed-propagation` function: a function's valid typed-wrapper
branch does not exempt its unrelated branches. Constructing an approved typed
wrapper from the cause is the permitted building block; aggregating the cause
into a non-wrapper accumulator is not.

Function-level classifications grant no blanket permission. `definition` sites
may store a supplied cause as exception state but may not read a typed
wrapper's `.cause` and raise or return it; `pre-adoption-raw` sites receive raw
POSIX errors directly and may not read a typed wrapper's `.cause` and return
it. The only exemption for an actual raw-cause replacement is an explicit
`_JUSTIFIED_RAW_CONTRACTS` entry (currently empty), so `_unwrap_sites()`
currently reports nothing. The production
`DirectoryCapability.from_secure_path` typed-wrapper propagation is parsed
directly and asserted permitted, and a synthesized equivalent classified branch
containing `raise cause` is asserted rejected.

Synthetic fixtures prove each prohibited form is rejected: direct raw re-raise,
aliased raw re-raise, direct domain chaining, aliased domain chaining,
annotated-alias domain chaining, assignment into an aggregate,
constructor/method-call aggregation, transitive-alias aggregation, returned
raw cause, returned aggregate, and `carry_secondary_diagnostics`. Additional
fixtures prove an approved `raise TransactionError(..., cause=cause) from cause`
and its alias form pass, while bare `raise cause`, `return cause`, aggregation,
`raise DomainError() from cause`, and an approved wrapper missing its `cause=`
argument are rejected at a classified site. Pass-through fixtures prove
`isinstance` and errno inspection, alias-and-compare inspection, and
`type`/render formatting are not flagged.

`_raise_effective_failure` was annotated `-> NoReturn` so the type checker
retains flow narrowing after the domain-wrap replacement.

## Focused coverage (tasks 5A.2–5A.7)

- **5A.2** `BuildCleanupAggregationTests`: a typed `STAGE_VALIDATE` failure
  stays the aggregated `CleanupFailure.error`, its exact raw stat cause is
  reachable through `.cause`/`.__cause__`, and the simultaneous caller-owned
  raw close failure is secondary on the typed wrapper with nothing copied onto
  the raw cause.
- **5A.3** `ProjectStateChainingTests` and `RenderingChainingTests`: the
  domain error chains directly from the typed construction/validation failure,
  which keeps its raw cause and its secondary diagnostics. The
  generated-directory ownership split is covered behaviorally by
  `test_transactions_phase6_effective_projection.GeneratedAdoptionOwnershipTests`
  (failed adoption raw-closes the caller-owned descriptor exactly once;
  successful adoption releases only through the capability).
- **5A.4** `AdapterTypedPreservationTests`: the npm-publication,
  artifact-cache, build-cache, build-marker, effective-state, and rendering
  adapters preserve a typed lock/transaction failure unchanged, or chain their
  domain error directly from it. Runtime-projection domain wrapping applies to
  typed operational `TransactionError` failures only: `create_runtime_projection`
  chains its `EffectiveConfigError` directly from the operational wrapper, while
  a capability misuse / no-follow safety classification keeps its original
  `CapabilityError` (with the raw `OSError` as its direct cause) and a
  process-control interruption propagates unchanged. Dedicated tests pin all
  three outcomes and assert the operational and safety paths never collapse
  into each other.
- **5A.5** `LockReleaseCleanupTests`: `CleanupFailures` classifies a typed
  `LockError` directly under `ordinary=(LockError,)`, preserves an active
  primary with the typed failure as secondary, attempts every independent
  release once, propagates a process-control interruption unchanged, and the
  shared cleanup type is the same object as the transaction re-export.
  `ConstructorProjectBuildLockExitPolicyTests` fixes the lock's `__exit__`
  policy: a `LockError` or a close-stage `TransactionError` from release stays
  secondary to an active body exception, while an unexpected raw `OSError`,
  an unexpected `RuntimeError`, or a process-control interruption from release
  remains authoritative over the body exception.  The acquisition cleanup no
  longer lists raw `OSError` as ordinary for the adopted lock capability, so
  that boundary stays typed after adoption.
- **5A.6** `LegitimateCauseInspectionTests`: `FileNotFoundError` absence and
  no-follow safety inspection keep the typed wrapper; the direct injected
  `PosixFileOps` boundary and the pre-adoption raw descriptor close keep the
  raw `OSError`.
- **5A.7** `RawContractInventoryTests`: no production site is grandfathered as
  requiring the exact raw exception type; the declared pre-adoption/direct-POSIX
  boundaries are named explicitly.

## Exception graph (task 5A.13)

`ExceptionGraphTests` proves one coherent chain — domain error (when
applicable), then typed transaction/lock failure, then exact raw cause — and
that secondary diagnostics are retained once on the typed failure with no
duplicate transfer onto the raw cause.

## Capability safety classification (task 5A.4)

The runtime-projection adapter narrows domain wrapping to typed operational
failures. `create_runtime_projection` translates an operational
`TransactionError` into `EffectiveConfigError` (chained directly from the
wrapper), but a capability misuse or no-follow safety rejection remains the
original `CapabilityError` with the rejected operation's raw `OSError` as its
direct `__cause__`, and a process-control interruption propagates unchanged.
This preserves the pre-existing safety contract: a genuine capability
violation is never reclassified as a generic publication failure.

## Ownership boundary (task 5A.14)

`OwnershipBoundaryInventoryTests` pins the `write_effective_build` `finally`
split: `generated` (adopted) releases through `DirectoryCapability.close`,
while `generated_fd` (adoption failed) is still caller-owned and raw-closes via
`ops.close` under `ordinary=(OSError,)`. `build_cleanup._open_algorithm_directory`
raw-closes only the pre-adoption caller-owned descriptor and aggregates the
typed failure unchanged.

## Verification results

```text
python -m unittest tests.test_transactions_l1_error_boundary \
  tests.test_transactions_phase5a_typed_preservation \
  tests.test_transactions_phase6_effective_projection \
  tests.test_transactions_phase6_project_metadata \
  tests.test_transactions_phase7_runtime_projection \
  tests.test_transactions_phase7_runtime_lock \
  tests.test_constructor_build_cleanup \
  tests.test_constructor_build_generation_integration \
  tests.test_constructor_build_persistence \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_specialized \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_npm_environment_publication_cleanup \
  tests.test_npm_environment_publication \
  tests.test_transactions_locking \
  tests.test_transactions_phase8_npm_lock \
  tests.test_transactions_phase9b_remap_boundaries \
  tests.test_transactions_phase9_boundaries
  -> Ran 547 tests in 9.173s
     OK

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5573 tests in 102.989s
     OK (skipped=13)

ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!

scripts/check-types
  -> All checks passed!

openspec validate extract-generic-descriptor-capabilities --strict
  -> Change 'extract-generic-descriptor-capabilities' is valid
```

## Summary

| Task | Status |
| --- | --- |
| 5A.1–5A.7 (RED / inventory / characterization) | complete |
| 5A.8–5A.11 (GREEN) | complete |
| 5A.12–5A.14 (INTROSPECT) | complete |
| 5A.15–5A.16 (VALIDATE) | complete |
