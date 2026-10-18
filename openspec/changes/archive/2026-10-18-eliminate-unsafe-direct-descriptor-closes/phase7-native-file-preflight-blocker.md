# Phase 7 preflight: local-file ownership defects delegated

## Status and scope

The user explicitly delegated these findings to
[own-local-file-stream-lifecycles](../own-local-file-stream-lifecycles/proposal.md).
**Phase 7 proceeds without the delegated local-file defects in its acceptance
scope and does not depend on their resolution.** The original pause for this
family is lifted; the file name is retained for traceability. Phase 7 remains
at 148/158 completed tasks, with 7.1–7.10 unchecked.

The delegation covers actual-stream ownership and cleanup precedence for local
file factories (builtin/`io.open`/`Path.open` and equivalent origins), the five
readers below, replacement of the staged `_FileRelease` proxy with prepared
direct stream adoption, and the associated local-file convergence, ledger,
packaging and implementation verification. The
[new tasks](../own-local-file-stream-lifecycles/tasks.md) remain entirely unchecked.

A staged minimal patch and prior green test runs do not accept the proxy or
prove repository convergence. The
[new design](../own-local-file-stream-lifecycles/design.md) records that provisional
baseline and plans its replacement. Creating this plan does not revert, unstage,
move or approve the staged code.

The separate [network-resource delegation](phase7-preflight-blocker.md) remains
in effect. Neither delegation excludes entire modules: original raw-fd,
directory, `mkstemp`, `fdopen` and subprocess lifecycle obligations remain in
scope. The complete-tree audit must report delegated origins separately rather
than call them safe, non-descriptor or a fourth allowed release category.
Unknown families still require a task 7.3 pause and characterization decision.

## Original candidate inventory

Before the staged minimal patch, AST inspection identified these native file
contexts. They are historical discovery evidence, not the new change's complete
local-file inventory:

| Production site | Acquisition |
| --- | --- |
| `docker/runtime_installer.py::read_projection` | `open(path, 'rb')` |
| `docker/runtime_installer.py::InstallContext.real_metadata._RealMetadataReader.read` | `open(meta_path, 'rb')` |
| `docker/versioning/cache.py::DiskCache.get` | `open(path, encoding='utf-8')` |
| `docker/versioning/build_materialization.py::_verify_hit` | `path.open('rb')` |
| `docker/versioning/verification.py::_load_build_projection` | `path.open('rb')` |

These are distinct from raw artifact fd ownership, `DiskCache.set()` and
`materialize_artifact()`'s `mkstemp`/`fdopen(closefd=False)` migration. The table
is a set of candidates requiring characterization, not a completed inventory
of every implicit filesystem acquisition or proof that all five exhibit the
same externally visible failure policy.

## Deterministic precedence reproduction (before the staged patch)

Executed in the project virtual environment, without network or OS descriptor
acquisition. A retained `io.BytesIO` subclass uses the native context-manager
exit path. Its read raises an exact `KeyboardInterrupt`; its close raises an
exact ordinary `OSError`. `Path.open` is patched to return that stream.

```python
import io
from pathlib import Path
from unittest.mock import patch
from docker.versioning.verification import _load_build_projection

primary = KeyboardInterrupt('projection read interrupted')
cleanup = OSError('projection file close failed')

class ProjectionFile(io.BytesIO):
    close_attempts = 0
    injecting = True

    def read(self, *args, **kwargs):
        raise primary

    def close(self):
        self.close_attempts += 1
        if self.injecting:
            raise cleanup
        super().close()

stream = ProjectionFile(b'')
observed = None
try:
    with patch.object(Path, 'open', return_value=stream):
        try:
            _load_build_projection(Path('/not-opened/projection.toml'))
        except BaseException as exc:
            observed = exc
    print('Observed exception:', type(observed).__name__)
    print('Original interruption preserved:', observed is primary)
    print('Ordinary close error replaced interruption:', observed is cleanup)
    print('Displaced interruption remains context:', observed.__context__ is primary)
    print('Native context close attempts:', stream.close_attempts)
    assert observed is primary, 'ordinary native file close masks the active KeyboardInterrupt'
finally:
    stream.injecting = False
    stream.close()
```

The final cleanup disposes of a descriptor-less double after injection is
turned off; it is not an example authorizing retry of an ambiguously closed
OS descriptor or production stream.

Complete stdout/stderr and runner status:
`/tmp/docker-constructor-phase7-native-file-preflight.gZ1rga.log`.

```text
Observed exception: OSError
Original interruption preserved: False
Ordinary close error replaced interruption: True
Displaced interruption remains context: True
Native context close attempts: 1
AssertionError: ordinary native file close masks the active KeyboardInterrupt
Preflight runner exit status: 1
```

This is an unsuccessful preflight, not passing verification or a real-fd leak
measurement. It demonstrates an ordinary close failure displacing an active
interruption in the production loader. The remaining candidates need their own
operation, cleanup, domain-mapping and ownership characterization.

## Phase 7 continuation

Resume the original change's Phase 7 work for its remaining approved scope.
Its tasks 7.1–7.6 still require complete-tree convergence, the Phase 1–6/6B
fd-ledger and current `verification.md` records. Delegation, five-reader tests
and the staged proxy's green runs do not satisfy those deliverables. Complete
the remaining release checks before accepting any corresponding task.

The local-file change independently owns direct-adoption implementation and
its complete-tree local-file proof. Do not require its completion before the
remaining Phase 7 acceptance, and do not silently absorb unrelated new families
into either delegation. No Phase 7 acceptance is claimed by this record.
