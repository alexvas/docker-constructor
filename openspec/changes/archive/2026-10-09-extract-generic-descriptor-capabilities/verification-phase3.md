# Phase 3 Verification Record — Directory Descriptor API

Date: 2026-10-05T14:22:43Z (hardened 2026-10-05T14:30:14Z, 2026-10-05T14:47:22Z, 2026-10-05T14:55:43Z)

Change: `extract-generic-descriptor-capabilities`

Scope: Phase 3 only. Phases 4–8 remain unstarted.

## Production files changed

```text
docker/filesystem/descriptors.py  # add DirectoryDescriptor + authority/adoption/walk
```

No other production file changed. `docker/filesystem/operations.py`,
`docker/filesystem/cleanup.py`, and the empty `docker/filesystem/__init__.py`
are unchanged from Phases 1–2.

## Test files added

```text
tests/test_filesystem_directory_descriptors.py  # 57 tests
```

## Artifacts changed

```text
openspec/changes/extract-generic-descriptor-capabilities/tasks.md  # 3.1–3.19 checked
```

## Commands and results

RED evidence (before implementation):

```text
python -m unittest tests.test_filesystem_directory_descriptors
  -> ImportError: cannot import name 'DirectoryDescriptor' from
     'docker.filesystem.descriptors'
```

GREEN and phase gate (task 3.19):

```text
python -m unittest tests.test_filesystem_cleanup \
  tests.test_filesystem_descriptor_operations \
  tests.test_filesystem_owned_descriptors \
  tests.test_filesystem_directory_descriptors
  -> Ran 134 tests ... OK
     (cleanup 31, descriptor operations 11, owned descriptors 35,
      directory descriptors 57)
```

Typecheck:

```text
ty check docker --python-version 3.14 --output-format concise
  -> All checks passed!
```

Full repository suite:

```text
python -m unittest discover -s tests -p 'test_*.py'
  -> Ran 5384 tests ... OK (skipped=13)
```

Whitespace:

```text
git diff --check
  -> clean
git diff --cached --check
  -> clean
```

## Task-by-task evidence

- **3.1** `ConstructionAuthorityTests` proves a direct
  `DirectoryDescriptor(ops, fd, label=...)` raises `TypeError` before any
  operation or ownership transfer, that `open_secure_path` and `adopt` are the
  only construction seams (no `from_path`/`from_fd`/`from_secure_path`), and
  that `DirectoryDescriptor` subclasses `OwnedDescriptor`.
- **3.2** `SecureWalkTests` proves a relative path is rejected before any
  operation; that `/a/b` opens `/` then `a` then `b` relative to each parent
  (never reconstructing a path); that only the leaf remains live; that a
  non-directory final component raises `UnsafeDescriptorError` and releases
  every descriptor; and that effective ownership is required by default and
  optional via `require_owner=False`.
- **3.3** `SecureWalkHandoffTests` proves a failed retained-parent close is
  attempted once and not retried, the already-open child is released exactly
  once and never returned, and an ordinary child-close failure is secondary to
  the parent-close failure.
- **3.4** `SecureWalkFailureTests` proves a component open failure, symlinked
  component, stat failure, process-control interruption, and unexpected defect
  each release every still-owned descriptor exactly once under Phase 1
  precedence.
- **3.5** `ChildBasenameTests` proves a valid basename is returned and that
  empty, `.`, `..`, slash, absolute, NUL-containing, and alternate-separator
  names are rejected before any operation.
- **3.6** `ChildDirectoryTests` proves `open_directory` opens one existing
  no-follow child without chmod, rejects a non-directory child, and secures the
  child to the explicit `mode` under `create_directory` and
  `open_or_create_directory`; `open_or_create_directory` opens an existing
  child, creates a missing one, never swallows a non-missing error, rejects a
  foreign owner, and child operations reject unsafe names before operating.
- **3.7** `PrimitiveOperationTests` proves `stat_child` uses the requested
  symlink policy, `list_names` returns the directory entries as a tuple,
  `unlink_child`/`remove_child_directory` target exactly one validated
  basename, unsafe names are rejected before operating, and primitive failures
  propagate the original `OSError`.
- **3.8** `DirectoryDescriptor.__init__` is guarded by the module-private
  `_AUTHORITY` token and raises `TypeError` before `super().__init__`, so a
  direct call never takes ownership. `adopt()` transfers ownership first, then
  validates directory type and requested ownership, releasing the adopted fd
  exactly once under Phase 1 precedence on failure.
- **3.9** `open_secure_path()` requires an absolute path, opens `/` then walks
  each component relative to its retained parent with
  `O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC`, and adopts only the leaf.
- **3.10** Intermediate parents are retained as `OwnedDescriptor`; a clean
  descent closes the previous parent once, and the terminal `finally` closes
  the retained parent then releases the capability only when the walk failed or
  the parent close did not succeed.
- **3.11** `child_basename()` implements the complete rejection contract
  (`""`, `.`, `..`, `/`, NUL, `os.sep`, non-`None` `os.altsep`).
- **3.12** `open_directory()` opens one existing no-follow child and returns
  its validated capability (`secure_mode=None`).
- **3.13** `create_directory()` validates its resolved label (non-empty
  string) before `mkdirat`, then calls `mkdirat` exclusively, opens, validates
  directory type and requested ownership, `fchmod`s to the explicit mode, and
  returns the adopted descriptor. A failing `fchmod` (ordinary `OSError`,
  process-control interruption, or unexpected defect) releases the adopted
  child exactly once through `_attempt_release` under shared cleanup
  precedence, and the original failure stays authoritative.
- **3.14** `open_or_create_directory()` validates its resolved label before any
  operation, then opens once. Only an `ENOENT` from that initial open triggers
  the creation fallback; failures from `fchmod`, validation, or cleanup
  propagate unchanged, and every non-missing open error (including
  symlink/`ELOOP` and foreign-owner failures) is re-raised.
- **3.15** `stat_child()`, `list_names()`, `unlink_child()`, and
  `remove_child_directory()` are single-basename primitive delegations.
- **3.16** `DirectoryApiSignatureTests` pins the exact parameter
  names/kinds/defaults and return annotations for both construction seams and
  every child method, including keyword-only `label`, `require_owner`, `mode`,
  and `follow_symlinks`, and rejects variadic parameters.
- **3.17** `FoundationApiSurfaceTests` proves the package initializer has an
  empty AST body, every module's public definition set exactly matches the
  design surface, no definition name contains a recursive-removal,
  retry/durability/lock/cache/namespace/npm token, and no module imports
  `shutil`/`fcntl` or calls `os.walk`/`os.scandir`/`os.fsync`/`os.replace` etc.
- **3.18** `FdLedgerTests` accounts for every allocated descriptor across a
  successful walk (then detach owns exactly one live fd and a later close issues
  no operation) and across component-open, stat, parent-close, and
  parent-and-child-close failures; every descriptor is closed at most once with
  no leak, and a failed parent close is attempted exactly once.
- **3.19** The gate command above passes: 134 tests, OK.

## Hardening — `fchmod` failure precedence (post-Phase-3)

`DirectoryDescriptor._open_validated_child()` previously caught only
`OSError` around `fchmod`, leaking the freshly opened child descriptor when the
call raised `KeyboardInterrupt`, `SystemExit`, or an unexpected defect. It now
catches `BaseException`: an `OSError` keeps the existing `DescriptorError`
translation, while every other failure stays primary; in all cases the child is
released exactly once through `_attempt_release` and the result re-raised under
the shared Phase 1 cleanup precedence.

Focused coverage added to `tests/test_filesystem_directory_descriptors.py`:

- `ChildSecuringFailureTests` exercises both `create_directory()` and
  `open_or_create_directory()` for an ordinary `OSError`, `KeyboardInterrupt`,
  and `RuntimeError`, asserting the original failure stays authoritative, the
  child fd is closed exactly once, only the parent remains live, and no child
  capability is returned. A close-failure variant confirms precedence and that
  the close diagnostic is attached as secondary to the original exception.
- `FdLedgerTests.test_securing_failures_release_every_child_fd_at_most_once`
  extends the fd ledger over the same paths and asserts every allocated child
  fd receives one close attempt with no leak and no double close.

RED evidence (temporary revert of the `except BaseException` clause):

```text
python -m unittest \
  tests.test_filesystem_directory_descriptors.ChildSecuringFailureTests \
  tests.test_filesystem_directory_descriptors.FdLedgerTests.test_securing_failures_release_every_child_fd_at_most_once
  -> Ran 5 tests ... FAILED (failures=12); leaking child descriptor 101
```

GREEN: the Phase 3 gate is 124 tests, OK; `ty check docker --python-version
3.14` passes; the full suite is 5374 tests, OK (skipped=13); `git diff --check`
is clean.

## Hardening — early label validation and narrowed creation fallback

Two latent descriptor leaks and one mis-scoped fallback were closed after
Phase 3:

- **Early label validation.** `open_secure_path()`, `open_directory()`,
  `create_directory()`, and `open_or_create_directory()` now resolve and
  validate their effective label (non-empty string, via the module-private
  `_validate_label()`) before the first `openat`, `mkdirat`, or `fchmod`. A
  late rejection by `adopt()` would otherwise strand an already-opened
  root/leaf descriptor. `label=None` keeps its previous default-label
  behavior, and public `adopt()` is unchanged: an invalid label is still
  refused before ownership transfers, so the caller retains the descriptor it
  passed in.
- **Narrowed missing-child fallback.** `open_or_create_directory()` no longer
  catches every `DescriptorError` from `_open_validated_child()` (the old
  `_is_missing_child()` predicate matched an `ENOENT` cause from *any* stage).
  The creation fallback is now scoped to the initial `ops.openat()` call and
  fires only for `errno.ENOENT` from that call; `ENOENT` from `fstat()` or
  `fchmod()` propagates under shared cleanup precedence and never reaches
  `mkdirat()`. The now-unused `_is_missing_child()` helper was removed, and the
  open/securing split lives in `_open_child_fd()` / `_secure_and_adopt()`.

Focused coverage added to `tests/test_filesystem_directory_descriptors.py`:

- `LabelValidationTests` exercises `label=""` and a non-string label across
  all four factories, covering both `/` and a multi-component secure path, and
  asserts rejection happens with no filesystem call and no leaked descriptor.
  It also confirms an invalid label rejected by public `adopt()` leaves the
  caller-owned fd open.
- `MissingChildFallbackTests` injects an independent `ENOENT` from `fstat()`
  and from `fchmod()` during `open_or_create_directory()`, asserting no
  `mkdirat()` occurs, the original cause is retained, the child receives
  exactly one close attempt, and the parent remains live. The existing
  `ChildDirectoryTests.test_open_or_create_directory_creates_a_missing_child`
  continues to prove an initial-open `ENOENT` still triggers creation.

RED evidence (pre-change index version restored temporarily):

```text
python -m unittest \
  tests.test_filesystem_directory_descriptors.LabelValidationTests \
  tests.test_filesystem_directory_descriptors.MissingChildFallbackTests
  -> Ran 5 tests ... FAILED (failures=12)   # leaked fds / spurious mkdirat
```

GREEN: the Phase 3 gate is 129 tests, OK; `ty check docker --python-version
3.14` passes; the full suite is 5379 tests, OK (skipped=13); both
`git diff --check` and `git diff --cached --check` are clean.

## Hardening — validate before mutating a child's mode

`_secure_and_adopt()` previously called `fchmod()` *before* adoption, so a
foreign-owned or non-directory child was chmod'd and only then rejected —
mutating a directory the capability does not own (potentially under
privileged execution). It now adopts first: `DirectoryDescriptor.adopt()`
validates directory type and requested ownership, and only a validated
descriptor is secured. The ordering now matches
`docker/npm_environment/storage.py` (open → `fstat` → type/ownership check →
`fchmod`).

- Adoption failure releases the fd once via its existing cleanup; the caller
  does not close it again.
- On a `fchmod` failure the adopted descriptor is released exactly once via
  `_attempt_release(descriptor.close, primary)`, keeping the `OSError` →
  `DescriptorError` translation and preserving interruptions/unexpected
  defects under shared cleanup precedence.
- `require_owner=False` still permits foreign ownership and applies the mode.

Focused coverage added to `tests/test_filesystem_directory_descriptors.py`:

- `ValidationBeforeSecuringTests` asserts the successful call order is
  `openat → fstat → fchmod` (with `mkdirat` first for `create_directory()`),
  and that foreign-owned and non-directory children are rejected with no
  `fchmod` and exactly one child close across both `create_directory()` and
  `open_or_create_directory()`. It also confirms `require_owner=False` permits
  foreign ownership and still secures the mode.
- The existing chmod-failure and missing-child fallback regression tests
  (`ChildSecuringFailureTests`, `MissingChildFallbackTests`, and
  `FdLedgerTests.test_securing_failures_release_every_child_fd_at_most_once`)
  still pass.

RED evidence (pre-change worktree, chmod-first):

```text
python -m unittest \
  tests.test_filesystem_directory_descriptors.ValidationBeforeSecuringTests
  -> Ran 5 tests ... FAILED (failures=6)
     (observed order openat -> fchmod -> fstat; foreign child chmod'd)
```

GREEN: the Phase 3 gate is 134 tests, OK; `ty check docker --python-version
3.14` passes; the full suite is 5384 tests, OK (skipped=13); both
`git diff --check` and `git diff --cached --check` are clean.

## Scope note

No architecture allowlist change was required. `docker/filesystem/descriptors.py`
adds no filesystem writer markers (all mutation is delegated through the
injected `DescriptorOps` protocol as `mkdirat`/`rmdirat`/`unlinkat`/`fchmod`),
so `tests/data/filesystem_writer_matrix.json` is unchanged and the writer
inventory remains exhaustive. The Phase 2 foundation-dependency AST checks and
writer-inventory boundary tests still pass.
