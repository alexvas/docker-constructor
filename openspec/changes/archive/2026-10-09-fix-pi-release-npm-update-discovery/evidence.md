# Verification Evidence: fix-pi-release-npm-update-discovery

Recorded during implementation of tasks 1.1, 2.1, 2.2, and 3.1. This file is
implementation evidence only; it is not a specification artifact.

## Code change

- `docker/versioning/providers/npm.py`
  - `NpmProvider.discover` now admits `NpmSource` **or** `PiReleaseSource`
    (`isinstance(source, (NpmSource, PiReleaseSource))`) and reuses the
    existing package-based registry request, stable filtering, publication
    time extraction, semantic comparison, and result construction.
  - Every other source type keeps the existing source-type skipped
    diagnostic (`expected npm source, got <Type>`).
- `tests/versioning/providers/test_npm.py`
  - Added `test_pi_release_source_queries_encoded_package_and_yields_candidate`.
  - Added `test_pi_release_source_honors_stable_filter_and_publication_time`.
  - Added `test_unrelated_source_returns_skipped_diagnostic`.
- `tests/test_version_updates.py`
  - Added `TestPiReleaseNpmDiscovery.test_pi_target_is_outdated_and_fragment_retains_release_metadata`,
    which drives the real `NpmProvider` through `check_updates()` against the
    canonical `docker-constructor.toml`, asserts the Pi target is
    `outdated` + applicable, and asserts the rendered replacement fragment
    retains `type = "pi-release"`, `release_repository`,
    `release_tag_prefix`, package, and the candidate version.

## Commands and results

### OpenSpec strict validation

```
$ openspec validate fix-pi-release-npm-update-discovery --strict
Change 'fix-pi-release-npm-update-discovery' is valid
exit=0
```

### Type check

```
$ ty check docker --python-version 3.14 --output-format concise
All checks passed!
exit=0
```

### Focused version-provider and update-discovery tests

```
$ python -m unittest tests.versioning.providers.test_npm tests.test_version_updates \
    tests.test_constructor_replacement_headers tests.test_constructor_check_updates_acceptance
Ran 65 tests in 0.027s
OK
```

Focused `npm` provider suite in isolation:

```
$ python -m unittest tests.versioning.providers.test_npm
Ran 28 tests
OK
```

Update/orchestration + suggestion suite in isolation:

```
$ python -m unittest tests.test_version_updates
Ran 25 tests
OK
```

### Full project test suite (green loop)

```
$ python -m unittest discover -s tests -p 'test_*.py'
Ran 5694 tests in 96.709s
OK (skipped=13)
```

## Regression scope

- Inventory schema: unchanged; no schema fields touched.
- Reporting/JSON contracts: unchanged; provider only gains source admission.
- Pi materialization/installation: unchanged; `pi-release` source metadata is
  still retained and used by materialization.
- Provider compatibility boundary: stays closed to explicitly listed source
  types; unrelated sources still skip.
