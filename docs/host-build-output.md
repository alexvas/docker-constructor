# Host build output

Host-side materialization reports presentation-neutral activity before Docker's native build output begins. Configure presentation only in the untracked local companion, `docker-constructor.local.toml`:

```toml
[output]
host_heartbeat = "interactive"
show_network_hosts = false
```

`host_heartbeat` accepts `interactive`, `lines`, or `off` and defaults to `interactive`. Interactive mode uses a replaceable status on a text TTY. Set `lines` explicitly to receive durable, newline-terminated status output in noninteractive text execution. `off` hides heartbeat presentation but does not disable activity collection, actionable diagnostics, terminal states, or bounded failure context. JSON output never creates a live presentation worker. There are no command-line or environment aliases for these local settings.

`show_network_hosts` defaults to `false`. Enabling it may show normalized hostnames, but never a scheme, port, user information, path, query, fragment, proxy detail, credential, or exception message. Sanitization and bounded failure capture do not depend on this display setting.

The first heartbeat is produced after 3 seconds. Interactive status can refresh once per second; `lines` emits ordinary status every 30 seconds. A byte count means only that the existing host transport exposed and yielded those bytes. It does not imply a known total, percentage, rate, or current network activity. Steps without stdout/stderr, such as host downloads, do not report diagnostic silence.

For `npm ci`, diagnostic silence means only that no stdout or stderr was observed for 120 seconds. It is distinct from the fixed execution timeout and is not proof that npm, a process, or the network is inactive. Last activity describes the latest observed diagnostic or transport-progress activity and its whole monotonic age; it makes no claim about unobserved work. npm output is never used to infer current download or network activity.

`Ctrl-C`, timeout, and ordinary failure retain the existing subprocess and cleanup behavior. A nonempty bounded sanitized tail is included once under a retained-context label and may repeat output that was already shown live. Healthy presentation coalesces exact repeats on a best-effort basis; interactive mode may replace one conservative unsigned numeric-token variant in its mutable slot, while `lines` keeps changed values as separate durable lines. Saturation or renderer failure can make live output incomplete without changing retained context or the primary result.

Presentation shutdown has one shared five-second completion budget for drain, completion acknowledgement, and worker join. If terminal output blocks beyond that budget, execution can return while a daemon presentation thread and its already-started write remain in flight. The final output may therefore be unavailable on that degraded path. Cancellation prevents subsequent renderer operations after the blocked write returns, and Constructor does not synchronously retry output to the failed stream.

The reviewed npm 11.16.0 research accepted `--loglevel=http`. The canonical locked-assembly invocation and policy identity include that setting so cache reuse cannot cross the prior logging policy. HTTP diagnostics still pass through secret redaction, URL sanitization, bounded collection, and presentation filtering.
