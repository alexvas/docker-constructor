# P6 — configured check discovery (6.2)

Reviewed `pyproject.toml`, `.github/workflows/validation.yml`,
`pi-green-loop.json`, and `scripts/` on the current checkout.

| Configuration | Applicable command | Evidence |
| --- | --- | --- |
| CI production types, Python 3.14, pinned ty 0.0.73 | `./scripts/check-types` (executes `ty check --output-format concise docker tests/typing`) | `phase6_types.log` |
| Local green-loop types | `ty check docker --python-version 3.14 --output-format concise` | `phase6_configured_types.log` |
| Local green-loop tests | `.venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | `phase6_unittest.log` |
| Binding project verification | `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | `phase6_validation.log` |

The local discovery command uses the project Python rather than an unqualified
system Python. It is additional verification, not a replacement for pytest's
required eight-worker loadfile run. Both type commands use the installed `ty`
from PATH; no new tool is installed. The initial attempted `.venv/bin/ty`
invocation returned 127 because that executable does not exist. The full log
retains this command error and the corrected, configured PATH invocation's
successful status. This was not a typecheck failure or an accepted exception.

No Python lint, formatter or build command is configured in pyproject/CI/local
check configuration. `scripts/check-dockerfile` runs Dockerfile-only hadolint;
Dockerfile and `.hadolint.yaml` are unchanged and are outside this Python
change's affected checks. Host/registry validation scripts require their own
integration environments and belong to other changes, not an invented P6
requirement. Their Python unit coverage remains in the unfiltered full suite.
No warning suppression, exclusions, scheduler changes or fixture reductions
are used. Full outputs and actual statuses are preserved in the named logs.
