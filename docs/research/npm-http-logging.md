# npm `loglevel=http` research — Phase 6

## Command and environment

- **Image:** `node:24.18.0-trixie-slim` (the current reviewed container).
- **Node/npm:** `v24.18.0` / `11.16.0`.
- **Command:** `npm ci --ignore-scripts --no-bin-links --no-audit --no-fund`, once
  with `--loglevel=notice` and once with `--loglevel=http`.
- **Environment:** every measured `(scenario, loglevel)` has an independent
  project and `npm_config_cache`. Cache-hit cases independently seed their own
  cache before the measured run; every other measured run begins with an empty
  cache. The local TLS CONNECT fixture serves deterministic data for
  `registry.npmjs.org` and never contacts the public network.

## Evidence

The harness runs cache-miss, cache-hit, retry (503), timeout (withheld
response), and high-volume (24 controlled 503 retries) cases at both log
levels. It observes first bytes at binary `os.read` pipe boundaries and records
exit observation; all ten cases confirmed output while the process was still live.

### Raw fixtures

Representative raw HTTP/stderr lines from the controlled harness, in scenario
order. They intentionally retain the canonical registry URLs so the
sanitization boundary is auditable:

```text
# cache miss
npm http fetch GET 200 https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz 15ms (cache miss)
# cache hit
npm http cache npm-http-research-fixture@https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz 0ms (cache hit)
# retry
npm http fetch GET https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz attempt 1 failed with 503
# timeout
npm error network timeout at: https://registry.npmjs.org/npm-http-research-fixture/-/npm-http-research-fixture-1.0.0.tgz
```

The omitted raw npm error trailer contains a clock-derived temporary log path;
it is excluded rather than normalized in this report. No credentials, proxy
endpoints, or CA material are captured.

### Projected fixtures

Each line below corresponds to the raw line above in the same order after the
shared sanitizer runs, before any research coalescing. URLs become
`<redacted>` and the separately captured normalized hostname is
`registry.npmjs.org` for all four cases. The temporary log path is not a
network token and remains in projected diagnostic text when npm emits it; it
is absent from these representative examples.

```text
# cache miss
npm http fetch GET 200 <redacted> 15ms (cache miss)
# cache hit
npm http cache npm-http-research-fixture@<redacted> 0ms (cache hit)
# retry
npm http fetch GET <redacted> attempt 1 failed with 503
# timeout
npm error network timeout at: <redacted>
```

### Measured required scenarios

One identified clean run produced the following harness observations. Times are
seconds from process start at the binary `os.read` boundary; they can vary by
machine. Exit-observation time is when the parent detected reaping, not a
claim about the child's exact exit instant; each first read independently
confirmed that the child was still live.

| Scenario | Loglevel | First-observation time | Exit-observation time | Raw line count | Projected line count | Return code |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| cache-miss | notice | 0.254 | 0.264 | 2 | 2 | 0 |
| cache-miss | http | 0.219 | 0.264 | 6 | 6 | 0 |
| cache-hit | notice | 0.221 | 0.264 | 2 | 2 | 0 |
| cache-hit | http | 0.211 | 0.264 | 3 | 3 | 0 |
| retry | notice | 0.275 | 0.314 | 3 | 3 | 1 |
| retry | http | 0.219 | 0.314 | 9 | 9 | 1 |
| timeout | notice | 0.655 | 0.665 | 4 | 4 | 1 |
| timeout | http | 0.231 | 0.665 | 9 | 9 | 1 |

### High-volume supplementary evidence

The separate high-volume HTTP run recorded first observation at 0.218 s,
exit observation at 0.716 s, return code 1, and 32 raw/32 projected lines. It
contains 25 retry lines with distinct attempt numbers and a final HTTP status
line with a distinct duration/attempt value. Raw URLs are replaced before any
coalescing, but final projected diagnostic text remains distinct:

```text
npm http fetch GET <redacted> attempt 1 failed with 503
npm http fetch GET <redacted> attempt 2 failed with 503
```

## Aggregation

Research aggregation matches the approved Phase 7 semantics: it coalesces only
**consecutive identical final projected lines**, irrespective of whether a line
is status, retry, warning, or error, using the canonical
` (repeated N times)` suffix. It does not normalize durations, attempts,
package names, status values, URLs, or temporary paths; differing final lines
remain distinct. A small direct fixture verifies the same coalescing rule for
status, warning, and error lines.

The coalescer retains only the current pending line and occurrence count while
streaming, so its working state is constant even when total session output
grows. The high-volume run produces **32 raw lines**, **32 projected lines**,
and **32 exact-coalesced presented lines**: changing attempt numbers prevent
consecutive-identical coalescing. The bounded mailbox and omission notices are
a separate producer-admission overload mechanism, not a total session-output
cap. Exact-line coalescing is therefore insufficient for this diagnostic
stream and needs refinement at the presentation boundary.

## Decision: ACCEPT

`loglevel=http` provides timely, useful observations before process exit and
distinguishes cache hits, cache misses, retries, HTTP failures, and timeouts.
These are precisely the positive and negative progress signals needed to make
a long-running npm operation understandable to the user. Every observed
network URL is handled by the shared sanitizer, and the collection pipeline
can remain memory-bounded while continuing to drain input.

The additional output volume is expected and useful: without additional
observations there is no basis for reporting progress. Retry lines with
changing attempt numbers and other unstable fields do not collapse under
exact final-line equality, but that is a limitation of the current
presentation coalescer rather than a defect in npm's diagnostic source or a
reason to discard the observations. The coalescing mechanism must be refined
without suppressing warnings, errors, retries, timeouts, or meaningful status
transitions.

The accepted decision is to adopt `loglevel=http` in the canonical reviewed
npm policy and update the corresponding policy digest, exact invocation,
evidence identity, and assembled-output/cache identity in a subsequent
implementation phase.

## Production impact

Phase 6 changed none of the following production contracts, and specifically
left the production npm invocation unchanged:

- the production `npm ci` invocation;
- the canonical npm policy or policy digest;
- assembler evidence fields, content, or evidence identity; and
- assembled-output/cache identity.

`--loglevel=http` and the short retry/timeout values exist only in the research
harness and are not yet production policy. The accepted decision requires a
subsequent implementation phase to add `--loglevel=http` to the canonical
reviewed npm policy and update the policy digest, exact invocation, assembler
evidence identity, and assembled-output/cache identity. The short
retry/timeout values remain research-only.
