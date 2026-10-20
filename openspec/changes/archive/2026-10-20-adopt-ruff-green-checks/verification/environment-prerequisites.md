# P1 environment and prerequisite manifest

Observed on the preparation host at the recorded source state. Prerequisites
are listed for each manifest check; a missing installable prerequisite is a
blocked check, not a pass.

## Observed environment

| Component | Observed | Evidence |
| --- | --- | --- |
| Python (project venv) | 3.14.7 (`/home/dev/.local/share/uv/python/cpython-3.14-linux-x86_64-gnu/bin/python3.14`) | `.venv/bin/python -V` |
| pytest | 9.1.1 | `.venv/bin/python -m pytest --version` |
| pytest-xdist | 3.8.0 | `.venv/bin/python -c "import xdist; print(xdist.__version__)"` |
| uv | 0.12.24 | `uv --version` |
| ty (local) | 0.0.85 from `uv tool` (`uv-receipt.toml` specifier `==0.0.85`); `ty version` prints the uninformative local string `ty: ok` | `uv tool list`, `/home/dev/.local/share/uv/tools/ty/uv-receipt.toml` |
| ty (CI pin) | 0.0.73 | `.github/workflows/validation.yml` install step |
| Docker CLI / daemon | absent (`docker: command not found`) | `command -v docker`, `docker info` |
| Ruff | not installed in P1 (P2 dependency) | `.venv/bin/pip list` contains no Ruff |

## Prerequisites per existing check

### E1 — mandated full pytest

- Tool: project Python 3.14 with `pytest` and `pytest-xdist`.
- Provision (reproducible):
  ```sh
  uv venv --python 3.14 .venv
  uv pip install --python .venv/bin/python pytest pytest-xdist
  ```
- External services: none. Some tests are documented environmental skips; see
  [`skip-inventory.md`](skip-inventory.md).
- Limitation: the two multiprocessing/fork `DeprecationWarning` families are
  emitted by the CPython test processes; they are recorded, not suppressed.

### E2 — type check

- Tool: `ty` on `PATH`.
- Provision (matches the CI pin):
  ```sh
  python -m pip install --disable-pip-version-check ty==0.0.73
  ```
  The preparation host uses `ty` 0.0.85 from `uv tool`, not the CI pin. Local
  success therefore does not by itself prove CI-pin success; P6/P7 verify the
  pinned CI environment. No ty diagnostics remain locally.
- External services: none.

### E4 — containerized Hadolint Dockerfile gate

- Tools: Docker CLI, running daemon, permission to pull the pinned image, and
  network/registry access for `hadolint/hadolint@sha256:27173fe2…`.
- Provision:
  ```sh
  # Install Docker Engine/CLI for the host, then:
  sudo systemctl enable --now docker    # or equivalent rootless setup
  docker info                            # must exit 0
  scripts/check-dockerfile
  ```
- Current limitation: Docker is not installed in this environment. The check
  exits 127 with its own diagnostic if invoked. This is a documented
  environmental limitation for a Dockerfile-scoped gate that this change does
  not exercise; it is not converted into a passing result.

## P2 prerequisites (not required in P1)

- Development-only `ruff==0.17.1` installed from the declared dependency group
  (`uv pip install --python .venv/bin/python --group dev` once P2 declares it).
- No runtime dependency or image inventory change is part of P1.
