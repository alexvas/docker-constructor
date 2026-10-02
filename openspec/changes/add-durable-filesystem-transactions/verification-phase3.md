# Phase 3 Validation Record — Immutable Build Generations

## Run

Date: 2026-10-02T15:29:56Z

Commands (run from the repository root with Python 3.14.7):

```bash
python -m unittest tests.test_constructor_build_generations
python -m unittest discover -s tests -p 'test_*.py'
ty check docker --python-version 3.14 --output-format concise
openspec validate add-durable-filesystem-transactions --strict
git diff --check
```

## Results

- RED state confirmed before implementation: the new suite failed at
  collection with `ModuleNotFoundError: No module named
  'docker.versioning.build_generations'`.
- Focused Phase 3 module after implementation: **55 tests, OK**.
- Complete repository suite (regression guard): **4746 tests, OK
  (skipped=13)**, exit code `0`. (Phase 2 left the suite at 4687; Phase 3 adds
  55 module tests plus 4 codec duplicate-key regression tests, 59 total.)
- Typecheck: `All checks passed!`.
- Strict OpenSpec validation: `Change 'add-durable-filesystem-transactions' is
  valid`.
- `git diff --check`: clean.

## Scope delivered (tasks 3.5-3.7)

New module `docker/versioning/build_generations.py` (build-domain L3) and test
suite `tests/test_constructor_build_generations.py`.

- **Canonical names** — `format_generation_name` emits
  `committed-build-<20 zero-padded decimal digits>.json`, rejecting booleans,
  non-integers, zero, negatives, and overflow (`> 10**20 - 1`).
  `parse_generation_name` accepts only an exact-width, nonzero, ASCII-digit
  suffix on the `committed-build-` prefix and returns `None` for everything
  else; because the width is fixed, lexical order equals numeric order.
- **Build-owned closed manifest** — `BuildManifest` has exactly `version` and
  `blobs`. `decode` rejects non-objects, missing or unknown fields, repeated
  object keys, non-integer or unsupported versions, non-list `blobs`, duplicate
  identities, out-of-order `blobs` lists, and noncanonical/unsafe keys (path
  components, unsupported algorithms, wrong length, non-lowercase hex).
  `encode` delegates byte determinism to the shared canonical JSON codec, which
  grants no field interpretation. No shared envelope performs schema, version,
  or identity validation.
- **Classification** — `inspect_generations` lists and validates every
  candidate, ignores the legacy `committed-build.json` and unrelated entries,
  and returns `EMPTY`/`STABLE`/`RECOVERABLE`. More than two generations,
  non-consecutive two-generation pairs, malformed generation names, unsafe
  entries (symlink, non-regular, foreign-owned, multiply linked, wrong mode),
  and corrupt manifests raise `BuildGenerationError` without deleting or
  repairing anything.
- **Discovery authority** — `discover_generations` requires a live,
  root-matching, namespace-matching `LockCapability` and synchronizes the
  generation directory with exactly one `fsync` before returning any
  inventory. `inspect_generations` deliberately grants no authority and issues
  no `fsync`. Both discovery and publication synchronize through the shared
  internal `_synchronize_generation_directory` helper, so error translation and
  synchronization behavior cannot diverge.
- **Publication** — `publish_generation` first validates the lock and performs
  fail-closed preflight inspection: ambiguous state (`RECOVERABLE`), invalid
  manifest input, and counter overflow are all rejected before any directory
  `fsync` or publication mutation. Only after those checks pass does it
  synchronize the generation directory (through the same helper as discovery)
  and then trust the inspected inventory to write `max(valid) + 1` once through
  the L2 durable no-clobber contract at mode `0600`. A generation left visible
  by a failed publication is therefore completed or rejected by this
  synchronization, never silently adopted and completed by the retry's own
  directory `fsync`. An existing generation is never modified or replaced.

## Boundary behavior

| Input / injected boundary | Observable outcome |
| --- | --- |
| zero generations | `EMPTY`, no authority-bearing generation |
| one valid generation | `STABLE`; authoritative only after directory `fsync` |
| two consecutive valid generations | `RECOVERABLE`; `previous`/`current` identified by number |
| two non-consecutive valid generations (for example 1 and 3) | `BuildGenerationError` before directory `fsync`, both files preserved |
| three or more generations | `BuildGenerationError`, nothing deleted |
| malformed candidate name (`committed-build-abc.json`) | `BuildGenerationError`, entry preserved |
| legacy `committed-build.json` (even a symlink) | ignored; never opened, adopted, or deleted |
| symlink / non-regular / foreign-owned / multiply linked / wrong-mode generation | `BuildGenerationError`, entry and any target preserved |
| corrupt manifest bytes | `BuildGenerationError`, bytes preserved |
| out-of-order `blobs` list in a persisted manifest | `BuildGenerationError`, bytes preserved (not normalized) |
| duplicate JSON object keys in a persisted manifest (`version`/`blobs`) | `BuildGenerationError` before directory `fsync`, bytes preserved (never last-wins) |
| discovery directory `fsync` failure | `BuildGenerationError`; all generations preserved |
| publication allocation / write / file `fsync` / commit failure | `BuildGenerationError`; no generation visible |
| publication directory `fsync` failure | `BuildGenerationError`; generation visible but not authoritative until a later successful discovery-time `fsync` |
| retry publication while the discovery-time directory `fsync` fails | `BuildGenerationError` before temporary allocation; no next generation, no `linkat`/`unlinkat` |
| retry publication after a visible generation and a successful discovery-time `fsync` | next generation published; discovery sync precedes temporary allocation, `write`, and `linkat` |
| counter at `MAX_GENERATION` | `BuildGenerationError` during preflight, before any directory `fsync` or `linkat`/`unlinkat` |
| released or wrong-namespace lock capability | `CapabilityError` before any inspection or mutation |

## INTROSPECT review (task 3.8)

1. **Counter ambiguity** — generation identity is the parsed fixed-width
   number, never a filename coincidence or timestamp. Duplicate numbers cannot
   occur because one canonical name maps to one number. A gap in the sequence
   is tolerated (`max + 1`), and two coexisting numbers are ordered
   numerically, so the lower is unambiguously the retained predecessor.
2. **Overflow** — `format_generation_name` and `parse_generation_name` both
   reject values outside `1..MAX_GENERATION`, and publication validates the
   manifest and formats the next name during preflight, before synchronizing
   the directory or touching any other filesystem state. Counter overflow at
   `MAX_GENERATION` therefore fails with zero `fsync`, `linkat`, and `unlinkat`
   calls.
3. **Replacement** — publication is one L2 `durable_no_clobber` (hard-link)
   commit; it never opens, truncates, chmods, or renames an existing
   generation. Malformed or corrupt candidates fail closed during inspection
   and never reach the commit.
4. **Authority before directory synchronization** — the initial (first-build)
   generation becomes authoritative through `durable_no_clobber`, which
   `fsync`s the directory before reporting success. Discovery accepts authority
   only in `discover_generations`, which `fsync`s before returning. The
   non-authoritative `inspect_generations` emits no `fsync` and is documented
   as granting no authority.
5. **Visible publication after a failed directory `fsync`** — the manifest may
   remain visible; the publishing call still reports failure. A later
   `discover_generations` completes the publication by synchronizing the
   directory, and its failure preserves every generation. `publish_generation`
   performs the same synchronization during preflight-to-publication transition,
   so a retry either completes the visible predecessor through a successful
   directory synchronization or fails before allocating its successor.
6. **Permissive JSON** — the build-owned schema is closed: unknown fields,
   missing fields, repeated object keys, unknown or non-integer versions,
   non-list `blobs`, duplicate identities, noncanonical keys, and non-finite
   numbers are all rejected. The shared codec consumes generic JSON values and
   rejects any repeated object key at any nesting depth, so an ambiguous
   manifest such as `{"version":999,"version":1,"blobs":[]}` or one with a
   duplicate `blobs` field cannot reach schema validation with a silently
   chosen last value.
7. **Generic-envelope leakage** — the schema, version, identity set, and
   authority live entirely in `build_generations.py`. The codec and L2
   contracts expose only bytes and generic JSON; no generic envelope, schema
   hint, or recovery contract is introduced.
8. **Malformed-state mutation** — every ambiguity path raises before any
   unlink, chmod, rename, or repair; `inspect_generations` and
   `discover_generations` are read-only apart from the discovery `fsync`.
   Verified by asserting `unlinkat == 0` and byte-for-byte entry preservation.
9. **Lock-capability mismatch** — both `discover_generations` and
   `publish_generation` call `LockCapability.assert_authorizes` with the
   generation directory and the fixed `build-generation` namespace before any
   inspection or mutation; released and cross-namespace capabilities are
   rejected with `CapabilityError`.
10. **Domain error boundary** — L2 `TransactionError` failures from
    `validated_read` and `durable_no_clobber` are translated to
    `BuildGenerationError` with the raw failure preserved as `__cause__`;
    process-control interruptions are not `TransactionError` and propagate
    unchanged.
11. **Robustness hardening** — `BuildManifest.from_blobs` now rejects a
    non-iterable input with `BuildGenerationError` instead of leaking
    `TypeError`, keeping the public schema surface domain-consistent. An
    unsorted `from_blobs` input is intentionally normalized, because
    `from_blobs` is the build-owned constructor that establishes canonical
    order.
12. **Persisted noncanonical order** — `BuildManifest.__post_init__` enforces
    canonical order, but `decode` previously routed persisted key lists through
    `from_blobs`, which silently sorted a noncanonical on-disk manifest and
    granted it authority. `decode` now validates each key, rejects duplicate
    identities, and compares the parsed key list against its sorted form before
    construction; an out-of-order persisted list raises
    `BuildGenerationError` and is never normalized. Regression test
    `test_out_of_order_blob_lists_are_rejected` pins the canonical positive
    control and the rejected reverse-order payload; a mutation check (feeding
    the same bytes through the previous `from_blobs` path) accepts and
    normalizes them, proving the test catches the defect.
13. **Directory enumeration** — discovery enumerates the open directory
    descriptor with `os.listdir` rather than reconstructing a pathname; this
    follows the existing build-cache precedent and does not widen L0's
    fault-injection surface.
14. **Publication adoption of an unsynchronized generation** —
    `publish_generation` previously called `inspect_generations` directly, so a
    generation left visible by a failed publication could be adopted and used
    to allocate the next number, with the predecessor's durability completed
    by the successor's own directory `fsync` rather than a synchronization of
    the pre-publication state. `publish_generation` now performs fail-closed
    preflight inspection (lock validation, ambiguous-state rejection, manifest
    validation, and overflow detection) and only then synchronizes the
    generation directory through the shared
    `_synchronize_generation_directory` helper before trusting the inventory
    and publishing. The lock is held throughout, so the synchronized inventory
    remains valid for the subsequent publication. Regression tests
    `test_visible_generation_is_not_allocated_without_discovery_sync` (failed
    synchronization raises before allocation, with the failing sync proven to
    be the generation-directory descriptor and no `linkat`/`unlinkat`) and
    `test_visible_generation_is_completed_by_discovery_sync` (successful
    synchronization precedes temporary allocation, `write`, and `linkat`) pin
    the contract. A mutation check that synchronizes before the overflow check
    makes `test_overflow_fails_before_any_mutation` fail on its zero-`fsync`
    assertion, and a mutation check that restores the direct
    `inspect_generations` call makes the recovery tests fail.
15. **Duplicate JSON object keys** — Python's `json.loads` silently resolves a
    repeated object key last-wins, so `{"version":999,"version":1,"blobs":[]}`
    and a duplicate `blobs` field previously reached the closed-schema check
    with the last value and could be granted authority, contradicting the
    fail-closed handling of ambiguous manifest contents. The shared codec now
    passes `object_pairs_hook=_reject_duplicate_keys`, which raises
    `ValueError` on any repeated key at any nesting depth; `BuildManifest.decode`
    translates that through its existing `ValueError` handler into
    `BuildGenerationError` before any field or identity is trusted. Schema and
    identity validation remain in `BuildManifest`; duplicate-key detection is
    generic JSON validation and neither normalizes nor resolves the ambiguity.
    Codec regression tests `DuplicateKeyTests` cover identical and conflicting
    repeated values, nested duplicates, and an ordinary-JSON positive control;
    generation tests `test_duplicate_version_fields_are_rejected` (including
    `999` followed by `1`), `test_duplicate_blobs_fields_are_rejected`
    (including an unsafe identity list followed by an empty list), and
    `test_duplicate_manifest_fields_fail_closed_before_synchronization`
    (discovery raises before directory synchronization with zero `fsync` calls
    and byte-for-byte preserved generation files) pin the behavior. A mutation
    check that removes the hook makes all 14 duplicate-key subtests fail.
16. **Non-consecutive recovery pairs** — publication is strictly
    `max(valid) + 1`, so a legitimate recoverable state can only be a pair
    `(n, n + 1)`. `inspect_generations` previously accepted any two valid
    generations and returned `RECOVERABLE`, so a state such as generations 1
    and 3 — impossible under monotonic publication and therefore ambiguous —
    was treated as authoritative. `inspect_generations` now rejects a
    two-generation pair whose second number is not the first plus one with
    `BuildGenerationError("non-consecutive build generations are ambiguous")`,
    before any `GenerationInventory` is returned. Regression tests
    `test_non_consecutive_generations_fail_closed_without_mutation`
    (`inspect_generations` raises, both manifests byte-for-byte preserved, zero
    `unlinkat`), `test_discovery_rejects_non_consecutive_generations_before_sync`
    (discovery raises with zero `fsync`, so ambiguity is rejected before
    authority), and the publication guard
    `test_publication_rejects_non_consecutive_generations_before_mutation`
    (no temporary allocation, `linkat`, `unlinkat`, or `fsync`) pin the
    contract. A mutation check that removes the consecutive test makes the
    classification and discovery tests fail; the publication guard holds
    independently because publication already refuses every `RECOVERABLE`
    state.

No unresolved findings remain.

## VALIDATE (task 3.9)

The focused suite injects a failure at every required publication boundary —
temporary allocation (`openat`), `write`, file `fsync`, no-clobber commit
(`linkat`), and directory `fsync` — and at the discovery directory `fsync`. It
confirms:

- no generation becomes authoritative until the durable no-clobber contract
  (including the parent-directory `fsync`) completes;
- publication preflight rejects a counter at `MAX_GENERATION` before any
  directory `fsync`, `linkat`, or `unlinkat` (zero-`fsync` assertion), and
  rejects ambiguous or invalid input before synchronization;
- a generation left visible by a failed directory `fsync` is completed only by
  a later successful generation-directory synchronization, and a failing
  synchronization grants no authority and deletes nothing;
- a publication retry against a visible, unsynchronized generation fails
  closed before temporary allocation when the pre-publication `fsync` fails,
  and when it succeeds that synchronization is proven to target the generation
  directory and precede temporary allocation, `write`, and the no-clobber
  `linkat`;
- every result is exactly zero-generation initial state, one stable
  generation, two recoverable generations, or fail-closed ambiguity;
- malformed names, unsafe entries, corrupt manifests, and excess generations
  fail closed without mutation;
- a two-generation state is accepted as `RECOVERABLE` only when the numbers
  are consecutive; a non-consecutive pair is rejected before directory `fsync`
  and preserved byte-for-byte;
- duplicate JSON object keys are rejected generically by the codec before any
  schema or identity validation, so an ambiguous manifest cannot be granted
  authority; discovery raises before the directory `fsync` and preserves the
  generation bytes;
- the legacy `committed-build.json` is outside discovery and is never
  inspected, adopted, rejected, or deleted.

## Dependency boundary

Phase 3 is additive. It composes the Phase 1 canonical codec and L2 durable
no-clobber leaf contract and the Phase 2 `LockCapability`. This review pass
adds generic duplicate-object-key rejection to the Phase 1 codec — a generic
JSON concern that leaves schema, version, and identity authority in the build
domain — and otherwise does not modify those layers. It does not yet replace
the mutable `committed-build.json` consumer, reconcile markers, or clean up
superseded blobs — those migrations and protocols belong to Phases 4 and 5. No
shared layer gains build-domain schema or authority.
