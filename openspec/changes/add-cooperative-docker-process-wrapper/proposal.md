## Why

Stopping the local attached Docker CLI does not prove that its daemon-side container stopped, and immediate local termination can discard final output. Docker execution needs a domain wrapper with reliable prelaunch cancellation and cooperative whole-container stop/removal, while local resource safety remains the generic owner's responsibility.

## Dependencies

This change depends on **`eliminate-unsafe-direct-descriptor-closes`**. While it is active, read its proposal/design/delta specs by that change id; after completion, use its archived evidence and the synced [generic ownership/API contract](../../specs/durable-filesystem-transactions/spec.md). Implement it only after that change's generic stop-policy API, consumer adapters, convergence and release checks are complete and accepted. Before archiving this dependent delta, the prerequisite specifications must have been synced/archived into main specs. The prerequisite does not depend on this change. These are explicit implementation/archive gates, not a claim that CLI artifact readiness enforces inter-change dependencies.

## What Changes

- Compose a Docker operation wrapper, whole-container stop/removal policy and one acquired `OwnedProcess`, without subclassing or replacing the generic shutdown coordinator.
- Retain irreversible cancellation before launch; linearize acceptance/cancellation under a short state lock, suppress acquisition if cancellation wins, and retain pending requests through in-flight owner handoff or failed-launch observation. Do not add generic `prepare()/start()` APIs.
- Replace the existing terminate-CLI-first shutdown order with `docker stop` plus soft-stage waiting, then `docker rm -f` plus distinct hard-stage CLI waiting, followed by local terminate/kill/reap only after hard failure/expiry. Successful soft completion still receives bounded final removal without unnecessary hard waiting.
- Configure finite positive `T_soft`, `T_hard` and reserved `T_local` within the existing total bound. Each stage includes command execution and subsequent original CLI waiting; nonretryable errors or exhausted attempts advance immediately, and repeated requests/helpers never restart budgets.
- Run stop/removal helper clients through separate generic owners using default local policies and stage/parent-capped budgets. Preserve bounded final output, decoding, selected tail limits, callback isolation and abort redaction while draining.
- Track confirmed container stop/removal separately from local cleanup. Daemon unavailability or ambiguous control results produce controlled sanitized diagnostics through existing result/error interfaces, not success inferred from CLI exit. Absence reported before pending daemon-side creation settles does not confirm cleanup; Use a private --cidfile, observing pending identity for at most 10 seconds inside existing budgets with 10 ms doubling delays and ±10% jitter. Permit up to three sequential command attempts for classified transient launch/daemon errors, sharing counters and deadlines. On lost identity, discover all containers by exact managed/controller-PID/unique-operation labels and remove their full IDs within the reserved recovery budget. Insufficient causal evidence still yields unconfirmed reporting; no additional shutdown time is granted.
- Preserve first-interruption and active-operation authority, locked undelivered publication, at-most-once control stages and cleanup, retained incomplete resources and observation-only late outcomes. Keep staging cleanup and prior immutable/cache content guarantees.
- Add deterministic prelaunch/handoff, cancellation during CID observation/Create/Start and label recovery, stage-clock and publication-gap tests, real-subprocess Docker-command fixtures, dependency proofs and executable documentation. Provide a separately runnable host evidence collector for supported Docker versions; it is not wrapper implementation or evidence of its acceptance. This change introduces no user-facing signature or persistent representation changes; only the listed cancellation/cleanup behavior changes are authorized.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `locked-npm-environment-assembly`: Add wrapper cancellation and whole-container cooperative stop/removal with distinct stage timeouts, bounded final drain and separate local/container outcomes; amend the prerequisite's pipe-ownership compatibility requirement and existing timeout/failed-execution cleanup requirements without weakening their other guarantees.

## Impact

Affected code is the Docker execution wrapper/policy in the npm domain and its streaming/captured adapter seams, tests and docs. The generic implementation and public policy API are supplied by the prerequisite, not implemented or expanded again here. Dependency direction remains Docker consumer/policy → `docker.process` → explicit filesystem owners. Each main/control pipe has one `OwnedStream`; no raw main-process control, duplicate readers/reaping, process-tree/group or per-PID container control, stdin/PTY, runtime-installer migration, global ownership registry or fourth direct-close category is introduced.

The generic stage-cap/reserve/drain/managed-action mechanisms remain in the prerequisite. All Docker commands, identities, wrapper states, Docker soft/hard mapping, daemon evidence, unconfirmed-container diagnostics and domain acceptance tests are owned by this change. Prior generic verification is prerequisite evidence, not proof of this Docker behavior.
