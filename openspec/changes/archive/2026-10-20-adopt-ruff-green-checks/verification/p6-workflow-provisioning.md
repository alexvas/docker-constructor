# P6 workflow provisioning — awaiting pushed CI verification

User directed implementation of the 6.11 provisioning in the existing GitHub
workflow, with remote execution to be checked after their push. This supersedes
the environment-related pause in `p6-prerequisite-probe.md`; that file is a
historical probe, not a current blocker to writing the workflow.

Checked base revision: `5ca7307ee9be494853b9eaf6ac40e01f871f5a17`.
Only implementation delta: `.github/workflows/validation.yml`, SHA-256
`c85c272b6af2fbf81048aec87d1850636bf350ca5b6038163760a60a8798b469`.
The Python inventory remains the 397-file P5 state. Evidence additions are
outside that inventory.

The retained Ubuntu/Python 3.14 job now provisions sudo/util-linux, the
`docker-dev` account/group, and verifies `nobody`, noninteractive sudo/runuser,
and a different invoking UID. It creates `.venv`, installs the declared dev
group plus pytest, pytest-xdist and the existing ty==0.0.73 pin, reports tool
versions, and exports the venv bin directory to subsequent workflow steps.
Push/PR triggers, pinned checkout/setup-python actions, read-only permissions,
and `scripts/check-types` are retained. No separate Ruff installation pin,
Docker image change, automatic commit, fix command or swallowed status was
introduced. Provisioning scripts use Bash `set -euo pipefail`.

## Local verification

Environment: Linux container, CPython 3.14.7, Ruff 0.17.1, pytest 9.1.1,
pytest-xdist 3.8.0; local ty 0.0.85. The clean-environment CI-pinned ty probe
is separately recorded in `p6-prerequisite-probe.md`.

Commands, executed sequentially under `set -e` with complete stdout/stderr
captured temporarily:

| Command | Exit | Result |
| --- | --- | --- |
| `scripts/check-python-lint` | 0 | All checks passed. |
| `scripts/check-python-format` | 0 | 397 files already formatted. |
| `scripts/check-python-compile` | 0 | Full inventory compiled. |
| `scripts/check-types` | 0 | All checks passed. |
| `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q` | 0 | 7993 passed, 13 skipped, 18026 subtests passed, 11 warnings; 53.39 seconds. |
| `git diff --check` | 0 | No whitespace errors. |

Warnings: 11 CPython multiprocessing/fork DeprecationWarnings in the build
lock/generation/transaction and transaction-locking tests. No warnings were
suppressed. Skip reasons were not freshly audited in this provisioning-only
check; the result is not represented as completed P6 G/skip-policy acceptance.

## Limitations

The privileged provisioning steps have not run in this non-root container.
The user's pushed GitHub run must confirm them. Task 6.11 remains unchecked
until clean CI provisioning is observed. Other P6 contracts and permanent
policy integration are not completed by this focused workflow change. No
remote success, full P6 exit, or P7 work is claimed. No staging or commit was
performed. Full temporary logs are not required as durable evidence links.
