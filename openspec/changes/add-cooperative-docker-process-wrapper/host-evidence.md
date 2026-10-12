# Host Docker protocol evidence

The production wrapper is not implemented. The host collector probes real Docker
protocol behavior independently of the prerequisite implementation gate; it does
not certify the proposed cancellation policy or its shutdown budgets.

## Run manually

Requires Python 3.11+, a repository checkout (the collector imports its shared
filesystem foundation), Docker CLI/daemon access and an image providing `sleep`.
The current Docker context is used. Preload the image separately so an image
pull is not mistaken for cancellation latency:

```sh
docker pull busybox:1.36
python3 scripts/collect-docker-cancellation-evidence.py \
  --image busybox:1.36 \
  --output /tmp/docker-cancellation-evidence-$(date +%Y%m%d-%H%M%S)
status=$?
printf 'Collector exit status: %s\n' "$status"
```

The output directory must be new and is created private. Each command gets full
stdout/stderr files and a JSON record with argv, return code/error and monotonic
timing. `summary.json` retains Docker version/context command references,
assertions, the collector exit status and cleanup failures. `cid-polls.json`
records observation times and whether a complete ID was present. Review host
metadata before sharing the evidence directory.

## Coverage and safety

- Created-container stop followed by start: stop is not a future-start veto.
- Removal before start: a successfully removed ID cannot start.
- Concurrent start/removal (five samples by default): removal wins eventually,
  whether start succeeds first or is rejected. Transient execution is possible.
- Attached `run --cidfile` with bounded ID observation and exact-ID cleanup.
- Discarded create identity recovered via all three exact labels, including a
  created/stopped container; a different operation with the same PID survives.
- Final cleanup queries only the collector's unique operation tokens and removes
  validated full IDs, never broad PID/name/image/managed-label matches.

Options include `--races`, `--command-timeout` (20 seconds per command by default)
and `--docker`. These are evidence-tool limits, not production policy settings.
Timeout or missing daemon access produces failure, never a simulated pass.
All collector file I/O uses OwnedDescriptor and CleanupFailures, including
subprocess output files, CID reads and JSON publication. Independent cleanup is
attempted after errors/interruption; terminal descriptor release is never retried.
Cleanup failures also cause nonzero exit and preserve affected operation tokens.
If summary publication itself fails, the process exit status is authoritative;
the summary may be missing, partial or contain its pre-publication status.

The collector does not inject daemon-create latency or lost RPC responses, prove
all race interleavings, or execute the future wrapper. Its recovery fixture
intentionally discards a successful create response; it is not a lost-response
simulation. An empty sweep cannot establish settlement of an unresolved create
request. Deterministic policy/fault tests and supported-version host results
remain pending implementation obligations, including task 4.8 and task 1.3.

## Historical initial collector verification

The results below precede the owned-I/O correction. See
`owned-io-verification.md` for the correction's fault matrix and current results.

No real Docker daemon verification was performed in the agent container.
Offline tests cover ID validation, jitter/deadline clipping, exact label filters,
missing-Docker failure evidence and refusal to overwrite existing evidence.
All commands below exited 0:

- Full pytest: 6644 passed, 13 skipped, 11 warnings, 17775 subtests; log
  `/tmp/docker-constructor-pytest.oKl5e7.log`.
- Configured unittest discovery: 6410 tests, 13 skipped, OK; log
  `/tmp/docker-cancellation-unittest.RBxMAf.log`.
- `ty check docker --python-version 3.14 --output-format concise`; log
  `/tmp/docker-cancellation-typecheck.kd0Z6c.log`. This configured check covers
  `docker`, not the host script; the script's mechanics are covered by tests.
- Strict OpenSpec validation and `git diff --check`.

These logs verify local changes only, not daemon behavior or Docker-wrapper
acceptance. No pending implementation or host-run checkbox was marked complete.
