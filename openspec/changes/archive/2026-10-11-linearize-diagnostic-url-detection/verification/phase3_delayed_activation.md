# P3 follow-up — delayed URL activation

**Current status: P3 PASS.** The initial registry cleanup blocker below was
subsequently fixed with user authorization and a deterministic regression.
See `registry_cleanup.md`: final full suite 6352 passed, exit 0; types pass.

## Implementation and behavioral evidence

The requested regression uses secret `Xhttps://host/pathZ` in host-path mode:
first feed `Xhttps://host/path` returns nothing; `?q` immediately returns
`Xhost/path`, with no early facts; ` done` returns only ` done` and settles
eligible host/path facts. Clean finish adds no marker or duplicate component.
The same recognizer persists and source is fed exactly once.

Recognition now records the exclusive raw end of the full separator in
`RecognizedMatch.separator_end`. Activation uses that coordinate rather than
the activation-time consumed offset, including nested percent spellings. The
extra field is coordinate-only; no source payload or replay is introduced.
A single bounded activation snapshot recovers already-crossed authority/path
boundaries using the selected interpretation depth and bounded decoder units.
Provenance visits are included in lifetime work counters. Settled components
are delivered during the activation feed.

Boundary recovery also exposed static component-helper overmasking: hostname
and path were substrings of the registered but mismatched crossing secret.
At activation after that mismatch, component validation excludes only patterns
which still matched the preceding withheld prefix (the snapshot before the
mismatching source symbol, or the full snapshot at clean EOF). This does not
remove patterns from recognizers or disable raw/decoded coordinate coverage.
Other component secrets remain active; tests verify separate host and path
masking. The filtered tuple refers to registration strings, not source payload.
Original-source fact validation continues independently of rendering.

## RED and checks

- `phase3_delayed_activation_red.log`: 1 failed, 38 passed, 11 subtests,
  exit 1. The requested activation feed returned only `X`.
- `phase3_delayed_activation_development.log`: after coordinate/boundary repair,
  the same regression returned `X<redacted-host>/<redacted-path>`, exposing
  static helper overmasking; 1 failed, 38 passed, exit 1.
- `phase3_current.log`: final P3 suite, 41 passed / 11 subtests, exit 0.
  Three new tests cover exact per-feed output and coordinates, literal/nested
  encoded boundaries and all partitions, and other component-secret coverage.
- `phase3_projector_differential.log`: 33558 partition comparisons including
  ordered facts/fingerprints, exit 0.
- `phase3_incremental_bounds_probe.log`: single-owner / lifetime audit passes,
  including identical work for character versus one-feed execution, exit 0.
- `phase3_types.log`: `./scripts/check-types` passes, exit 0.
- `phase3_predecessor_audit.log`: P2 allocation audit passes, exit 0.

All Python tests used the project venv and eight loadfile workers. Original
pre-fix gate logs were copied to `historical-phase3/*_before_delayed_activation.log`
before the new checks. Full outputs and actual statuses are preserved.

## Initial full-suite blocker (historical failure, subsequently resolved)

The unchanged mandated full-suite command was run without exclusions:

```
.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q
```

`phase3_validation.log`: **1 failed, 6350 passed, 13 skipped, 11 warnings,
17535 subtests passed**, exit 1. Failure:

```
tests/test_bound_pi_assembly_execution_phase6_registry.py::TestPhase6RegistryHarness::test_fixture_tarball_and_lock_are_deterministic_and_validly_bound
tests/phase6_acceptance_harness.py:154: in __exit__
    self._loop.call_soon_threadsafe(self.controller.release.set)
RuntimeError: Event loop is closed
```

This is a registry harness cleanup error, outside the changed production and
regression-test files. It is not assumed to be pre-existing or declared fixed.
No harness, scheduler, budget, warning filter or test exclusion was changed.
Verification was paused for guidance rather than silently retrying/suppressing
this failure. The user then authorized investigation; the race was reproduced
and fixed, not suppressed. The failed run is preserved in
`historical-phase3/phase3_validation_registry_cleanup_failure.log`. Final-state
verification is in `registry_cleanup.md`; P3 is now 14/14, overall 44/72, with
P4–P6 untouched.
