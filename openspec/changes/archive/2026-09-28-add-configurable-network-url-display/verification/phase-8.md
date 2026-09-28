# Phase 8 verification evidence

Date: 2026-08-12

## RED documentation assertions

Command:

```text
python -m unittest tests.test_host_observability_acceptance_phase10.TestHostOutputDocumentation -v
```

Result before documentation/example updates: **FAILED**. The assertion output reported the stale `show_network_hosts` example and missing text/JSON channel, one-tail, typed propagation, direct-caller default, SDK/evidence/identity exclusion, disclosure, bounds, and operator-responsibility statements.

## Documentation and example validation

Command:

```text
python -m unittest discover -s tests -p '*documentation.py' -v
```

Result: **PASS** — 9 tests.

Command:

```text
python -m unittest tests.test_host_observability_acceptance_phase10.TestHostOutputDocumentation -v
```

Result: **PASS** — 1 test.

Command:

```text
python -m unittest tests.test_constructor_host_access_migration_red.TestLocalExampleExists -v
```

Result: **PASS** — 5 tests.

Command:

```text
python - <<'PY'
import tomllib
from pathlib import Path
from docker.versioning.local_project_configuration import validate_local_document
from docker.versioning.model import NetworkUrlDisplay
paths = sorted(Path('.').glob('*.example.toml'))
assert paths, 'no shipped TOML examples found'
for path in paths:
    parsed = tomllib.loads(path.read_text(encoding='utf-8'))
    config = validate_local_document(parsed)
    assert config.output.network_url_display is NetworkUrlDisplay.REDACTED
    print(f'{path}: TOML parsed and local schema accepted ({config.output.network_url_display.value})')
PY
```

Result: **PASS** — `docker-constructor.local.example.toml` parsed as TOML, passed the local schema, and resolved `network_url_display=redacted`.

Command:

```text
npx pi-green-loop check --affected docker-constructor.local.example.toml,docs/host-build-output.md,tests/test_host_observability_acceptance_phase10.py,tests/test_constructor_host_access_migration_red.py
```

Result: **PASS** — affected typecheck and test checks passed.

## Stale-guidance audit

Command:

```text
rg -n -i "show_network_hosts|show network hosts|multiple (bounded )?(retained )?tails|three .*tails|exact.*secret-safe" README.md README.en.md README.zh.md docs docker-constructor.local.example.toml || true
```

Result: no stale guidance. The sole match is the required statement that `exact` **is not secret-safe**. The shipped example set was enumerated with `/usr/bin/find`; `docker-constructor.local.example.toml` is the only `*.example.toml` outside change history.
