# Minimal network-resource repairs (not full change acceptance)

The user explicitly requested immediate minimal repairs for all three discovered
families while leaving the broader improvement plan in this change. No task
checkboxes or specification/design/proposal content were changed by this patch.
No commit, staging, unstaging or index operation was performed.

## Implemented scope

- `docker/versioning/transports.py`: the buffered HTTPError response is released
  through one `OwnedStream`, including when reading fails. Existing status,
  header/body values and error-handler boundaries remain. A sole ordinary close
  failure here propagates from the HTTPError handler; the broader plan's uniform
  operational mapping is not claimed implemented.
- `docker/versioning/artifact_cache.py`: close an acquisition HTTPError through
  one `OwnedStream` before propagating it. Retain the existing native response
  context manager for successful streaming responses. Explicitly finalize a
  closeable chunk iterator in the artifact consumer, including retained
  generators when processing fails; plain iterators remain supported. Use the
  existing shared accumulator to preserve an active operation over ordinary
  iterator-close failure and retain independent outer temporary/lock cleanup.
- `docker/networking.py`: prepare a narrow listener adapter and its sole
  `OwnedStream` before listener construction; bind the returned server before
  address inspection or thread construction. Startup failure cleans the listener
  without shutdown/join of a never-started thread. Stop detaches references before
  fallible cleanup, attempts shutdown, owner release and started-thread join via
  one accumulator, and cannot repeat listener release on ordinary repeated or
  reentrant stop. Reject an overlapping sequential start instead of replacing
  the owned listener.
- Regenerate the existing `phase6bd-process-inventory.json` through its generator
  for changed source hashes and dependency edges. Its equality test remains
  unchanged. This is derived-evidence maintenance, not original Phase 7 acceptance.

The broad plan remains pending: pre-acquisition HTTP release preparation,
normal-response migration to shared ownership, all malformed/exception acquisition
seams, actual serving-loop readiness, thread-start-then-raise races, full concurrent
start/stop coordination, worker-safe deferred listener release, retained winning
outcomes/incomplete status, shared stop deadlines, diagnosis-finalization policy
and expanded complete-tree network-origin enforcement. In particular, this minimal
stop path still releases before joining and does not claim to safely coordinate an
arbitrarily stalled worker or to retain outcomes after an incomplete stop.

## Tests

`tests/test_network_resource_minimal_cleanup.py` adds 29 regression cases covering
buffered/streamed HTTPError release, close-only and operation-plus-close failures,
interruption identity, explicit retained-iterator finalization, owner preparation
and server/thread construction failures, startup-plus-close failure, repeated and
reentrant stop, independent shutdown/close/join attempts, and actual loopback
listener/worker release. Descriptor-less doubles acquire no OS descriptors; the
real listener is released through its production filesystem owner.

## Verification

Complete output and runner statuses are preserved in the following logs:

- Development RED attempts:
  `/tmp/docker-constructor-minimal-network-red.tR7wbR.log` and
  `/tmp/docker-constructor-minimal-network-red.hQPxfs.log`, both exit 2. Existing
  missing cleanup was exposed, but the tests also let injected KeyboardInterrupt
  escape from a second cleanup/finalizer. These are unsuccessful development
  runs, not passing verification. Test disposal was corrected to avoid replacing
  assertions, and repeat-stop exceptions are now reported as explicit failures.
- Initial focused gate:
  `/tmp/docker-constructor-minimal-network-focused.c23Njj.log`, 122 passed,
  exit 0, before adding the extra lifecycle cases.
- Intermediate full suite:
  `/tmp/docker-constructor-pytest.G2AyUi.log`, 7861 passed, 13 skipped,
  11 warnings, 17753 subtests passed, exit 0. The accompanying static run
  `/tmp/docker-constructor-minimal-network-static.wmSeTR.log` failed typecheck
  with exit 1 because generic Iterator does not satisfy contextlib.closing's
  close protocol. Replaced that implementation with explicit callable checking
  and shared cleanup, also adding the operation-plus-iterator-close regression.
- Final full suite:
  `/tmp/docker-constructor-pytest.WJsk4D.log`, **7862 passed, 13 skipped,
  11 warnings, 17753 subtests passed**, 51.67s, exit 0. Command:
  `.venv/bin/python -m pytest tests -n 8 --dist loadfile --tb=short -q`.
  Warnings remain unsuppressed multiprocessing/fork deprecations.
- Final static/validation:
  `/tmp/docker-constructor-minimal-network-static.MStyg5.log`:
  `ty check docker --python-version 3.14 --output-format concise`,
  `git diff --check`, and strict validation of both this change and
  `eliminate-unsafe-direct-descriptor-closes` each exited 0.

Explicit unittest discovery and the full planned change acceptance were not run
or claimed for this minimal patch. The original Phase 7 blocker is not silently
removed by these repairs; its full convergence obligations remain pending.
