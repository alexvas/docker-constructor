# 6BC guide links — targeted verification

This records the user-approved documentation refinement, not Phase 6BC acceptance.
At the time of this targeted check, tasks 6BC.19–20 remained unchecked pending
the phase's full completion gates. Current phase acceptance is recorded in
`phase6b-verification.md`.

## Changes

- Process and assembler guides keep short executable usage examples and link to
  twelve concrete fault/protocol tests instead of embedding mock coordinators.
- Each test link has a source-line anchor and a Markdown title naming its exact
  function or class method.
- `tests/test_lifecycle_guide_links.py` resolves local document targets and uses
  AST definitions to verify test names and line anchors. Negative cases reject
  missing files, repository escapes, missing/invalid/stale anchors, wrong names,
  missing test titles and unchecked document fragments.
- 6BC.19–20 now explicitly require short usage examples, linked executable
  scenarios and automated link validation. No behavior or other phase scope was
  changed.

## Successful targeted checks

- Guide-link tests, executable process/assembler examples, and all twelve linked
  scenarios, using `.venv/bin/python -m pytest ... -n 8 --dist loadfile --tb=short -q`:
  **86 passed, 3 subtests passed**, runner exit **0**.
  Complete stdout/stderr and exit status:
  `/tmp/docker-constructor-6bc-guide-links.hw1bbe.log`.
- Strict OpenSpec validation and `git diff --check`: both exit **0**.
  Complete output and individual statuses:
  `/tmp/docker-constructor-6bc-guide-links-static.4Z2yDv.log`.

## Unsuccessful invocation (not acceptance)

An accidental `.venv/bin/python -m pytest` invocation ran without the required
xdist flags or output redirection and timed out after 120 seconds. It did not
produce a final runner summary/status or a complete saved stdout/stderr log.
It is unsuccessful verification and is not evidence of full-suite success.
The correctly logged targeted run above replaces it only for this documentation
change; the full Phase 6BC suite gate remains pending.
