## Context

See proposal.md for motivation and the explicit prerequisite gate. `eliminate-unsafe-direct-descriptor-closes` supplies completed resource ownership plus a pending domain-neutral launch-selected policy API, stage caps/reserve, bounded draining, managed auxiliary actions and publication/precedence guarantees. This change consumes those contracts only after the prerequisite's implementation and release checks are accepted. It does not reimplement or expand the generic owner.

The prerequisite preserves current terminate-local-CLI → domain force-removal → local wait/reap behavior. This dependent change alone replaces that sequence for an attached whole `docker run` container. Preexisting immutable/cache/staging and presentation contracts remain binding; consumer signatures and persistent representations do not change.

## Goals / Non-Goals

**Goals:** represent a Docker operation before acquisition; reliably cancel it before launch; request daemon-side whole-container stop before signalling the CLI; wait separately after soft stop and force removal; retain final bounded abort-redacted output; distinguish owned local completion from confirmed container cleanup.

**Non-Goals:** generic prepare/start APIs, existing-process adoption, overriding OwnedProcess shutdown/reaping, independent main-process/pipe ownership, process groups/trees, per-PID container control, stdin/PTY, arbitrary external readers, runtime-installer migration, new persistent/user-facing configuration schemas or a fourth direct-close category.

## Decisions

### Compose the wrapper with the prerequisite owner

Use a domain object (working name DockerExecution) containing the container identity, policy and, after acquisition, one OwnedProcess. The wrapper interprets Docker evidence and projects existing consumer results/errors. The generic coordinator alone waits/reaps, controls reader barriers, releases streams, arbitrates concurrency and publishes local outcomes. Configure the policy at generic launch so deadline, context exit, setup/reader failure and explicit cancellation all select it; a wrapper-only call before explicit shutdown would miss autonomous triggers. Do not use a subclass or replace the whole shutdown method.

### Retain cancellation before acquisition

The wrapper exists before a process. Distinguish ready, launch-accepted/in-flight, running, cancelled-before-launch and shutdown-pending. Shutdown in ready records irreversible cancellation; subsequent/repeated launches must not call OwnedProcess.launch, any factory or Docker control command or acquire pipes. Prepared wrapper resources still receive independent cleanup. Cancellation is not successful execution or confirmation that a nonexistent container stopped.

Linearize launch acceptance/cancellation under one short threading.Lock protecting state, pending request and owner handoff. No launch, I/O, readiness wait, callback or completion join runs under it. If shutdown wins, suppress acquisition. If acceptance wins, the in-flight launch may still acquire a child and pending shutdown is forwarded once when its owner is available. Failed launch retains generic cleanup observation where supplied, with no retry or reconstructed owner. Repeated launch cannot acquire another main owner. An unresolved launch remains pending, not complete; bounded in-flight observation is not a terminal cleanup outcome and cannot forcibly bound arbitrary blocked factory/setup code. No OwnedProcess.prepare/start extension is needed. A try-lock that silently loses cancellation, GIL-dependent check/set, or a lock held across acquisition is rejected.

### Map Docker control onto bounded generic policy stages

The configured policy targets the entire attached container, not the local CLI PID or a PID inside the container. Each managed stop/removal command uses its own OwnedProcess and one OwnedStream per pipe, with default local policy and stage/parent-capped budgets; do not recursively configure the container policy for helpers. Nonblocking managed-command initiation/observation must not strand the sole coordinator when a Docker control child stalls. Policy commands and absence/status interpretation remain domain-local.

1. Begin T_soft before ID observation or the first docker stop attempt. It covers bounded ID waiting, helper setup/execution/observation and subsequent natural original-CLI waiting. Continue bounded drain. Daemon-side stop grace is a distinct parameter fitting this phase, not a new timer after the command.
2. If soft stop and CLI exit succeed, reap naturally and ensure final removal via bounded docker rm -f; an already-exited CLI does not need a full hard wait. Nonretryable soft error, exhausted attempts or soft expiry enters hard removal, not immediate local terminate.
3. Begin distinct T_hard before recovery discovery or the first docker rm -f attempt. It covers label discovery, bounded removal attempts, helper setup/execution/observation and then natural original-CLI waiting after force removal. A successful removal while the CLI remains alive does not itself permit early local signals.
4. Nonretryable hard-stage error, exhausted attempts or hard expiry permits generic local terminate, bounded wait, kill and bounded reap as needed, within reserved T_local. Nonretryable errors or exhausted retries advance immediately; do not sleep through unused stage time. A failed removal cannot skip local cleanup or staging cleanup.
5. Complete bounded drain, cancel supported readers, confirm actual stop barriers and release through their existing owners. A failed reader skips unusable drain work, not domain control or sibling cleanup. Keep abort/redaction active throughout cancelled-operation output; never flush ambiguous secrets as normal EOF.

### Keep separate phase timers under the existing total cap

Validate finite positive T_soft, T_hard, T_local and `T_soft + T_hard + T_local <= T_total` before main launch. T_total cannot exceed the prerequisite's characterized path-specific upper bound; the current deadline-supervisor envelope is 6 * grace_seconds, not an allowance to add another stop interval. Defaults derive from reviewed existing policy settings without changing consumer signatures or persistent data. Preserve existing assembler identity rules when reviewed execution limits change.

Phase clocks start before command initiation. Every action/wait is capped by both phase and remaining total time, including helper cleanup; no full fresh grace after commands, across retries, or from concurrent shutdown callers. Unused early time remains in the total envelope but cannot enlarge another phase's configured cap. Execution deadline starts shutdown; it does not replace phase timers. Tests must pin daemon-grace parameter conversion, exact phase ordering, retry classification and the reserved local remainder with deterministic clocks/barriers. CID waiting belongs to T_soft and label recovery to T_hard; neither receives an additional interval outside the configured caps.

### Interpret local and daemon outcomes separately

Generic completion means owned local children/readers/pipes/managed resources are safely finalized and winning outcomes published, not that remote work stopped. Keep independent confirmed container stop/removal versus unconfirmed cleanup. Daemon failure or ambiguous command evidence yields a bounded sanitized controlled domain cleanup diagnostic even with local completion. Local CLI exit alone never confirms remote termination. Characterized already-absent evidence is idempotent domain success only when creation for this execution is known to be settled and the evidence is causally subsequent to that settlement; matching identity alone is insufficient.

An active operation/domain exception remains the original CleanupFailures primary; unexpected cleanup defects can outrank ordinary operation failures, and the first recorded interruption outranks later failures/interruption objects. Preserve exact identities and secondary diagnostics. Use the prerequisite's locked pre-delivery ledger refresh, including the publication-before-phase-notification gap. No policy stage is repeated across concurrent/reentrant callers.

Initiate required lifecycle control stages inside the shared reserved budget before terminal delivery. Retain and observe already-started late helper results; no new post-delivery stop/removal requests, user-facing callbacks/output, changed delivered errors/results or retroactive remote-success projection. Incomplete resources remain owned without a global registry or GC authority. Wrapper pending acquisition observation is distinct from terminal generic shutdown delivery.

### Observe container creation through a private CID file

Launch attached docker run with --cidfile pointing to a fresh, operation-private path. Prepare its protected parent and identity before acquisition; never reuse a stale CID file or parse container stdout as an ID. Missing, empty or partially written files mean pending creation, not confirmed absence. Accept only a complete validated container ID. Clean up the ephemeral file through the domain's existing staging/resource responsibilities; it is not a new persistent representation.

On cancellation without an ID, observe the file for at most min(10 seconds, remaining soft-stage allowance, available total allowance), leaving hard recovery and local-cleanup reservations intact. Start the first retry delay at 10 milliseconds, double subsequent delays, and apply multiplicative jitter in [0.9, 1.1]. Clamp every delay to the deadline; the schedule and deadline bound the observation count. Stop on a valid ID or expiry, not on local CLI exit alone. Clocks start before observation, not after ID arrival. Inject clocks/jitter for deterministic tests; the wait must not block the sole cleanup coordinator.

The Docker CLI writes --cidfile after successful ContainerCreate and before ContainerStart. An ID therefore proves creation, not startup or cancellation. stop on a created/not-running container does not prohibit a subsequent start. Successful rm -f of that exact ID prevents subsequent start of that object; if start wins the race, force removal finalizes the running object instead. Qualify already-absent evidence by the known creation/ID, not merely a name or a local CLI exit. Verify these assumptions on supported Docker versions using host evidence, not just current upstream source.

### Bound retries and recover lost identity by labels

Each logical stop/removal stage is entered once and shared by concurrent callers, but its commands may have up to three total attempts per command/exact target ID, including the initial attempt. Retry only classified transient subprocess-launch or daemon-communication failures, including an ambiguous lost response; do not retry interruption, unexpected defects, permanent errors or phase expiry. Use the same 10 ms doubling/±10% jitter schedule, under the original phase/total deadlines. Attempts are sequential: do not launch another while the preceding helper is still running or its local cleanup is unresolved. Each acquired helper has its own default-policy owner; pending cleanup retains ownership rather than authorizing a duplicate. Share counters across normal and recovery paths; duplicate callers never grant new attempts. Exact-ID stop/removal is operationally idempotent, but stop-before-start still requires final removal.

Label every main container with io.docker-constructor.managed=true, io.docker-constructor.controller-pid=<current process PID>, and io.docker-constructor.operation-id=<fresh unique operation token>. PID is metadata, not sufficient authority: the token prevents PID reuse and cross-operation deletion. Labels are not an authorization boundary against a hostile daemon peer. Main acquisition is never retried.

If ID observation expires without trustworthy identity, enter hard recovery and list all containers, including stopped/created ones, with docker ps -a --no-trunc and all three exact label filters. Remove only validated full IDs returned by that scoped query, never broadly by PID, name, image or the managed label alone. Discovery and per-ID removal attempts use the remaining hard/total allowance and existing local reserve; discovery also has at most three attempts for classified transient failures. A known ID whose removal response was lost is retried by exact ID; recovery never resets that target's counter. Discovery failure, exhausted removal attempts, insufficient time or an empty list while creation may still be pending leave remote cleanup unconfirmed. A successful sweep does not prove that an unresolved create request cannot produce a later container.

No new control/discovery attempt may begin after terminal delivery. Late identity, creation or helper evidence is observation-only. Preserve bounded sanitized unconfirmed diagnostics, authoritative operation/interruption identity and staging cleanup. This bounded recovery improves cleanup but does not guarantee orphan prevention when creation or daemon communication remains unresolved.

Use barriers, a fake clock and injected jitter to cover delayed/partial/missing CID, cancellation between Create and Start, retryable and permanent failures, lost responses, scoped discovery, PID reuse and concurrent callers. Allow creation during recovery and after delivery; require confirmed exact-ID removal where provable, otherwise unconfirmed cleanup, without extra grace, counter resets or late requests. Release all test-controlled workers.

### Preserve compatibility while changing only approved shutdown behavior

Streaming adapters retain signatures, normal output, decoding, identities, capture/tail limits, URL-display policy, safe SDK projection, staging/removal duties and total bounds. Cancelled tails may include additional bounded final output, using unchanged abort-redaction rules. Captured helper clients continue using the prerequisite's default local ownership adapters and established list/LifecycleOutcome shapes. Sanitization happens in adapters; generic raw notes are never dumped directly to public channels.

Alternatives rejected: overriding generic shutdown; raw main process/pipe fallbacks; external-reader ownership; terminating the CLI before requesting container stop; treating CLI exit as daemon evidence; unrestricted blocking policy callbacks; independent full per-command grace budgets; a wrapper lock held across launch.

## Risks / Trade-offs

- **[Risk] Pending cancellation is lost or an in-flight launch is reported complete.** → Short atomic gate, retained handoff obligation and barrier-controlled failed/blocked-launch tests.
- **[Risk] Domain policy recursively stops its helper clients or duplicates cleanup.** → Default local policy per helper, explicit owner ledger, once-only logical stages and shared bounded attempt counters.
- **[Risk] Soft expiry skips hard waiting or command execution is unbounded before timing starts.** → Distinct stage clocks starting before command initiation, reserve feasibility checks and deterministic transition tests.
- **[Risk] Final output/backpressure delays cleanup or reveals sensitive suffixes.** → Prerequisite bounded drain independent of callback progress, unchanged selected tails and abort finalization, real-pipe tests.
- **[Risk] Early absent results precede delayed container creation.** → Bounded CID observation, scoped label recovery, causal evidence qualification and barrier-controlled delayed-creation tests.
- **[Risk] Daemon outage is hidden by clean local CLI exit.** → Separate outcomes and sanitized controlled unconfirmed diagnostics with staging cleanup preserved.
- **[Risk] A late result changes delivered authority or repeats requests.** → Reuse prerequisite precedence/publication tests across policy stages and retain late outcomes as observation only.

## Migration Plan

1. Verify the prerequisite API, adapters and full release gates; do not start production work from its partial baseline.
2. Characterize the unchanged consumer surface and old cleanup behavior separately from new RED wrapper/cancellation/control contracts.
3. Implement the composition/cancellation gate, then the Docker staged policy and separately owned helper clients, with their tests/docs in the corresponding groups.
4. Integrate streaming/domain outcome projection and replay generic primary/concurrency/publication regressions plus real-subprocess command fixtures.
5. Run dependency/ownership/import/example proofs and full project checks, retaining complete logs/statuses in this change's verification evidence.
6. Before archiving this dependent delta, sync/archive prerequisite specs first. Archive is a follow-up workflow, not a checked implementation task.

Rollback restores prerequisite adapter behavior before removing the Docker wrapper/policy. Keep generic ownership, API and proof gates intact. No persistent migration or runtime image-packaging change is required.
