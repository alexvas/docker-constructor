# Follow-up: dedicated lifecycle enforcement package

Moved the eight lifecycle enforcement suites and their shared
`process_convergence.py` audit into `tests/enforcement/`. Added an initializer
for recursive discovery and a README describing scope and the focused command.
Behavioral/fault/real-pipe suites remain in their domain locations.

Updated package imports, repository-root resolution, documentation paths and
the checked executable-test anchor. AGENTS.md now references the directory,
not a growing list of individual gates. OpenSpec project guidance also points
to the directory. The guide-link validator accepts documented directories
only with a README, without test symbols or unchecked anchors; positive and
negative directory-link cases are covered. Guidance tests reject file lists
in AGENTS.md.

No production source, API, source-hashed architecture inventory, specification
or task checkbox changed in this follow-up. Earlier evidence retains its
historical paths/commands; current imports use `tests.enforcement`.

Verification (complete stdout/stderr and runner exit status preserved):

- `.venv/bin/python -m pytest tests/enforcement -n 8 --dist loadfile --tb=short -q`:
  **219 passed, 451 subtests passed**, 10.49s, exit 0.
  `/tmp/docker-constructor-enforcement-move.EVmugU.log`.
- `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`:
  **7779 passed, 13 skipped, 11 warnings, 17753 subtests passed**, 50.19s,
  exit 0. `/tmp/docker-constructor-pytest.qZopIX.log`.
  Warnings remain unsuppressed multiprocessing/fork deprecations.
- Typecheck, `git diff --check` and strict OpenSpec validation each exited 0:
  `/tmp/docker-constructor-enforcement-static.twfKLo.log`.
- No live old-path/package references remain outside historical OpenSpec
  artifacts. Normal recursive pytest discovery includes the moved suites.

No commit, staging or other index operation was performed.
