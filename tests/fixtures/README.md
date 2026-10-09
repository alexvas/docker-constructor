# Test fixtures

## `stable_inventory.toml`

A committed, reviewed, **test-owned** inventory document used by the
behavioural test lane. It is intentionally *not* a copy of the
repository's `docker-constructor.toml`.

Why it exists: behavioural tests must not change when dependencies are
bumped or optional Pi extensions are installed or removed. The fixture
therefore pins its own fixed tool versions, artifact URLs/digests, and
extension membership (`pi-read`, `pi-usage`, `pi-proxy`) with an
additional reviewed `pi-read` `0.3.0` alternate artifact for
override-selection scenarios.

The fixture must:

* load through the real production loader (`docker.versioning.inventory`)
  without reading the repository local companion configuration; and
* stay valid under the current production schema. A deliberate schema
  change may require a fixture update; a dependency bump must not.

## Two test lanes

### Behavioural lane

Tests that assert launcher, schema, artifact-selection, Pi-release,
progress, effective-state, and visual behaviour read the fixture through
`tests/inventory_fixtures.py` (or copy it into a temporary project with
`write_stable_project`). Expectations are exact and independent: they
must not be derived from the same selector/renderer under test.

### Live-contract lane

A small, explicit set of tests reads the repository's real
`docker-constructor.toml`. Those tests assert only structural and
cross-field invariants — successful load, selected-artifact/platform
agreement, Pi source contract, propagation to effective state, and
declared-owner visual headers. They must **not** pin dependency versions
or optional-extension membership, so a future bump needs no behavioural
expectation edits.

## Usage

```python
from tests.inventory_fixtures import (
    artifact_integrity,
    rewrite_artifact_integrities,
    stable_inventory_text,
    write_stable_project,
)

# Fresh project document (plus a minimal Dockerfile) in a temp dir:
inventory_path = write_stable_project(tmp_dir)

# Locally derived SRI for deterministic synthetic bytes:
integrity = artifact_integrity("https://example.invalid/a.tgz")

# Rewrite every url/integrity pair in a fixture copy:
document = rewrite_artifact_integrities(stable_inventory_text())

# Append a valid extension whose name contains a dot (quoted TOML key):
document = with_dotted_extension(stable_inventory_text())
```

Variant helpers used by the live-contract lane (all test-owned):

* `rewrite_artifact_integrities(text, algorithm=...)` derives valid SRI for
  any production-supported algorithm (`sha256`, `sha384`, `sha512`);
* `remove_extension(text, name)` / `with_empty_pi_extensions(text)` model
  installed/removed optional extensions;
* `with_dotted_extension(text, name="foo.bar")` appends an extension whose
  key must be quoted (`runtime.pi-extensions."foo.bar"`).

`tests/build_test_support.py` exposes the same stable document as
`INVENTORY_PATH` for shared build/orchestration consumers.
