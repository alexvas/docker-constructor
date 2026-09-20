# Phase 6 verification

## Clean-cache rerun

**Date:** 2026-09-20T04:12:38Z  
**Environment:** reviewed `node:24.18.0-trixie-slim` image; Node `v24.18.0`; npm `11.16.0`.

Run from the repository root:

```bash
python -m unittest tests.test_npm_http_logging_research
```

Result:

```text
Ran 7 tests in 11.091s
OK
```

The harness creates a fresh independent project and npm cache for every
measured `(scenario, loglevel)` run. Cache-hit is the sole intentional
exception: its loglevel-specific cache is seeded by an unmeasured successful
run, then `node_modules` is removed before the measured run. The local TLS
CONNECT fixture never opens an upstream connection, so this rerun did not use
the public npm registry.

Another developer can reproduce the recorded **ACCEPT** decision by running
the command above in the reviewed image. The report contract, clean-cache
harness, sanitization checks, confirmed-live-output checks, and distinct-line
coalescing checks run as part of that command.

## Production scope confirmation

This rerun did not modify:

- the production npm invocation;
- the canonical npm policy or policy digest;
- assembler evidence fields, content, or evidence identity; or
- assembled-output/cache identity.

Research-only `--loglevel=http` and short retry/timeout values remained
confined to the Phase 6 harness during this rerun, so production policy was
unchanged by Phase 6. The accepted decision requires a subsequent
implementation phase to add `--loglevel=http` to the canonical reviewed npm
policy and update the corresponding policy digest, exact invocation, evidence
identity, and assembled-output/cache identity. The short retry/timeout values
remain research-only.
