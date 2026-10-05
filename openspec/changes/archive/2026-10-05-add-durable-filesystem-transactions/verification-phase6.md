# Phase 6 Verification — Project Metadata and Effective Build Projection

Date: 2026-10-03T22:10:06Z
Change: `add-durable-filesystem-transactions`
Phase: 6 (Project Metadata and Effective Build Projection)
Depends on: Phase 1 (`Add durable filesystem transactions substrate`, commit
`05fe4b4`)

## Scope delivered

Project identity metadata now publishes through the shared L2 **durable
no-clobber** contract, and the effective build projection publishes through the
shared L2 **durable replacement** contract.  Project identity derivation,
concurrent-winner verification, project-state validation, TOML
serialization, destination identity, and the existing domain diagnostics stay
in their owning domains.  The user-directed `write_effective_inventory`
output is deliberately left on its original sibling-temporary-file plus
`os.replace` mechanics and is not migrated to L2.

Production changes:

- `docker/versioning/project_state.py`
  - `_publish_metadata` now builds a `DirectoryCapability` from the already
    validated namespace descriptor and calls
    `RegularFileContracts(ops).durable_no_clobber(directory, "project.json",
    expected, 0o600)`.  Only a typed `DestinationExists` is treated as the
    concurrent winner; every other `TransactionError` is mapped to the
    unchanged `ProjectStateError("cannot publish project identity metadata
    {label}")` with the raw cause and chaining preserved.  The manual
    random-sibling open/write/fsync/link/unlink/fsync sequence and its
    temp-name `FileExistsError` branch are removed.  The namespace descriptor
    remains owned by the caller, so the transient capability is not closed.
- `docker/versioning/rendering.py`
  - `write_effective_build` now opens `generated` relative to the retained
    namespace descriptor, wraps it in a `DirectoryCapability`, validates an
    existing destination leaf through a retained no-follow descriptor with the
    existing `EffectiveInventoryOutputError` messages, and publishes the
    serialized TOML through
    `RegularFileContracts(ops).durable_replace(generated, name, payload,
    0o600)`.  A new `_raise_effective_failure` preserves an unsafe-destination
    `EffectiveInventoryOutputError` and re-raises an unexpected raw `OSError`
    with shared cleanup diagnostics attached; interruptions are never caught.
  - New helpers `_serialize_toml_bytes`, `_validate_effective_destination`,
    and `_raise_effective_failure` keep the domain authority in the rendering
    module.  `_VALIDATE_FLAGS` was added for the no-follow destination check.
  - `write_effective_inventory` is unchanged.

## RED (tasks 6.1–6.3)

Two new focused test modules were added before the migration was applied:

- `tests/test_transactions_phase6_project_metadata.py` (task 6.1, 13 tests)
  - routes through L2 `durable_no_clobber`; exact bytes and `0600` mode; file
    flush before publication and parent flush before success; three fresh
    temporary allocations after `EEXIST`; exhausted allocation is a
    `ProjectStateError` at `allocate-temporary`; operational failure preserves
    `ProjectStateError` message and raw cause/errno/`__cause__`; interruption
    passthrough; matching/mismatched/unsafe final-collision concurrent-winner
    verification; non-regular, permissive, and foreign-owned entries rejected;
    `resolve_project_state` publishes exact metadata.
- `tests/test_transactions_phase6_effective_projection.py` (tasks 6.2–6.3, 18
  tests)
  - routes through L2 `durable_replace`; valid TOML and `0600` mode; file
    flush before replacement and parent flush before success; safe creation;
    generated symlink swap fails closed; permissive generated directory,
    symlink/non-regular/permissive/multiply-linked/foreign-owned destinations
    rejected without repair; raw `OSError` re-raised with `errno`; interruption
    passthrough; missing generated child is an `EffectiveInventoryOutputError`;
    `write_effective_inventory` uses a sibling temp plus `os.replace`, cleans a
    write failure, does not call any shared durable contract, and makes no
    directory-durability claim.

RED result against the pre-Phase-6 sources (source files temporarily reverted,
then restored with `git apply /tmp/phase6_impl.patch`):

```text
Ran 31 tests in 0.134s
FAILED (errors=27)
```

The four passing tests were the pure user-output boundary checks, which by
design assert the unchanged, non-migrated path.

Existing `tests/test_constructor_build_persistence.py` regressions that pinned
the old `os.replace` / `.build-effective-` temporary implementation detail were
updated to assert the equivalent L2 mechanics: one atomic descriptor-relative
`os.rename` from a private `.transaction-` sibling with matching source and
destination directory descriptors, and a raw `OSError` on replacement failure.

## GREEN (tasks 6.4–6.6)

```text
$ python -m unittest tests.test_transactions_phase6_project_metadata \
      tests.test_transactions_phase6_effective_projection
Ran 31 tests in 0.283s
OK
```

Task 6.4 (project identity metadata on durable no-clobber) and task 6.5
(effective build projection on durable replacement) are covered by the two new
modules.  Task 6.6 is covered by
`UserDirectedInventoryOutputBoundaryTests`, which asserts that
`write_effective_inventory` calls no shared durable contract and never calls
`os.fsync`.

## INTROSPECT (task 6.7)

1. **Temp/final collision conflation** — the project-metadata consumer handles
   only `DestinationExists` as the concurrent winner; a temporary-name
   collision is retried inside L2 and an exhausted allocation is a
   `TransactionError` at `allocate-temporary` mapped to `ProjectStateError`.
   The previous implementation's `except FileExistsError` around the random
   sibling open could misread a temporary-name collision as a final
   destination collision; the migration removes that latent conflation.  The
   `durable_replace` path has no final-collision outcome, so only internal
   temporary retry applies.  No finding.
2. **Pathname reconstruction** — `project_state` wraps the caller's already
   validated namespace descriptor; `rendering` opens `generated` relative to
   the retained namespace descriptor and every subsequent validation and
   commit is relative to `generated.fd`.  Neither adapter reconstructs a path
   from a descriptor.  No finding.
3. **Domain-error drift** — `_publish_metadata` retains the exact
   `"cannot publish project identity metadata {label}"` text; the projection
   adapter retains the existing `"is not a regular file"`, `"is not owned by
   the invoking user"`, and `"has mode ..., expected 0o600"` messages for the
   existing cases, and adds a single-link diagnostic for the multiply-linked
   case L2 now rejects.  `ProjectStateError` from `validate_project_state` is
   still mapped to `EffectiveInventoryOutputError(str(exc))`.  No finding.
4. **Raw-cause loss** — the metadata adapter chains the `TransactionError` as
   `ProjectStateError.__cause__`, whose own `__cause__` is the raw `OSError`.
   The projection adapter re-raises the raw `OSError` object directly and
   attaches shared cleanup failures as secondary context.  No finding.
5. **Interruption conversion** — L2 catches `BaseException` to run cleanup and
   re-raises the interruption; both adapters catch only `TransactionError` /
   `ProjectStateError`.  `KeyboardInterrupt` and cancellation are not
   translated.  Pinned by
   `test_interruption_passes_through_unchanged` in both modules.  No finding.
6. **False durability** — project metadata now uses `durable_no_clobber`
   (file flush before publication, parent flush before success), and the
   projection uses `durable_replace` (complete private sibling, file flush,
   validated destination, atomic rename, parent flush).  Parent-flush failure
   remains a failure even though the destination is visible because the L2
   contract raises at `fsync-directory` and the projection adapter re-raises
   the raw cause.  No finding.
7. **Cleanup masking** — L2 `_discard` attaches ordinary cleanup/close
   failures as secondary diagnostics and preserves the primary failure; the
   projection adapter transfers `TransactionError.secondary` onto the
   re-raised raw `OSError`.  A follow-up pass found four remaining cases: a
   destination-descriptor `close` failure masking a destination `fstat`
   failure in `_validate_effective_destination`, an unsafe-destination
   diagnostic (for example an invalid mode) masked because the validation ran
   after the descriptor was closed, a generated-directory `close` failure
   masking any primary validation/publication failure in `write_effective_build`,
   and a namespace-descriptor `close` failure masking any primary
   validation/publication failure through `ValidatedProjectState.__exit__`.
   All four are fixed and pinned; see the *Post-completion hardening passes*
   below.
8. **Accidental user-output migration** — `write_effective_inventory` was not
   modified and imports no shared durable API;
   `test_not_migrated_to_shared_l2_durable_contracts` and
   `test_does_not_claim_directory_durability` pin the boundary.  No finding.
9. **Error-boundary fidelity** — every adapter must translate exactly the
   documented unsafe-leaf diagnostics and leave unexpected operational errors
   observable.  A follow-up pass found two gaps: `_validate_effective_destination`
   converted *every* destination `openat` `OSError` (EIO, EMFILE, ...) into
   `"... is not a regular file"`, and `_publish_metadata` performed
   `DirectoryCapability.from_fd` outside its publication `try`, so an
   operational capability `fstat` failure escaped as a raw `OSError` instead of
   the required publication `ProjectStateError`.  Both are fixed and pinned;
   see the *Post-completion hardening passes* below.
10. **Collision cleanup masking** — a typed final-destination collision is a
   clean concurrent-winner outcome only when the private temporary entry was
   actually removed.  A follow-up pass found that `_publish_metadata` caught
   `DestinationExists` without binding it and unconditionally returned, so a
   collision whose temporary cleanup (`unlinkat`) failed reported success and
   silently leaked the private entry.  It now returns only for a clean
   collision (`exc.secondary` empty) and otherwise raises
   `ProjectStateError` chained from the collision, preserving its attached
   cleanup diagnostics.  Fixed and pinned; see the *Post-completion hardening
   passes* below.

These findings required code changes; the remaining checklist items
resolved without one.  The findings are recorded in the hardening passes
below.

## Post-completion hardening pass: destination-close masking (task 6.7/6.8)

### Issue

`_validate_effective_destination` opened a retained no-follow descriptor for
an existing destination leaf and closed it in a bare `finally`:

```python
try:
    info = ops.fstat(fd)
finally:
    ops.close(fd)
```

When `fstat` raised a primary failure and `close` then also raised, the close
error replaced the primary exception, violating the cleanup-secondary
requirement (cleanup failures are diagnostics, the primary failure is
authoritative).

### Fix (`docker/versioning/rendering.py`)

The descriptor close follows the shared L2 primary/secondary pattern now used
elsewhere in the substrate (for example `RegularFileContracts.validated_read`):

```python
primary: BaseException | None = None
try:
    info = ops.fstat(fd)
except BaseException as exc:
    primary = exc
    raise
finally:
    try:
        ops.close(fd)
    except OSError as close_exc:
        if primary is not None:
            attach_secondary(primary, [close_exc])
        else:
            raise
```

The descriptor is always closed.  An ordinary `OSError` close failure is
attached to the in-flight primary via `attach_secondary`, so the original
`fstat` exception object, `errno`, and message stay primary and the close error
is observable in `_transaction_secondary`.  A close failure alone (no primary)
propagates normally.  Only I/O (`OSError`) close failures are caught; a
process-control interruption during `fstat` re-raises unchanged and a
`KeyboardInterrupt` during `close` is not converted.

### Test (`tests/test_transactions_phase6_effective_projection.py`)

`_DualFaultOps` faults only the retained destination descriptor: `fstat` on
that descriptor raises `OSError(EIO, "injected destination fstat")` and its
`close` raises `OSError(EIO, "injected destination close")`.  The generated
directory and all other descriptors behave normally, isolating the
`_validate_effective_destination` cleanup path.

`test_fstat_failure_survives_destination_close_failure` asserts the raised
exception *is* the fstat error object with its `errno`/`strerror` intact, that
the close error appears exactly once in the primary's
`_transaction_secondary`, that it is not the raised exception, and that the
prior destination bytes and the absence of committed temporaries are
preserved.

Pre-fix fault injection reproduced the masking exactly
(`AssertionError: OSError(5, 'injected destination close') is not OSError(5,
'injected destination fstat')`); post-fix the test passes.  The source file was
restored byte-for-byte after the mutation check.

## Post-completion hardening pass: generated-directory close masking (task 6.7/6.8)

### Issue

`write_effective_build` closed its retained `generated` directory descriptor in
an unconditional `finally`:

```python
finally:
    if generated_fd is not None:
        ops.close(generated_fd)
```

When validation or publication raised a primary failure and closing the
generated-directory descriptor then also raised, the close error replaced the
primary exception.  The same masking class fixed in
`_validate_effective_destination` therefore remained on the outer exit path.

### Fix (`docker/versioning/rendering.py`)

The retained descriptor close now uses the same primary/secondary pattern.  The
`ProjectStateError -> EffectiveInventoryOutputError` mapping moved into an
inner `try` so that the outer `except BaseException` records whichever
exception will actually propagate as `primary`:

```python
primary: BaseException | None = None
try:
    try:
        ... validate and publish ...
    except ProjectStateError as exc:
        raise EffectiveInventoryOutputError(str(exc)) from exc
except BaseException as exc:
    primary = exc
    raise
finally:
    if generated_fd is not None:
        try:
            ops.close(generated_fd)
        except OSError as close_exc:
            if primary is not None:
                attach_secondary(primary, [close_exc])
            else:
                raise
```

`generated_fd` is still closed on every exit path.  An `OSError` close failure
with an in-flight primary is attached via `attach_secondary`, so the primary
identity, `errno`, and message survive; a close failure alone propagates
normally; only `OSError` is caught, so a `KeyboardInterrupt` from close
propagates unchanged and is never attached or converted.

### Tests (`tests/test_transactions_phase6_effective_projection.py`)

`_GeneratedCloseOps` faults only the descriptor returned when opening
`generated`, leaving publication and L2 cleanup descriptors intact:

- `test_publication_failure_survives_generated_close_failure` — `renameat`
  fails with a primary `OSError` and the generated close also fails; asserts
  the raised exception is the publication error object with intact
  `errno`/`strerror`, that the close error is attached exactly once in
  `_transaction_secondary`, that it does not replace the primary, and that the
  prior destination bytes are preserved.
- `test_generated_close_failure_propagates_when_publication_succeeds` — only
  the generated close fails; asserts the close `OSError` is the primary and the
  destination was nevertheless published as valid TOML.
- `test_generated_close_interruption_propagates_unchanged` — the generated
  close raises `KeyboardInterrupt`; asserts it propagates directly with no
  secondary attachment and the destination was published.

Pre-fix fault injection reproduced the masking exactly
(`AssertionError: OSError(5, 'injected generated close') is not OSError(5,
'injected publication')`); post-fix the tests pass.  The source file was
restored byte-for-byte after the mutation check.

## Post-completion hardening pass: namespace-descriptor close masking (task 6.7/6.8)

### Issue

`ValidatedProjectState.__exit__` ignored the context-manager exception
arguments and closed unconditionally:

```python
def __exit__(self, exc_type, exc, tb) -> None:
    self.close()
```

Because `close()` calls `os.close` directly, a namespace-descriptor close
failure raised from `__exit__` replaced any active exception, masking a
validation or publication failure before `write_effective_build`'s outer
handler recorded it.  This was the remaining exit path of the same masking
class.

### Fix (`docker/versioning/project_state.py`)

`attach_secondary` is imported from `docker.transactions.errors`, and
`__exit__` now uses the protocol arguments:

```python
def __exit__(self, exc_type, exc, tb) -> None:
    if exc is None:
        self.close()
        return
    try:
        self.close()
    except OSError as close_exc:
        attach_secondary(exc, [close_exc])
```

With an active exception, an `OSError` close failure is attached as a
secondary diagnostic and the original exception continues; with no active
exception the close error propagates.  Only `OSError` is caught, so a
`KeyboardInterrupt` from close propagates unchanged.  `close()` is unchanged
and still idempotent: `_closed` is set **before** `os.close`, so a failed POSIX
close is never retried.

### Tests

`tests/test_transactions_phase6_project_metadata.py` adds
`NamespaceCloseMaskingTests`, which faults only the validated namespace
descriptor and counts close attempts:

- `test_close_failure_does_not_mask_active_failure` — body raises a primary
  `OSError` and the namespace close also fails; asserts the body error remains
  primary with intact `errno`, the close error is attached exactly once in
  `_transaction_secondary`, it does not replace the primary, and the descriptor
  was closed exactly once.
- `test_close_failure_propagates_when_body_succeeds` — body succeeds and only
  the namespace close fails; asserts the close error is primary and is not
  retried.
- `test_close_interruption_propagates_unchanged` — the namespace close raises
  `KeyboardInterrupt`; asserts it propagates directly with no secondary
  attachment.

`tests/test_transactions_phase6_effective_projection.py` adds
`test_publication_failure_attaches_namespace_close_secondary`, which drives the
real Phase 6 consumer: a `renameat` publication failure plus a namespace-close
failure during `write_effective_build`.  It asserts the publication `OSError`
stays primary with intact `errno`/`strerror`, the namespace-close error is
attached exactly once, it does not replace the primary, the descriptor is
closed once, and the prior destination bytes are preserved.

Pre-fix fault injection reproduced the masking exactly
(`AssertionError: OSError(5, 'injected namespace close') is not OSError(5,
'injected body failure')` and `... is not OSError(5, 'injected publication')`);
post-fix the tests pass.  The source file was restored byte-for-byte after the
mutation check.

## Post-completion hardening pass: destination open errors, validation ordering, and capability boundary (task 6.7/6.8)

### Issue A — destination `openat` errors misreported

`_validate_effective_destination` converted every destination `openat`
`OSError` into an unsafe-leaf diagnostic:

```python
except OSError as exc:
    raise EffectiveInventoryOutputError(f"{name} is not a regular file") from exc
```

That misreported operational failures such as `EIO` or `EMFILE` as a
non-regular file.  Only a known unsafe-leaf rejection should be translated;
missing destinations stay allowed and unexpected operational errors must
remain observable.

### Fix A (`docker/versioning/rendering.py`)

`errno` is imported and the handler now translates only `ELOOP` (a no-follow
symlink rejection) and re-raises everything else unchanged:

```python
except FileNotFoundError:
    return
except OSError as exc:
    if exc.errno == errno.ELOOP:
        raise EffectiveInventoryOutputError(
            f"{name} is not a regular file"
        ) from exc
    raise
```

### Issue B — destination validation after descriptor cleanup

The destination type/ownership/link/mode checks ran *after* the descriptor
was closed.  A close failure then raised before the validation ran, masking
an unsafe-destination diagnostic (for example an invalid mode) instead of
being attached as secondary.

### Fix B (`docker/versioning/rendering.py`)

The checks now run inside the same protected `try` as `fstat`, so the
unsafe-destination diagnostic is captured as the primary failure and a close
failure is only attached via `attach_secondary`, while `KeyboardInterrupt`
still passes through and the descriptor is closed exactly once:

```python
primary: BaseException | None = None
try:
    info = ops.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise EffectiveInventoryOutputError(f"{name} is not a regular file")
    ...
except BaseException as exc:
    primary = exc
    raise
finally:
    try:
        ops.close(fd)
    except OSError as close_exc:
        if primary is not None:
            attach_secondary(primary, [close_exc])
        else:
            raise
```

### Issue C — capability creation outside the publication boundary

`_publish_metadata` called `DirectoryCapability.from_fd(ops, namespace_fd,
label)` before its `try`.  An operational `fstat` failure during capability
creation escaped as a raw `OSError` rather than the required
`ProjectStateError`.

### Fix C (`docker/versioning/project_state.py`)

`DirectoryCapability.from_fd` is now inside the publication `try`, and an
operational `OSError` is mapped to the domain diagnostic with exception
chaining preserved.  Only `DestinationExists` remains eligible for the
concurrent-winner return, and no interruption is caught:

```python
try:
    directory = DirectoryCapability.from_fd(ops, namespace_fd, label)
    RegularFileContracts(ops).durable_no_clobber(
        directory, "project.json", expected, 0o600
    )
except DestinationExists:
    return
except TransactionError as exc:
    raise ProjectStateError(
        f"cannot publish project identity metadata {label}"
    ) from exc
except OSError as exc:
    raise ProjectStateError(
        f"cannot publish project identity metadata {label}"
    ) from exc
```

### Tests

`tests/test_transactions_phase6_effective_projection.py` adds
`_DestinationOpenOps` and `_DestinationCloseOps` plus:

- `test_destination_open_operational_failure_escapes_raw` — the destination
  `openat` raises `EIO` and then `EMFILE`; asserts the original `OSError`
  object escapes with its `errno`, and the prior bytes/mode are unchanged.
- `test_invalid_mode_survives_destination_close_failure` — a real `0o644`
  destination plus an injected destination-close failure; asserts the
  `"expected 0o600"` diagnostic stays primary, the close error is attached
  exactly once as secondary, and the destination bytes and `0o644` mode are
  unchanged.

`tests/test_transactions_phase6_project_metadata.py` adds
`PublicationCapabilityBoundaryTests.test_capability_fstat_failure_is_publication_error`
— an injected capability `fstat` failure; asserts the exact
`ProjectStateError`, the preserved raw `OSError` cause, and that no
`project.json` was published.

Pre-fix fault injection reproduced each gap (blanket `openat` conversion
fails the open test; validation-after-close fails the invalid-mode test;
`from_fd` outside the `try` fails the capability test); post-fix the three
tests pass.  Both source files were restored byte-for-byte after the
mutation checks.

## Post-completion hardening pass: collision cleanup masking (task 6.7/6.8)

### Issue

A typed final-destination collision is the concurrent winner only when its
private temporary entry was actually removed.  `_publish_metadata` caught the
collision without binding it and returned unconditionally:

```python
except DestinationExists:
    return
```

When the temporary `unlinkat` cleanup itself failed, `_discard` attached the
cleanup `OSError` to the `DestinationExists` as a secondary diagnostic.  The
unconditional return then discarded that diagnostic and reported success,
silently leaking the private temporary file.

### Fix (`docker/versioning/project_state.py`)

The handler binds the collision and distinguishes a clean winner from a
collision with failed cleanup.  A clean collision still returns so the caller
reopens and verifies the winner's exact bytes; a collision with any attached
secondary diagnostic raises the publication `ProjectStateError`, chained from
the collision so both the collision and its cleanup diagnostics stay
observable.  The existing `project.json` is neither deleted nor replaced.

```python
except DestinationExists as exc:
    if exc.secondary:
        raise ProjectStateError(
            f"cannot publish project identity metadata {label}"
        ) from exc
    return
```

### Test

`tests/test_transactions_phase6_project_metadata.py`
`ConcurrentWinnerVerificationTests.test_collision_cleanup_failure_is_surfaced`
publishes against an existing matching `0600` `project.json`, injects an
`unlinkat` `EIO` failure while `_discard` cleans the private temporary entry,
and asserts the publication raises `ProjectStateError` whose cause is the
`DestinationExists` and whose secondary list still contains the injected
cleanup error, while the existing metadata bytes and `0600` mode are
unchanged.  The clean-collision test
(`test_matching_final_collision_defers_to_winner_verification`) continues to
pass.

Pre-fix fault injection reproduced the gap (the unconditional return fails
`test_collision_cleanup_failure_is_surfaced` with "`ProjectStateError` not
raised"); the source file was restored byte-for-byte afterwards.

## Mutation checks

The new tests are load-bearing:

- Removing the `DestinationExists` concurrent-winner branch from
  `_publish_metadata` fails **3** project-metadata tests
  (`matching`/`mismatched`/`unsafe` final collision).
- Removing the raw-`OSError` re-raise from `_raise_effective_failure` fails the
  projection `test_operational_failure_reraises_raw_oserror`.
- Reverting `_validate_effective_destination` to the unconditional `finally`
  close fails `test_fstat_failure_survives_destination_close_failure` with the
  close error masking the `fstat` error.
- Reverting `write_effective_build` to the unconditional `finally` close fails
  `test_publication_failure_survives_generated_close_failure` with the generated
  close error masking the publication error.
- Reverting `ValidatedProjectState.__exit__` to the unconditional `close()`
  fails `test_close_failure_does_not_mask_active_failure` and
  `test_publication_failure_attaches_namespace_close_secondary` with the
  namespace close error masking the body/publication error.
- Reverting the destination `openat` handler to the blanket conversion fails
  `test_destination_open_operational_failure_escapes_raw`.
- Moving the destination type/ownership/link/mode checks back after the
  `finally` close fails `test_invalid_mode_survives_destination_close_failure`
  with the close error masking the `"expected 0o600"` diagnostic.
- Moving `DirectoryCapability.from_fd` back outside the publication `try` fails
  `test_capability_fstat_failure_is_publication_error` (raw `OSError` instead of
  `ProjectStateError`).
- Restoring the unconditional successful return for `DestinationExists` fails
  `test_collision_cleanup_failure_is_surfaced` with "`ProjectStateError` not
  raised".

Both source files were restored byte-for-byte after each mutation (verified
with `diff -q`) and the 43 focused tests pass again.

## VALIDATE (task 6.8)

- Focused Phase 6 suites: `tests.test_transactions_phase6_project_metadata`
  (18) + `tests.test_transactions_phase6_effective_projection` (25) —
  **43 tests, OK**.
- Project-state / project-root / projection / rendering:
  `tests.test_constructor_project_state_phase1`,
  `tests.test_constructor_project_root_phase3`,
  `tests.test_constructor_project_root_phase6`,
  `tests.test_constructor_project_root_phase7`,
  `tests.test_constructor_build_projection`,
  `tests.test_constructor_build_persistence`,
  `tests.test_constructor_project`, `tests.test_constructor_project_inputs`,
  `tests.test_version_effective` — **all OK**.
- Path-security / failure-injection: `tests.test_build_context_confinement`,
  `tests.test_constructor_build_output`,
  `tests.test_constructor_build_output_acceptance`, and every
  `tests.test_transactions_*` module — **all OK**.
- Build transaction/generation integration:
  `tests.test_constructor_build_transactions`,
  `tests.test_constructor_build_generation_integration`,
  `tests.test_constructor_build_generations`,
  `tests.test_constructor_build_cleanup` — **all OK**.
- Full unit suite: `python -m unittest discover -s tests -p 'test_*.py'` —
  **4942 tests, OK (skipped=13)** in 96.9s (the hardening passes add twelve tests).
- Type check: `ty check docker --python-version 3.14 --output-format concise`
  — **All checks passed!**
- `git diff --check` — clean.
- `openspec validate add-durable-filesystem-transactions --strict` —
  **Change is valid**.

Recorded outcomes: project metadata is now durable/no-clobber with exact
bytes, `0600` mode, typed final collision, and concurrent-winner verification;
the effective build projection is durable/replace with unchanged domain
validation and raw-cause/interruption parity; the user-directed effective
inventory output retains its original atomic-output and failure behavior.
