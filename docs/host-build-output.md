# Host build output

Host-side materialization reports presentation-neutral activity before Docker's native build output begins. Configure presentation only in the untracked local companion, `docker-constructor.local.toml`:

```toml
[output]
host_heartbeat = "interactive"
network_url_display = "redacted"
```

`host_heartbeat` accepts `interactive`, `lines`, or `off` and defaults to `interactive`. Interactive mode uses a replaceable status on a text TTY. Set `lines` explicitly to receive durable, newline-terminated status output in noninteractive text execution. `off` hides heartbeat presentation but does not disable activity collection, actionable diagnostics, terminal states, or bounded failure context. JSON output never creates a live presentation worker. There are no command-line or environment aliases for these local settings.

## Network URL display policy

`network_url_display` is a closed setting with exactly three values:

- `redacted` omits source URL details. `network_url_display` defaults to `redacted` when the key or `[output]` table is absent.
- `host-path` may show a normalized hostname and safe canonical path. It omits scheme, user information, explicit port, query, fragment, proxy details, credentials, and exception messages. Unsafe paths use `<redacted-path>`. Package names and other sensitive path information can still be disclosed, so select this mode deliberately.
- `exact` preserves bounded third-party diagnostic content as closely as terminal safety permits. It can disclose URL credentials, proxy details, query values, fragments, and caller-provided secrets. It is not secret-safe.

Application-content redaction is therefore a user-selected presentation and noise-control facility, not a security boundary. Operators are responsible for controlling third-party output and sensitive input values, especially when selecting `host-path` or `exact`.

In `redacted`, consecutive recognized successful `npm http fetch` diagnostics can aggregate without URL or latency. In `host-path`, only latency variants for the same safe hostname/path and request identity aggregate. In `exact`, diagnostics are not aggregated or numerically replaced.

## Propagation and isolation

The facade resolves one typed `NetworkUrlDisplay` value before host materialization. That enum propagates through host materialization, Pi assembly, and the assembler execution request into immutable stream-collector configuration; it is never converted to a string or boolean in that request path. Direct assembler, SDK, and injected callers that omit the selector default to `redacted`, and invalid values are rejected before effects.

This selector is presentation-only. It does not enter assembler identity, assembler-input identity, assembled-output identity, cache hashing or equality, verification or assembler evidence, publication decisions, npm or Docker arguments, network behavior, lifecycle events, cleanup, or primary results. External SDK diagnostics remain transient, secret-redacted, URL-free, and path-free under every local mode.

## Output channels and retention

The selected policy applies consistently to interactive text, explicit noninteractive `lines`, text failure context, and JSON `host_failure.tail`. JSON output has no live sink and stdout remains one JSON document. Noninteractive text has live output only when `lines` is explicitly selected.

Collection maintains exactly one bounded retained tail per stream in the selected local representation. It does not retain redacted, host-path, and exact copies in parallel. A nonempty failure tail is emitted once under a retained-context label warning that it may repeat live output; an empty tail creates no diagnostic section. Simultaneous external SDK events are produced transiently and do not create another retained tail.

## Mandatory safety and bounds

Terminal-control neutralization is mandatory in all three modes, including `exact`: CSI, OSC, DCS, CR, backspace, NUL, and other terminal-affecting controls are rendered inert. Ordinary tabs and Unicode remain available. Incremental control-sequence state is bounded at 8 KiB, ambiguous sanitizer tokens at 8 KiB, and each unterminated source diagnostic at 64 KiB. Oversized and incomplete content fails closed with fixed visible markers, input continues draining, and processing recovers at a safe boundary. These safety rules and retained-tail byte bounds cannot be disabled by presentation policy.

The first heartbeat is produced after 3 seconds. Interactive status can refresh once per second; `lines` emits ordinary status every 30 seconds. A byte count means only that the existing host transport exposed and yielded those bytes. It does not imply a known total, percentage, rate, or current network activity. Steps without stdout/stderr, such as host downloads, do not report diagnostic silence.

For `npm ci`, diagnostic silence means only that no stdout or stderr was observed for 120 seconds. It is distinct from the fixed execution timeout and is not proof that npm, a process, or the network is inactive. Last activity describes the latest observed diagnostic or transport-progress activity and its whole monotonic age; it makes no claim about unobserved work. npm output is never used to infer current download or network activity.

`Ctrl-C`, timeout, and ordinary failure retain the existing subprocess and cleanup behavior. Healthy presentation coalesces exact repeats on a best-effort basis; interactive mode may replace one conservative unsigned numeric-token variant in its mutable slot, while `lines` keeps changed values as separate durable lines. Saturation or renderer failure can make live output incomplete without changing retained context or the primary result.

Presentation shutdown has one shared five-second completion budget for drain, completion acknowledgement, and worker join. If terminal output blocks beyond that budget, execution can return while a daemon presentation thread and its already-started write remain in flight. The final output may therefore be unavailable on that degraded path. Cancellation prevents subsequent renderer operations after the blocked write returns, and Constructor does not synchronously retry output to the failed stream.

The reviewed npm 11.16.0 research accepted `--loglevel=http`. The canonical locked-assembly invocation and policy identity include that setting so cache reuse cannot cross the prior logging policy. HTTP diagnostics still pass through the selected transformation, terminal-control neutralization, bounded collection, and channel isolation described above.
