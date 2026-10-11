# Phase 7 preliminary audit: scope clarification required

Implementation paused with 85/95 tasks complete. No task checkbox was changed and no production code was edited in this session.

## Newly encountered descriptor-release family

The preliminary complete-production-tree textual inventory identified subprocess pipe file-object releases outside the named Phase 1–6 characterization sites:

- `docker/npm_environment/execution.py::_close_stream_pipes`, line 324: `pipe.close()`; callers include lines 647, 674, 683, 708, 756, and 839. The process is created with `stdout=subprocess.PIPE, stderr=subprocess.PIPE` at line 603.
- `docker/npm_environment/lifecycle.py::close_descriptors`, line 163: `pipe.close()`; used by process reaping and lifecycle cleanup. Process creation uses `subprocess.PIPE` at lines 241–242; cleanup closes `proc.stdout` and `proc.stderr` at line 359.
- Additional direct file-object close branches appear in `execution.py` at lines 640 and 324 (the latter is the helper above).

These are file-object calls but release operating-system pipe descriptors, so they cannot simply be described as proven non-descriptor closes. They differ from the explicitly characterized `fdopen` wrappers: `subprocess.Popen` creates and exposes the file objects and their descriptor authority.

The binding contract forbids out-of-scope production consumer-owned releases. Task 7.3 also requires failing rather than initiating an uncharacterized refactor for sites absent from Phases 1–6. Clarification is needed on whether to expand planning with subprocess-pipe characterization and lifecycle migration, or explicitly revise the scope to distinguish subprocess-owned stream lifecycles from adopted integer-fd lifecycles. No exemption or refactor has been applied.

This is a preliminary inventory, not a completed alias-resolved production convergence audit. Tasks 7.1–7.10 remain unchecked.

## Baseline verification

- `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`: exit status 0; 6407 passed, 13 skipped, 11 warnings, 17579 subtests passed in 29.45 seconds. Complete stdout/stderr: `/tmp/docker-constructor-pytest.nmRzrB.log`. Warnings concern multiprocessing fork from multithreaded processes; none were suppressed.
- `ty check docker --python-version 3.14 --output-format concise`: exit status 0; `All checks passed!`.

These baseline checks do not establish completion of any Phase 7 task or replace the required convergence fixtures, generated evidence, unittest discovery, or release gates.
