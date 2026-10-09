# Phase 2 Verification Record — Descriptor Operations and Owned Lifecycle

Date: 2026-10-05T14:00:26Z

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 2 only. Phases 3–8 remain unstarted.

## Production files added

```text
docker/filesystem/operations.py  # DescriptorOps protocol + PosixDescriptorOps adapter
docker/filesystem/descriptors.py # DescriptorError, UnsafeDescriptorError, OwnedDescriptor
```

## Test files added

```text
tests/test_filesystem_descriptor_operations.py  # 11 tests
tests/test_filesystem_owned_descriptors.py      # 35 tests
```

## Artifacts changed

```text
tests/data/filesystem_writer_matrix.json  # classify the new L0 adapter (writer inventory)
```

## Commands and results

RED evidence (before implementation):

```text
python -m unittest tests.test_filesystem_descriptor_operations \
  tests.test_filesystem_owned_descriptors
  -> ModuleNotFoundError: No module named 'docker.filesystem.operations'
  -> ModuleNotFoundError: No module named 'docker.filesystem.descriptors'
```

GREEN and phase gate (task 2.16):

```text
python -m unittest tests.test_filesystem_cleanup \
  tests.test_filesystem_descriptor_operations \
  tests.test_filesystem_owned_descriptors
  -> Ran 77 tests ... OK
     (cleanup 31, descriptor operations 11, owned descriptors 35)
```

Writer inventory boundary:

```text
python -m unittest tests.test_transactions_phase9_boundaries
  -> Ran 28 tests ... OK
```

Typecheck:

```text
ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

Full repository suite:

```text
python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5327 tests ... OK (skipped=13)
```

## Task-by-task evidence

- **2.1** `tests/test_filesystem_descriptor_operations.py` pins every
  `DescriptorOps` method and proves `PosixDescriptorOps` delegates
  `openat`/`fstat`/`listdir`/`close`, `mkdirat`/`statat`/`rmdirat`/`unlinkat`,
  `fchmod`, and no-follow vs follow `statat`, preserving the `OSError`
  subclass and `errno` (`ENOENT`, `ENOTDIR`).
- **2.2** `DescriptorErrorMessageTests`, `UnsafeDescriptorErrorMessageTests`,
  and `OwnedDescriptorConstructionTests` pin
  `DescriptorError(stage, message, *, cause=None)` field and `__cause__`
  chaining, inherited `UnsafeDescriptorError` construction, successful
  `OwnedDescriptor(ops, fd, *, label)` construction, no filesystem validation
  during construction, and rejection of negative/non-integer fd and
  empty/non-string label with zero operations while the caller's descriptor
  stays open.
- **2.3** `OwnedDescriptorStateTests` proves `close()` marks release before
  invoking close (observed through a pre-close hook), and never invokes close
  a second time after success or after an injected `EIO` failure.
- **2.4** `OwnedDescriptorStateTests` proves `detach()` returns the live fd and
  issues no operation, makes the source terminal (fd access and second detach
  raise `DescriptorError`), and that a later `close()` issues no operation.
- **2.5** `OwnedDescriptorCleanupPrecedenceTests` proves an ordinary close
  failure is sole without a primary, secondary to an active primary (duck-typed
  aggregate and `DescriptorError` via `carry_secondary_diagnostics`),
  authoritative when it is a process-control interruption, and that a close
  failure never prevents an independent later cleanup action.  That last test
  injects the failure into the *inner* descriptor so it occurs first, then
  proves the *outer*, independent descriptor is still closed afterwards (the
  raised error is the injected inner failure, the outer close runs after it,
  and each descriptor is released exactly once).
- **2.6** `DescriptorOps` is a `typing.Protocol` exposing exactly the nine
  design operations.
- **2.7** `PosixDescriptorOps` implements the protocol over the corresponding
  descriptor-relative `os` calls; delegation and errno-preservation tests pass.
- **2.8** `DescriptorError` and `UnsafeDescriptorError` retain `stage`/`cause`
  and chain a supplied cause; `UnsafeDescriptorError` inherits the constructor
  unchanged.
- **2.9** `OwnedDescriptor(ops, fd, *, label)` validates arguments before
  accepting ownership and performs no filesystem validation.
- **2.10** The live/transferred/release-attempted state machine backs guarded
  `fd`, `label`, and `released` properties.
- **2.11** `close()` is an irreversible at-most-once release attempt.
- **2.12** `detach()` is an explicit raw-fd transfer that makes the source
  terminal without closing.
- **2.13** `__enter__`/`__exit__` use the Phase 1 cleanup precedence
  (`CleanupFailures` with `ordinary=(OSError,)`), and re-raise an authoritative
  interruption.
- **2.14** `DescriptorOpsSignatureTests` and `DescriptorSignatureTests` pin the
  exact parameter names/kinds/defaults for both operations surfaces and for
  `DescriptorError`/`UnsafeDescriptorError`/`OwnedDescriptor`, reject variadic
  constructor options, and prove inherited error construction is unchanged.
  The `OwnedDescriptor` coverage includes `__exit__(self, exc_type, exc, tb)`
  and the `fd`, `label`, and `released` property getters, asserting each is the
  design-specified property and each getter's `(self)` parameters and return
  annotation (`int`, `str`, `bool`, and `None` for `__exit__`).
- **2.15** `FoundationAuthorityBoundaryTests` asserts both modules import only
  the standard library and `docker.filesystem`, never a domain package, and
  contain no `os.walk`/`os.scandir`/fsync/fcntl/flock/rmtree authority, no
  path-derivation helpers, and no recursive functions.
- **2.16** The gate command above passes: 75 tests, OK.

## Scope note

The change proposal's Impact section explicitly lists "architecture allowlist
tests" as affected. `tests/test_transactions_phase9_boundaries.py::
WriterInventoryTests` derives the set of production modules containing writer
markers from `docker/**/*.py` and requires each to be classified in
`tests/data/filesystem_writer_matrix.json`. `docker/filesystem/operations.py`
legitimately contains the `os.mkdir`/`os.unlink`/`os.rmdir` delegation of the
new L0 adapter, so it was added to the matrix as an L0 shared foundation
adapter (mirroring `docker/transactions/posix.py`). No writer authority was
broadened; the inventory stays exhaustive.
