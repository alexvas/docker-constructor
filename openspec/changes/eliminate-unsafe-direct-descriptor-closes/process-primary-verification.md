# Active operation primary precedence correction

`OwnedProcess.shutdown(primary=...)` now installs the exact active exception
as `CleanupFailures(primary)`, regardless of its Exception subtype. It does not
classify an operation exception as an unexpected cleanup defect. Existing
cleanup entries retain their independently declared classifications.

Corrected the precedence matrix's RuntimeError special case and added ValueError
coverage. Domain-exception regression covers both pipe positions and ordinary
versus unexpected close failures: exact authoritative/displaced identity,
retained preexisting diagnostics, unchanged cause, terminal independent releases,
and no duplicate diagnostics after repeated shutdown.

Verification (complete stdout/stderr retained):

- RED, owned-process lifecycle tests: exit 1, 6 failed / 90 passed;
  `/tmp/docker-constructor-process-primary-red.OH4eKV.log`.
- GREEN, lifecycle plus API/ownership/docs tests: exit 0, 114 passed in 1.75s;
  `/tmp/docker-constructor-process-primary-focused.uChRox.log`.
- Full `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`:
  exit 0, 6618 passed, 13 skipped, 11 warnings, 17775 subtests, 30.68s;
  `/tmp/docker-constructor-pytest.McsUWJ.log`. Warnings were not suppressed.
- `ty check docker --python-version 3.14 --output-format concise`: exit 0,
  All checks passed.
- `git diff --check`: exit 0, clean.

No specification/task checkboxes or index state changed.
