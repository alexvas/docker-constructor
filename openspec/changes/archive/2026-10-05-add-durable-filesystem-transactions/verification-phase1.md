# Phase 1 Validation Record — L0–L2 Regular-File Substrate

## Run

Date: 2026-10-02T10:19:58Z

Change: `add-durable-filesystem-transactions`

This record was revised after four review rounds. The first localized
destination-collision handling, added staged error translation, and made
temporary cleanup retry-safe. The second reverted an unsafe inode-verified
quarantine experiment and restored atomic `renameat` replacement. A third
round briefly added a partial advisory lock layer; that layer was removed
because advisory locks are Phase 2 and depend on a completed Phase 1, so
introducing it here reversed the task dependency and bound capabilities to a
directory inode rather than the full normalized lock namespace. The fourth
round restores Phase 1 to standalone L2 leaf contracts, keeps the
capability-cleanup fixes, and documents that concurrent side-effecting
consumers coordinate through the Phase 2 lock layer.

Production files added:

```text
docker/transactions/__init__.py
docker/transactions/errors.py
docker/transactions/posix.py
docker/transactions/capabilities.py
docker/transactions/regular.py
docker/transactions/codec.py
```

Test files added:

```text
tests/test_transactions_l0_posix.py
tests/test_transactions_l1_capabilities.py
tests/test_transactions_l2_atomic.py
tests/test_transactions_l2_durable.py
tests/test_transactions_l2_lifecycle.py
tests/test_transactions_codec.py
tests/transactions_test_support.py
```

Commands:

```text
python -m unittest \
  tests.test_transactions_l0_posix \
  tests.test_transactions_l1_capabilities \
  tests.test_transactions_l2_atomic \
  tests.test_transactions_l2_durable \
  tests.test_transactions_l2_lifecycle \
  tests.test_transactions_codec

ty check docker --python-version 3.14 --output-format concise
python -m unittest discover -s tests -p 'test_*.py'
```

## Results

- RED state confirmed before implementation: all six focused modules failed with
  `ModuleNotFoundError: No module named 'docker.transactions'`.
- Focused Phase 1 suite after implementation: **126 tests, OK**.
- Typecheck: `All checks passed!`.
- Complete repository suite (regression guard): **4631 tests, OK (skipped=13)**, exit code `0`.

Boundary coverage (task 1.18). Every injection below is exercised by the
focused suite through `PosixFileOps`/`InjectedOps` with explicit assertion of
the selected contract, preservation of unrelated entries, primary-failure
authority, and owned-resource cleanup:

| Boundary | Behavior verified |
| --- | --- |
| `openat` | partial/`EEXIST` temp retries choose fresh names; exhaustion is an allocation failure, never `DestinationExists`; raw failure propagates; a generated private-name collision is never read, unlinked, or replaced |
| `read` | short reads terminate the read loop; raw failure propagates; symlink/non-regular/foreign/multi-link/wrong-mode leaves rejected through one no-follow descriptor |
| `write` | partial writes completed by `write_all`; zero-byte write is `EIO`; raw failure leaves no destination and cleans temp |
| `fstat` | validation reads the retained descriptor; unsafe leaves rejected without mutation |
| `fsync` | file fsync precedes publication; parent fsync precedes success; post-publication/parent-fsync failure reported as failure despite a visible destination |
| `linkat` | no-clobber publication; final `FileExistsError` maps to typed `DestinationExists`; unrelated destination preserved |
| `renameat` | durable replacement after destination validation; post-replacement parent-fsync failure remains failure |
| `unlinkat` | durable unlink validates one owned entry, retains the descriptor through unlink, and fsyncs the parent; unlink failure keeps the entry |
| `chmod`/`fchmod` | final mode established before visibility; forbidden-mode leaves rejected |
| `close` | every owned descriptor receives exactly one close attempt (success and failure paths); a close failure is fatal only when no primary failure exists, otherwise attached as secondary; a failed close is never retried; a second capability close is a no-op that issues no system call; non-I/O close exceptions (`KeyboardInterrupt`, cancellation) propagate unchanged |

- Temporary allocation: each `EEXIST` retries a fresh private name, collided
  entries are never read or mutated, three exhausted attempts fail at the
  allocation stage, and a temporary collision is never surfaced as
  `DESTINATION_EXISTS`.
- Durability: atomic no-clobber performs no directory `fsync`; durable
  no-clobber and durable replacement require the file/directory durability
  boundaries; a required post-publication parent fsync failure is fatal.
- Exception lifecycle: raw `OSError` subclass/`errno`/`__cause__` remain
  observable, `KeyboardInterrupt` and cancellation pass through unchanged,
  and cleanup/close failures never replace a primary failure.
- Codec: deterministic UTF-8 bytes, stable mapping order, generic decoding,
  and rejection of non-finite values during both encoding and decoding.
  Encoding rejects Python `nan`/`inf`/`-inf`; decoding rejects the
  non-standard `NaN`, `Infinity`, and `-Infinity` tokens (including nested
  forms) through a `parse_constant` callback that raises `ValueError`.
  Decoding also rejects numeric tokens that overflow to infinity (for example
  `1e400`, `-1e400`, and their nested forms) through a `parse_float` callback
  (`_parse_finite_float`) that applies `math.isfinite`; `parse_constant`
  alone does not see float tokens, so without this callback `1e400` decodes to
  `inf`.  Finite scientific notation such as `1e300` still decodes normally.
  Ordinary objects, arrays, strings, numbers, booleans, and null remain
  accepted, and no schema/version/path/envelope authority is added.
- Capability authority: `DirectoryCapability` and `FileCapability` can only be
  built by their validated factories.  Direct construction, including a forged
  `authority` argument, raises `CapabilityError` before any descriptor is
  touched, so a rejected construction neither closes nor mutates a
  caller-owned descriptor.  `FileCapability.assert_owned_by` re-checks the
  liveness of both the file capability and the directory capability before
  comparing identities, so a released file capability, a released directory
  capability, and a live cross-directory capability are all rejected with
  `CapabilityError`.

## Introspect Review (task 1.17)

Findings and resolutions:

1. **Descriptor accountability** — every owned descriptor receives exactly
   one close attempt on success and on every failure path
   (`_allocate_temporary` callers always reach `_close_fd` or `_discard`;
   `open_regular`, `validated_read`, `durable_unlink`, and
   `_validate_destination` close in `finally`). A failed close is not retried,
   so descriptor release is best-effort rather than guaranteed.
   `DirectoryCapability.from_fd` documents that ownership transfers only on
   successful validation, so a failed validation does not ambiguously close a
   caller-owned descriptor.
2. **Leaf following** — all leaf opens use `O_NOFOLLOW`; `linkat` uses
   `follow_symlinks=False`; `renameat` never follows. Symlink targets are
   verified unmodified by tests.
3. **Ancestor repair** — the substrate never chmods/chowns/creates ancestors;
   only a freshly created private temp is `fchmod`-ed to its final mode.
   Directory capabilities validate without repair.
4. **Pathname reconstruction** — capabilities call descriptor-relative
   operations (`openat(dir_fd, basename, ...)`); no `os.path.join`,
   `realpath`, `abspath`, or `/proc/self/fd` reconstruction exists. A source
   introspection test enforces this.
5. **Cross-filesystem promotion** — no-clobber `linkat` and replacement
   `renameat` use the same directory descriptor for source and destination;
   no path-based cross-device promotion is attempted.
6. **Partial writes** — `PosixFileOps.write_all` loops until every byte is
   written and raises `EIO` on a zero-byte write.
7. **Collision conflation** — temporary `FileExistsError` is consumed inside
   `_allocate_temporary`; only the final no-clobber commit maps to
   `DestinationExists`.
8. **False durability** — atomic publication never fsyncs; durable contracts
   require file fsync before publication and parent fsync before success, and
   treat a required post-publication parent-fsync failure as failure.
9. **Destination clobbering** — no-clobber contracts never replace; durable
   replacement validates the destination (regular, invoking-user-owned,
   single link) before `renameat`.
10. **Interruption translation** — only `OSError` is wrapped; `except
    BaseException` performs cleanup and re-raises `KeyboardInterrupt` and
    cancellation unchanged.
11. **Cleanup masking** — `attach_secondary` attaches ordinary cleanup/close
    diagnostics to the primary failure. Process-control interruptions
    (`KeyboardInterrupt` and cancellation) raised by cleanup are never
    consumed: they replace the primary failure and propagate unchanged.
12. **Generic VFS growth** — L0 exposes only the named descriptor-relative
    syscalls; no `exists`, `walk`, `listdir`, `mkdir`, `rmdir`, or
    path-oriented `replace`. L2 exposes only the five complete contracts.
13. **Domain authority leakage** — the codec grants no schema/version/path/
    deletion authority (enforced by a source introspection test); L2 carries no
    domain identity, retention, or cleanup-target authority.
14. **Blocking on non-regular leaves (resolved finding)** — validation/read
    opens added `O_NONBLOCK` so a FIFO leaf cannot block the open before it is
    rejected as non-regular. A FIFO regression test was added.
15. **Unvalidated construction** — `DirectoryCapability` and `FileCapability`
    require a module-private authority sentinel that only `from_path`,
    `from_fd`, and `open_regular` supply.  Direct or forged construction
    raises `CapabilityError` before touching the descriptor, and tests confirm
    the caller-owned descriptor is left open and unmutated.
16. **Stale ownership checks** — `FileCapability.assert_owned_by` first
    accesses `self.fd` and then `directory.fd`, so a released file capability
    or a released directory capability is rejected before the directory
    identity comparison; the live cross-directory case still raises
    `CapabilityError`.
17. **Float overflow bypassing non-finite rejection** — decoding `1e400`
    returned `inf` because `json`'s default float parser does not consult
    `parse_constant` for numeric tokens. `decode` now passes a
    `parse_float=_parse_finite_float` callback that converts the token and
    rejects any non-finite result, so positive/negative overflow and nested
    overflow raise `ValueError` while finite notation such as `1e300` still
    decodes.

No unresolved findings remain.

## Corrective Review Pass

A second review found and closed four defects. A third review briefly added a
partial advisory-lock layer that was then removed as out of scope, and also
fixed capability cleanup. The final state is covered by the focused suite (126
tests).

### Destination-collision localization
`DestinationExists` is now raised only by the final no-clobber commit
(`_link_no_clobber`). The broad `except FileExistsError` blocks were removed
from `atomic_no_clobber` and `durable_no_clobber`. Tests inject
`FileExistsError` from `fchmod`, `write`, `close`, and temporary cleanup and
prove each remains a staged operation failure
(`establish-mode`/`write`/`close`/`commit`) rather than a collision.

### Staged error translation
`fchmod` is wrapped by `_establish_mode` as a dedicated `STAGE_MODE` failure.
`DirectoryCapability.open_regular` and `_validate_destination` translate
`fstat` failures into staged `TransactionError`s that preserve the original
cause and close the descriptor. `durable_replace` has an `OSError` safety net
that wraps any remaining raw boundary with the current stage. Tests assert
stage, cause identity, errno, cleanup, and descriptor closure.

### Retry-safe temporary cleanup
`_unlink_temporary` no longer clears `state.temp_name` before a successful
`unlinkat`; it clears only after success or a confirmed `FileNotFoundError`.
A transient first-attempt failure is retried by `_discard` and the original
failure stays primary; a persistent failure retains the primary and records
the retry as secondary. `_rename_basename` clears the private name only after
the atomic commit succeeds.

### Atomic durable replacement; quarantine algorithm removed
The earlier quarantine design (rename the old destination to a private
sibling, verify its inode, then hard-link the new file) was removed. It
created a visible absence window and used ordinary `renameat` for both
quarantine allocation and restoration, so it could clobber a pre-existing or
concurrently created entry. `durable_replace` now commits with one atomic
`renameat(temp_name, destination_name)`:

- the destination name is never observably absent;
- there is exactly one commit boundary (one `renameat`, no `linkat`);
- a failed commit leaves the destination untouched and unlinks the tracked
  private temporary;
- a generated private-name collision is retried with a fresh unpredictable
  name and never read, unlinked, or modified;
- a failed commit never overwrites a concurrently created destination, and a
  cleanup failure stays tracked and is reported as secondary.

### Durable unlink without restoration
`durable_unlink` retains the validated no-follow descriptor until the
basename is unlinked, then flushes the parent directory. There is no
quarantine or restoration path, so a failed unlink leaves whatever currently
occupies the name untouched and never strands the validated entry under a
hidden private name. Tests assert that unlink precedes descriptor close and
that a failed unlink performs no `renameat`.

### Phase 2 lock integration deferred
The Phase 1 L2 APIs are standalone leaf contracts: `durable_replace` and
`durable_unlink` take no lock capability and enforce no namespace lock. L2
supplies the complete single-file mechanics and commits with one atomic
`renameat` or `unlinkat` relative to the directory descriptor. An adopting
layer that replaces or removes entries alongside other writers must
coordinate through the design's Phase 2 namespace lock; that lock layer is a
separate change whose tasks (2.1–2.8) depend on completed Phase 1.

A brief third-round attempt to add a partial lock capability was reverted. It
reversed the task dependency (Phase 1 depending on Phase 2) and bound the
capability only to the directory inode rather than the domain-selected
normalized lock namespace, so two different lock names in the same directory
could have authorized each other's protected operations. The complete lock
design — namespace identity, bootstrap races, post-acquisition revalidation,
contention policies, interruption, release failures, and namespace mismatch —
will be implemented and tested together in Phase 2. Phase 1 makes no claim to
protect against a same-user actor that mutates the namespace outside the
adopting layer's coordination.

### Primary-preserving capability cleanup
`DirectoryCapability.from_path` now captures the original `fstat`/
directory-validation failure, attempts to close the opened descriptor, and
attaches a close failure as a secondary diagnostic before re-raising the
original exception unchanged. `DirectoryCapability.open_regular` no longer
silently discards close failures; it attaches them as secondary diagnostics to
the primary `fstat`/validation failure. Tests inject `fstat`+`close` and
validation+`close` failures and assert the validation failure stays primary,
the close failure is available as secondary context, and no cleanup failure is
translated into `DestinationExists`.

### Single close attempt and release semantics
There is no guarantee that a descriptor is released after a failed `close()`.
POSIX does not promise the descriptor stays open when `close` reports an
error, and the descriptor number may already have been reused, so retrying a
failed close could close an unrelated descriptor. The substrate therefore
makes exactly one close attempt per owned descriptor:

- `DirectoryCapability.close` and `FileCapability.close` set the released
  state *before* attempting the close, so the capability is unusable after any
  attempt (including one raising `OSError`) and a second `close()` is a no-op
  that issues no further system call;
- `RegularFileContracts._close_fd` clears the tracked descriptor before the
  attempt, so a failed close is reported as `STAGE_CLOSE` and never retried;
- `validated_read`, `durable_unlink`, and `_validate_destination` catch only
  `OSError` from close: an isolated close failure becomes
  `TransactionError(STAGE_CLOSE)`, a close failure during an existing primary
  failure is attached as a secondary diagnostic, and `KeyboardInterrupt` or a
  cancellation-style non-I/O exception propagates unchanged;
- `_discard` attaches ordinary `OSError` close/unlink failures as secondary
  diagnostics and never lets them raise, so an ambiguity in cleanup cannot
  mask the primary failure; `KeyboardInterrupt` or a cancellation-style
  `BaseException` raised by `close` or `unlinkat` is not consumed and
  propagates unchanged.

### Interruptions always win over cleanup
Capability-validation cleanup follows the same policy: `DirectoryCapability.from_path`
and `DirectoryCapability.open_regular` catch only `OSError` from the cleanup
close and attach it as a secondary diagnostic, while `KeyboardInterrupt` and a
cancellation-style `BaseException` escape unchanged instead of being hidden
behind the earlier validation error.  `_discard` likewise catches only
`OSError` from `close` and `unlinkat`, so an interruption raised during
publication cleanup propagates as the exact injected instance.  Descriptor
close is still attempted at most once, and a failed close is never retried.

Tests assert one close call per attempt, unusability after an `OSError` close,
no second system call on a repeated close, isolated close failures surfacing
as `STAGE_CLOSE`, ordinary cleanup failures leaving the primary unchanged, and
the exact injected `KeyboardInterrupt`/cancellation instance propagating from
`validated_read`, `durable_unlink`, destination validation, capability
validation cleanup (`from_path`, `open_regular`), and `_discard` close/unlink
when a primary failure already exists.

`DirectoryCapability.__exit__` is exception-aware: when the context body has
an active exception, an ordinary `OSError` from close is attached as secondary
context and the body exception is re-raised as primary; with no active
exception, a close `OSError` propagates; and a `KeyboardInterrupt` or
cancellation raised by close still propagates unchanged even with a body
exception active.  Tests cover all four paths and assert the capability is
released after the close attempt.

### Float overflow rejection in the codec
`decode(b"1e400")` previously returned `inf` because numeric float tokens never
reach `parse_constant`.  `decode` now also passes `parse_float=_parse_finite_float`,
which converts the token with `float(...)` and raises `ValueError` when
`math.isfinite` is false.  Regression tests cover `1e400`, `-1e400`, `1e309`,
`-1e309`, overflowing numbers nested in arrays and objects, and confirm finite
scientific notation (`1e300`, `-1e300`, nested) still decodes.  The specification
files were left unchanged.

Re-run after the corrective pass:

```text
python -m unittest \
  tests.test_transactions_l0_posix \
  tests.test_transactions_l1_capabilities \
  tests.test_transactions_l2_atomic \
  tests.test_transactions_l2_durable \
  tests.test_transactions_l2_lifecycle \
  tests.test_transactions_codec

ty check docker --python-version 3.14 --output-format concise
python -m unittest discover -s tests -p 'test_*.py'
```

- Focused suite: **126 tests, OK**.
- Typecheck: `All checks passed!`.
- Full suite: **4631 tests, OK (skipped=13)**, exit code `0`.
