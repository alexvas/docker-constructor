# Phase 4 Verification Record — Transaction Capability Integration

Date: 2026-10-05T23:32:58Z (hardened 2026-10-05T23:49:21Z, 2026-10-06T01:03:24Z, 2026-10-06T01:44:19Z, 2026-10-06T01:52:26Z, 2026-10-06T01:58:58Z)

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 4 only. Phases 5–8 remain unstarted.

## Production files changed

```text
docker/filesystem/descriptors.py       # cause-preserving root-open translation + validated-transfer seam
docker/transactions/capabilities.py    # delegate ownership + walk; preserve transaction labels
docker/transactions/posix.py           # implement the four missing DescriptorOps members
```

`DirectoryDescriptor.adopt()` is unchanged. No other production file changed.

## Test files added

```text
tests/test_transactions_descriptor_integration.py  # 26 tests
```

## Artifacts changed

```text
openspec/changes/extract-generic-descriptor-capabilities/tasks.md  # 4.1–4.9 checked
```

## Commands and results

RED evidence (pre-migration `capabilities.py` restored from the index):

```text
python -m unittest tests.test_transactions_descriptor_integration
  -> Ran 18 tests ... FAILED (failures=6)
     (all six failures are in SharedDelegationStructureTests and
      DirectoryOwnershipAstTests; every FromFdContractTests case already
      passed because the pre-transfer semantics were unchanged)
```

RED evidence (root-open guard removed from `open_secure_path`):

```text
python -m unittest \
  tests.test_transactions_descriptor_integration.FromSecurePathRootFailureTests
  -> Ran 1 test ... FAILED (errors=4)
     with the raw `OSError: [Errno 40] injected root open` escaping
     DirectoryCapability.from_secure_path() instead of a CapabilityError
```

This second RED covers both `"/"` and `"/a/b"` across `errno.EIO`
(-> `DescriptorError`) and `errno.ELOOP` (-> `UnsafeDescriptorError`).

RED evidence (empty-label compatibility removed; previous-turn `capabilities.py`
restored, which rejected an empty effective label):

```text
python -m unittest \
  tests.test_transactions_descriptor_integration.EmptyLabelCompatibilityTests
  -> Ran 3 tests ... FAILED (failures=1, errors=2)
     from_path/from_secure_path raised CapabilityError and from_fd raised the
     raw DescriptorError("descriptor label must be a non-empty string, not ''")
```

RED evidence (validation-stat unwrapping removed; `from_secure_path` mapping
reverted to the blanket `CapabilityError` translation):

```text
python -m unittest \
  tests.test_transactions_descriptor_integration.SecurePathValidationStatFailureTests
  -> Ran 2 tests ... FAILED (errors=2)
     CapabilityError("cannot open directory capability ...: cannot stat
     directory ...") instead of the injected raw OSError
```

GREEN and task 4.8 gate:

```text
python -m unittest tests.test_transactions_descriptor_integration \
  tests.test_transactions_l0_posix tests.test_transactions_l1_capabilities \
  tests.test_transactions_locking tests.test_transactions_l2_atomic \
  tests.test_transactions_l2_durable tests.test_transactions_l2_lifecycle \
  tests.test_transactions_phase9_boundaries
  -> Ran 229 tests ... OK
     (descriptor integration 26, l0 posix 15, l1 capabilities 44,
      locking 56, l2 atomic 17, l2 durable 22, l2 lifecycle 21,
      phase 9 boundaries 28)
```

Task 4.9 consumer gate:

```text
python -m unittest tests.test_constructor_build_generation_integration \
  tests.test_constructor_build_cleanup \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_transactions_phase9a_specialized
  -> Ran 156 tests ... OK
```

Typecheck:

```text
ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

Full repository suite:

```text
python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5410 tests ... OK (skipped=13)
```

Whitespace:

```text
git diff --check
  -> clean
git diff --cached --check
  -> clean
```

## Task-by-task evidence

- **4.1** `tests/test_transactions_descriptor_integration.py` adds
  `FromFdContractTests`: successful `from_fd()` validation transfers sole
  ownership (`capability.fd is fd`, no close, one close on release); an
  injected `fstat` failure (`OSError`), a non-directory result, and a
  foreign-owned result each leave the fd caller-owned and open and never issue
  a close; `test_close_fault_injection_propagates_and_is_not_retried` covers
  close fault injection. The file also pins the existing imports, `fd`/
  `label`/`token`/`closed` properties, close semantics, and fault injection.
- **4.2** `SharedDelegationStructureTests` proves the module imports
  `DirectoryDescriptor` from `docker.filesystem.descriptors`, calls
  `DirectoryDescriptor.open_secure_path` and
  `DirectoryDescriptor._transfer_validated`, contains no component walker
  (`name.split(os.sep)`, `ops.openat(None, os.sep`, `components = [`), and
  shows `DirectoryCapability.__slots__ == {"_ops", "_descriptor",
  "_label", "_token"}` with no `_validate`/`_open_child_fd`/`_secure_and_adopt`
  methods. `_label` is compatibility metadata only — the caller-supplied
  transaction label returned by the `label` property — and is deliberately not
  ownership/release state (the removed `_fd`/`_closed` slots stay absent); the
  shared `DirectoryDescriptor` owns the descriptor and its release state. RED
  confirmed (see above).
- **4.3** `DirectoryCapability` is a thin adapter over the shared
  `DirectoryDescriptor`. `__init__` keeps the transaction `_AUTHORITY` token
  check and constructs the shared owner through
  `DirectoryDescriptor._transfer_validated`. `from_fd()` validates `fstat`,
  directory type, and effective ownership while the caller still owns the fd,
  then transfers exactly once through `DirectoryCapability._transfer_validated`
  without calling `DirectoryDescriptor.adopt()` or repeating validation
  (`test_from_fd_uses_the_validated_transfer_seam_not_adopt`). The foundation
  `DirectoryDescriptor.adopt()` is unchanged.
- **4.4** The transaction-local directory state machine and component walker
  were deleted; `close()`/`fd`/`closed`/`child_basename` now delegate, and
  `DirectoryDescriptor` provides release state. Regular-file behavior is
  preserved (`open_regular`, `_validate_regular`, `FileCapability`, `_READ_FLAGS`).
- **4.5** `docker/transactions/__init__.py` is untouched. `from_secure_path`
  maps foundation `DescriptorError`/`UnsafeDescriptorError` back to
  `CapabilityError` while preserving the raw operational cause (so the
  existing `effective.py` `__cause__`-is-`OSError` adapter still holds);
  `_adopt` keeps the raw-`OSError`-primary `from_path` behavior.
- **4.6** `DirectoryOwnershipAstTests` asserts `docker/filesystem/descriptors.py`
  defines `OwnedDescriptor`/`DirectoryDescriptor` with `_RELEASED` and the
  `ops.openat(None, os.sep` walk; no `docker/transactions/*.py` module
  reimplements the walk; and the `DirectoryCapability` segment carries no
  `_closed`/`_RELEASED` state or component walk.
- **4.7** `CompatibilityContractTests` pins `from_fd` parameter names
  `(ops, fd, label)`, the `from_path`/`from_secure_path` keyword-only `label`
  contract, the exact `docker.transactions.__all__` list, and the presence of
  `open_regular`/`child_basename`/`_validate_regular`.
- **4.8 / 4.9** Both gates pass (see commands above).

## Hardening — cause-preserving root-open translation (post-Phase-4)

`DirectoryDescriptor.open_secure_path()` previously called
`ops.openat(None, os.sep, ...)` unguarded, so a failure of the *initial* root
open leaked a raw `OSError` out of the foundation. That bypassed
`DirectoryCapability.from_secure_path()`'s required L1 mapping (which only
catches `DescriptorError`/`UnsafeDescriptorError`) and was observable for
`"/"` and for any injected first-open failure. The call is now wrapped:

```python
try:
    root_fd = ops.openat(None, os.sep, _DIR_FLAGS, 0)
except OSError as exc:
    raise _open_failure(os.sep, exc) from exc
```

`_open_failure` already maps `errno.ELOOP` to `UnsafeDescriptorError` and
other errnos to `DescriptorError`, chaining the original `OSError`; no cleanup
is added because no descriptor was returned. `DirectoryCapability.from_secure_path()`'s
error mapping, `DirectoryDescriptor.adopt()`, and all ownership/cleanup behavior
are unchanged.

The new `FromSecurePathRootFailureTests` parameterizes `"/"` and `"/a/b"`
over `errno.EIO` and `errno.ELOOP` and asserts the public exception is
`CapabilityError` (not `OSError`), `__cause__` is the injected `OSError`, only
one `openat` is attempted, and no `close` is issued. Foundation phase-3 gate
remains 134 tests OK; task 4.8 gate is now 229 tests OK and the full suite is
5410 tests OK (skipped=13). `ty check` and both whitespace checks stay clean,
and no Phase 4 checkbox changed.

## Compatibility — empty transaction labels remain supported

The binding contract requires that unchanged behavior stays unchanged: the
pre-migration `DirectoryCapability` accepted `label=""` and returned it
verbatim from `DirectoryCapability.label`. Delegating to the shared descriptor
initially regressed that, because the foundation intentionally rejects an empty
internal label (`DescriptorError` leaked from `from_path`/`from_fd` and the
mapped `CapabilityError` from `from_secure_path`).

`DirectoryCapability` now keeps the caller-supplied transaction label in a new
`_label` slot (transaction metadata only) and hands the shared owner a
non-empty private diagnostic label when the transaction label is empty:

```python
_INTERNAL_LABEL = "<directory-capability>"

def _internal_label(label: str) -> str:
    return label or _INTERNAL_LABEL

# in __init__
self._label = label
self._descriptor = DirectoryDescriptor._transfer_validated(
    ops, fd, label=_internal_label(label)
)
```

`from_secure_path` passes `_internal_label(target_label)` to
`DirectoryDescriptor.open_secure_path`, and `from_path` no longer maps a
post-open `DescriptorError` (the mapping branch and the now-unused
`carry_secondary_diagnostics` import were removed). The shared foundation's
non-empty-label contract is untouched, no second fd lifecycle or secure walker
was added, and the `fd`/`label` properties now report the transaction label.
`_label` is permitted transaction-specific metadata; the structural test still
rejects `_closed`, a raw `_fd` ownership field, duplicate release state, and
duplicate secure walkers.

The replacement `EmptyLabelCompatibilityTests` covers all three factories with
`label=""`: construction succeeds, `capability.label == ""`, ownership
transfers, and `close()` releases the descriptor exactly once (verified with
`os.fstat` and a second-close no-op). For `from_fd` it also proves the existing
fstat validation ran and that no `DescriptorError` is exposed after successful
transaction validation.

### Normalizing the private diagnostic label

`from_secure_path` calls `descriptor.detach()` *before* constructing the
transaction capability, so any rejection during transfer construction would
leave the detached leaf with no owner. `_internal_label()` now always returns a
valid non-empty `str` for the shared owner:

```python
def _internal_label(label: object) -> str:
    if isinstance(label, str) and label:
        return label
    return _INTERNAL_LABEL
```

A non-empty string is preserved (meaningful diagnostics); an empty string or a
non-string value — including a truthy non-string such as `Path("label")` or
`object()` — falls back to `_INTERNAL_LABEL`. `DirectoryCapability.label`
continues to return the original transaction-facing label verbatim. This
guarantees `DirectoryDescriptor._transfer_validated()` can never reject the
internal label after ownership has already moved out of the secure walk.

`NonStringLabelCompatibilityTests` pins the helper's normalization and drives
`from_secure_path(..., label=Path("label"))`/`object()` end to end: the factory
succeeds, preserves the transaction label, owns the validated leaf, leaves no
other walk descriptor open, and closes the leaf exactly once.

### Restoring the raw validation-stat failure mapping

`from_secure_path` translates foundation `DescriptorError`s into
`CapabilityError`, but the previous `_adopt` path surfaced a final-leaf
`ops.fstat` failure as the *raw* `OSError` (with cleanup diagnostics attached).
The secure walk wraps that stat failure in a validation-stage
`DescriptorError`, so the adapter now detects that one case and unwraps it
again:

```python
except DescriptorError as exc:
    cause = exc.cause
    if exc.stage == _FOUNDATION_VALIDATE_STAGE and isinstance(cause, OSError):
        carry_secondary_diagnostics(cause, exc)
        raise cause from None
    raise CapabilityError(
        f"cannot open directory capability {target_label!r}: {exc}"
    ) from cause
```

The validation stage is identified with the foundation's own stage constant
(`_STAGE_VALIDATE`, imported privately alongside the existing private
`_transfer_validated` seam). `carry_secondary_diagnostics` copies the
foundation's retained close/parent diagnostics onto the original `OSError`
without replacing it, so the raw error stays authoritative and no diagnostic is
discarded. All other bounded foundation errors keep the existing
`CapabilityError` translation: unsafe directory type or ownership
(`UnsafeDescriptorError`), secure-walk open failures, relative-path rejection,
label/construction failures, and transfer failures.

`SecurePathValidationStatFailureTests` injects a final-leaf `fstat` `OSError`
and asserts the exact instance is raised (not a `CapabilityError`); a second
case injects the leaf `fstat` failure together with a close failure and asserts
the stat error stays primary while the close defect is retained only as
`_transaction_secondary`. Both cases confirm every opened descriptor receives
exactly one close attempt.

## Added scope — `PosixFileOps` now satisfies `DescriptorOps`

Delegating the transaction walk to `DirectoryDescriptor.open_secure_path`
requires the injected transaction `ops` (`PosixFileOps`, extended by
`InjectedOps`) to structurally satisfy the foundation's `DescriptorOps`
injection protocol. `PosixFileOps` was missing `mkdirat`, `statat`, `listdir`,
and `rmdirat`, and its `openat`/`unlinkat` parameter names did not match the
protocol's keyword names. `docker/transactions/posix.py` now provides the four
delegating methods and uses the protocol parameter name `directory_fd` for the
five protocol members. The methods are additive; the existing L0 tests pass
(`tests.test_transactions_l0_posix`, 15 tests) and no source-only boundary
test regressed.

## Compatibility notes

- `DirectoryCapability` composes a `DirectoryDescriptor` rather than owning an
  fd directly. `isinstance()` checks against `DirectoryCapability` are
  unaffected. The only reintroduced slot is `_label`, which holds the
  caller-supplied transaction label returned by `DirectoryCapability.label`; it
  is not an fd or release-state field, and no code inspects `_fd`/`_closed`.
- The secure-path leaf is transferred from the foundation walk to the
  transaction capability with one explicit `detach()`; the source descriptor
  becomes terminal without closing, so the descriptor is closed at most once.

## Compatibility correction — restore pre-migration `from_secure_path` diagnostics

Date: 2026-10-07T03:07:53Z

The Phase 4 delegation layer initially translated every bounded foundation
failure through blanket `except UnsafeDescriptorError` / `except DescriptorError`
handlers that prefixed the message with `unsafe directory capability ...`.
That changed the observable `CapabilityError` text and causes for relative
paths, symlink/open (`ELOOP`) rejections, non-directory final leaves, and
foreign-owned final leaves, conflicting with task 4.5 and the design's
"preserve mapping at existing boundaries" requirement.

`DirectoryCapability.from_secure_path()` now maps every foundation failure back
to the pre-migration transaction wording while preserving the raw causes:

```python
except UnsafeDescriptorError as exc:
    cause = exc.cause
    if exc.stage == _FOUNDATION_OPEN_STAGE and cause is not None:
        raise CapabilityError(
            f"cannot open directory capability {name!r}: {cause}"
        ) from cause
    raise CapabilityError(str(exc)) from None
except DescriptorError as exc:
    cause = exc.cause
    if exc.stage == _FOUNDATION_VALIDATE_STAGE and isinstance(cause, OSError):
        carry_secondary_diagnostics(cause, exc)
        raise cause from None
    if cause is None:
        raise CapabilityError(str(exc)) from None
    raise CapabilityError(
        f"cannot open directory capability {name!r}: {cause}"
    ) from cause
```

Mapping by case:

| Case | Result | `__cause__` |
| --- | --- | --- |
| Relative path (`_STAGE_OPEN`, no cause) | `CapabilityError("directory capability '<name>' requires an absolute path")` | `None` |
| Root/child open failure (`_STAGE_OPEN`, `OSError` cause) | `CapabilityError("cannot open directory capability '<name>': <error>")` | the raw `OSError` |
| Symlink/`ELOOP` walk rejection (`UnsafeDescriptorError`, `_STAGE_OPEN`) | `CapabilityError("cannot open directory capability '<name>': <error>")` | the raw `OSError` |
| Final leaf not a directory (`UnsafeDescriptorError`, no cause) | `CapabilityError("directory capability '<label>' is not a directory")` | `None` |
| Final leaf foreign-owned (`UnsafeDescriptorError`, no cause) | `CapabilityError("directory capability '<label>' is not owned by the invoking user")` | `None` |
| Final-leaf `fstat` failure (`_STAGE_VALIDATE`, `OSError` cause) | raw `OSError`, secondary cleanup diagnostics carried | n/a (raised directly) |

`DirectoryDescriptor`'s generic error messages, `DirectoryDescriptor.adopt()`,
`DirectoryCapability.from_fd()` ownership behavior, the shared secure walk, and
all public signatures/exports are unchanged.

### Characterization tests

`tests/test_transactions_descriptor_integration.py` adds
`FromSecurePathCompatibilityTests` (9 tests) and strengthens
`FromSecurePathRootFailureTests` with an exact-message assertion. The new class
pins the exact message and `__cause__` for relative paths, root and child
`openat()` failures, injected `ELOOP` and a real intermediate symlink,
non-directory final leaves, and foreign-owned final leaves, plus label
preservation in the type/owner messages.

Verified as genuine characterizations by restoring the pre-migration
`HEAD:docker/transactions/capabilities.py` and running the new class:

```text
python -m unittest tests.test_transactions_descriptor_integration.FromSecurePathCompatibilityTests
  -> Ran 9 tests ... OK
```

### Phase 4 validation gates (re-run)

```text
python -m unittest tests.test_transactions_descriptor_integration \
  tests.test_transactions_l0_posix tests.test_transactions_l1_capabilities \
  tests.test_transactions_locking tests.test_transactions_l2_atomic \
  tests.test_transactions_l2_durable tests.test_transactions_l2_lifecycle \
  tests.test_transactions_phase9_boundaries
  -> Ran 238 tests ... OK   (descriptor integration 35)

python -m unittest tests.test_constructor_build_generation_integration \
  tests.test_constructor_build_cleanup \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_transactions_phase9a_specialized
  -> Ran 156 tests ... OK

ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

Tasks 4.8 and 4.9 remain checked because both gates pass reliably.

## Compatibility correction — mapped causes and transaction-label diagnostics

Date: 2026-10-07T03:17:31Z

Two further delegation regressions were corrected in the transaction adapter
only; the shared `DirectoryDescriptor` diagnostics and its non-empty-label
invariant are unchanged.

1. `DirectoryCapability.fd` and `DirectoryCapability.child_basename()` chained
   the foundation `DescriptorError` through `__cause__`. The pre-migration
   implementation raised bare `CapabilityError` instances, so both now raise
   with `from None` while keeping their existing transaction-facing messages:

   ```python
   @property
   def fd(self) -> int:
       try:
           return self._descriptor.fd
       except DescriptorError:
           raise CapabilityError(
               f"directory capability {self._label!r} is released"
           ) from None

   def child_basename(self, name) -> str:
       try:
           return self._descriptor.child_basename(name)
       except DescriptorError as exc:
           raise CapabilityError(str(exc)) from None
   ```

2. A validation-stage `UnsafeDescriptorError` exposed the foundation's
   normalized internal label (`<directory-capability>`) when the historically
   accepted `label=""` (or a non-string label) reached a final type/owner
   failure. The adapter now rebuilds the message from the caller-facing
   `target_label`, preserving the two established forms and raising with no
   cause:

   ```python
   message = str(exc)
   prefix = f"directory capability {_internal_label(target_label)!r} "
   if message.startswith(prefix):
       message = f"directory capability {target_label!r} {message[len(prefix):]}"
   raise CapabilityError(message) from None
   ```

   Open-stage (symlink/ELOOP) handling and the raw final-leaf `fstat()` unwrap
   are unchanged.

### Regression tests

`tests/test_transactions_descriptor_integration.py` adds
`MappedCauseRegressionTests` (6 tests):

- released `fd` access raises `CapabilityError` with `__cause__ is None`;
- an invalid child basename raises the exact transaction message with
  `__cause__ is None`;
- `from_secure_path(..., label="")` with a non-directory and with a
  foreign-owned final directory reports `directory capability '' ...` and never
  `<directory-capability>`;
- the same two cases for a non-string label (`Path("label")`) report
  `directory capability PosixPath('label') ...`.

`FromFdContractTests.test_successful_validation_transfers_sole_ownership` now
also asserts the released-`fd` error has `__cause__ is None`.

Verified as genuine characterizations by restoring the pre-migration
`HEAD:docker/transactions/capabilities.py`:

```text
python -m unittest \
  tests.test_transactions_descriptor_integration.MappedCauseRegressionTests \
  tests.test_transactions_descriptor_integration.FromSecurePathCompatibilityTests
  -> Ran 15 tests ... OK
```

### Phase 4 validation gates (re-run)

```text
python -m unittest tests.test_transactions_descriptor_integration \
  tests.test_transactions_l0_posix tests.test_transactions_l1_capabilities \
  tests.test_transactions_locking tests.test_transactions_l2_atomic \
  tests.test_transactions_l2_durable tests.test_transactions_l2_lifecycle \
  tests.test_transactions_phase9_boundaries
  -> Ran 244 tests ... OK   (descriptor integration 41)

python -m unittest tests.test_constructor_build_generation_integration \
  tests.test_constructor_build_cleanup \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_transactions_phase9a_specialized
  -> Ran 156 tests ... OK

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5425 tests ... OK (skipped=13)

ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

Tasks 4.8 and 4.9 remain checked because both gates pass reliably.

## Compatibility correction — carry cleanup diagnostics and restore `dir_fd`

Date: 2026-10-07T03:36:17Z

Two further delegation regressions were corrected.

### 1. Translated `CapabilityError`s retain foundation close diagnostics

`DirectoryCapability.from_secure_path()` replaced every bounded foundation
failure with a `CapabilityError`, but did not copy the foundation wrapper's
retained close/parent diagnostics onto the replacement. A validation failure
(non-directory or foreign owner) followed by a leaf close failure, or a walk
open failure (ordinary or `ELOOP`) followed by a retained-parent close
failure, therefore discarded the cleanup exception.

Each translated branch now constructs the local error, calls
`carry_secondary_diagnostics(error, exc)`, and only then raises it. The message
and chaining are unchanged: `from cause` for open failures and `from None` for
cause-free validation failures. The raw final-leaf `fstat()` unwrap keeps its
existing `carry_secondary_diagnostics(cause, exc)` treatment.

### 2. `PosixFileOps.openat`/`unlinkat` restore the `dir_fd` keyword

The migration renamed the first parameter of these two pre-existing methods
from `dir_fd` to `directory_fd`, breaking keyword callers. Both are restored to
`dir_fd` (the newly added `mkdirat`, `statat`, `listdir`, and `rmdirat` keep
the protocol's `directory_fd`).

The foundation's `DescriptorOps` protocol (and `PosixDescriptorOps`) keeps the
design-mandated `directory_fd` name, so the transaction adapter now presents
its backend through a small typed boundary:

```python
def _descriptor_ops(ops: PosixFileOps) -> DescriptorOps:
    return cast(DescriptorOps, ops)
```

`DirectoryDescriptor` only ever passes each descriptor positionally
(`openat(fd, ...)`, `unlinkat(fd, ...)`), so the two remain call-compatible.
The cast makes that compatibility explicit without renaming the public
backend method or altering the shared foundation/protocol. No specification
requirement was changed.

### Regression tests

`tests/test_transactions_descriptor_integration.py` adds
`CleanupDiagnosticCarryTests` (4 tests): non-directory and foreign-owner
validation failures followed by a leaf close failure, and ordinary and `ELOOP`
open failures followed by a retained-parent close failure. Each case asserts
the translated `CapabilityError` has the exact cleanup exception object in
`_transaction_secondary`, preserves its message/cause, and issues each close at
most once.

`tests/test_transactions_l0_posix.py` adds `KeywordCompatibilityTests` (2
tests): `PosixFileOps.openat(dir_fd=..., name=..., flags=..., mode=...)` creates
a file and `PosixFileOps.unlinkat(dir_fd=..., name=...)` removes it.

Both suites were confirmed to be genuine regressions by patching the
production code back to the pre-fix state:

```text
python -m unittest \
  tests.test_transactions_descriptor_integration.CleanupDiagnosticCarryTests \
  tests.test_transactions_l0_posix.KeywordCompatibilityTests
  -> Ran 6 tests ... FAILED (failures=4, errors=2)
     (4 empty-secondary failures + 2 `dir_fd` keyword TypeErrors)
```

After the fix, the same command reports `Ran 6 tests ... OK`.

### Phase 4 validation gates (re-run)

```text
python -m unittest tests.test_transactions_descriptor_integration \
  tests.test_transactions_l0_posix tests.test_transactions_l1_capabilities \
  tests.test_transactions_locking tests.test_transactions_l2_atomic \
  tests.test_transactions_l2_durable tests.test_transactions_l2_lifecycle \
  tests.test_transactions_phase9_boundaries
  -> Ran 250 tests ... OK   (descriptor integration 45)

python -m unittest tests.test_constructor_build_generation_integration \
  tests.test_constructor_build_cleanup \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_transactions_phase9a_specialized
  -> Ran 156 tests ... OK

python -m unittest tests.test_filesystem_cleanup \
  tests.test_filesystem_descriptor_operations \
  tests.test_filesystem_directory_descriptors \
  tests.test_filesystem_owned_descriptors
  -> Ran 134 tests ... OK

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5431 tests ... OK (skipped=13)

sh scripts/check-types
  -> All checks passed!
```

Tasks 4.8 and 4.9 remain checked because both gates pass reliably.

## Follow-up — unify `PosixFileOps` descriptor parameter names on `dir_fd`

Date: 2026-10-07T03:44:58Z

After restoring `dir_fd` on `openat`/`unlinkat`, the newly added
`mkdirat`, `statat`, and `rmdirat` still used the protocol name
`directory_fd`, leaving the single-descriptor methods inconsistent. All three
are now unified on the historical `dir_fd` (matching the existing `chmod` and
the `*_dir_fd` names of `linkat`/`renameat`); no caller used the
`directory_fd=` keyword for these methods.

`PosixFileOps` now consistently uses `dir_fd`; the foundation's `DescriptorOps`
protocol keeps the design-mandated `directory_fd`, and the small positional
`_descriptor_ops()` cast at the transaction adapter boundary continues to
bridge the two. No specification requirement changed.

### Verification

`tests/test_transactions_l0_posix.py` extends `KeywordCompatibilityTests` so
`mkdirat(dir_fd=..., name=..., mode=...)`, `statat(dir_fd=..., name=...,
follow_symlinks=...)`, and `rmdirat(dir_fd=..., name=...)` are exercised
alongside the existing `openat`/`unlinkat` keyword cases.

```text
python -m unittest tests.test_transactions_l0_posix \
  tests.test_transactions_descriptor_integration
  -> Ran 65 tests ... OK

python -m unittest tests.test_transactions_descriptor_integration \
  tests.test_transactions_l0_posix tests.test_transactions_l1_capabilities \
  tests.test_transactions_locking tests.test_transactions_l2_atomic \
  tests.test_transactions_l2_durable tests.test_transactions_l2_lifecycle \
  tests.test_transactions_phase9_boundaries
  -> Ran 253 tests ... OK

python -m unittest tests.test_constructor_build_generation_integration \
  tests.test_constructor_build_cleanup \
  tests.test_transactions_phase9a_build_cache_cleanup \
  tests.test_transactions_phase9a_project_state_cleanup \
  tests.test_transactions_phase9a_specialized
  -> Ran 156 tests ... OK

python -m unittest tests.test_filesystem_cleanup \
  tests.test_filesystem_descriptor_operations \
  tests.test_filesystem_directory_descriptors \
  tests.test_filesystem_owned_descriptors
  -> Ran 134 tests ... OK

python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5434 tests ... OK (skipped=13)

sh scripts/check-types
  -> All checks passed!
```

This supersedes the earlier note that `PosixFileOps` "uses the protocol
parameter name `directory_fd` for the five protocol members".
