[ Русский ](README.md) | **English** | [ 中文 ](README.zh.md)

# Pi Docker runtime

An isolated Docker environment for Pi and development tools. Selected non-Debian inputs live in `docker-constructor.toml`; do not duplicate their versions in README files, `.env`, Dockerfile, or Compose.

## Supported host environments and requirements

- Linux, or Windows through WSL2
- Docker Engine 24+ with BuildKit; on Windows, enable Docker Desktop integration for the WSL2 distribution (or run Docker Engine inside WSL2)
- Python 3 installed in the Linux/WSL2 environment

Native Windows Python and direct execution from PowerShell or Command Prompt are not supported. On Windows, clone the repository and keep constructor caches in the WSL2 Linux filesystem (for example under `~/src` and `~/.cache`), not under `/mnt/c`; the project relies on Linux ownership, permission, locking, symlink, and atomic-filesystem semantics. Run all constructor commands from the WSL2 shell using Linux paths.

## 1. Build the environment

Validate the reviewed inventory, then build the image:

```bash
./docker/docker-constructor.py validate
./docker/docker-constructor.py build -y
```

Normal text builds show native Docker/BuildKit progress as it is produced. Use `--output json` when a single machine-readable result is required.

The build needs no project path, `.env`, or host-gateway reachability. Gateway diagnostics are only for runtime host connectivity. For rootless Docker, use `doctor` separately when that connectivity needs troubleshooting or repair:

```bash
./docker/docker-constructor.py doctor
./docker/docker-constructor.py doctor --apply-rootless-override -y
```

A deliberate Python override uses:

```bash
./docker/docker-constructor.py build --override build.stages.toolchain.python.version=X.Y.Z
```

Override constraints accept only `==, >, >=, <, <=` with complete `X.Y.Z` versions. Wildcards, incomplete versions, OR expressions, and prerelease values are rejected unless policy explicitly allows them. The inventory pins reviewed non-Debian inputs, but Debian repositories and BuildKit metadata mean byte-identical OCI output is not guaranteed.

## Host access (optional)

Ordinary builds and runs do not need host connectivity. Host access is **disabled by default**: omit `[runtime.host-access]` and do not create a local companion unless you want a custom cache directory.

To let a container reach a host service, enable one of the two reviewed policies in `docker-constructor.toml`:

```toml
[runtime.host-access]
enabled = true
mode = "docker-gateway" # or "external-address"
# proxy-port = 1080      # optional; integer 1–65535
```

Both modes make the configured address available as `host.docker.internal` and `HOST_ACCESS_ADDRESS`. After configuration, run normally; no special `run` option is needed.

### docker-gateway: let doctor select the Docker gateway

Use this mode when Docker's gateway is the correct route to the host. Run doctor once to diagnose the gateway and save its selected concrete address:

```bash
./docker/docker-constructor.py doctor
```

Doctor writes `[host-access].address` to `docker-constructor.local.toml`, beside `docker-constructor.toml`. If the address later becomes stale, run doctor again. A missing address makes `run` fail with instructions to run doctor; ordinary `run` never probes Docker or changes local state. Doctor performs gateway diagnosis, persistence, and repair only in `docker-gateway` mode: it does not diagnose, overwrite, persist, or repair state for `external-address` or disabled host access.

### external-address: provide an address yourself

Use this mode when the host service is reachable through a known host-interface IP. Set that IP yourself in the local companion; doctor does not discover, replace, persist, or repair external-address state:

```toml
# docker-constructor.toml
[runtime.host-access]
enabled = true
mode = "external-address"

# docker-constructor.local.toml
[host-access]
address = "192.0.2.10"
```

`address` must be an IP address in this mode; `host-gateway` is not accepted. A service reached through `HOST_ACCESS_ADDRESS` must listen on an interface reachable from that address. A loopback-only service can remain unreachable, and firewall rules still apply.

### Constructor project and local companion

Every command selects one constructor project: the current working directory by default, or `--project-directory DIR`. Its fixed layout is `docker-constructor.toml`, optional `docker-constructor.local.toml`, `Dockerfile`, optional `.env`, and `.docker-local/` directly beneath that directory. The local companion contains machine-specific state only and cannot override reviewed policy, dependencies, or `cache.ttl`.

### Optional proxy port and environment variables

Set `proxy-port` only when applications need to know a host-side port. The constructor then sets `HOST_PROXY_PORT=<port>`; both modes set `HOST_ACCESS_ADDRESS=<address>`. These are neutral address/port variables: the constructor does not select a proxy protocol, construct a proxy URL, or set `PI_PROXY_URL`, `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`.

### Cache settings

Keep portable cache policy in the reviewed inventory and machine-specific paths in the local companion:

```toml
# docker-constructor.toml
[cache]
ttl = 3600

# docker-constructor.local.toml
[cache]
dir = "/home/dev/.cache/pi-docker"
```

`cache.ttl` belongs in reviewed `docker-constructor.toml`; `cache.dir` belongs only in `docker-constructor.local.toml`. `--no-cache` bypasses HTTP caching for one update check without changing the reviewed TTL.

Without `[cache].dir`, the persistent root is `${XDG_CACHE_HOME}/docker-constructor` when `XDG_CACHE_HOME` is non-empty and absolute, otherwise `~/.cache/docker-constructor`. HTTP responses use `versioning/`; verified artifacts, locks, and temporary state use `runtime-artifacts/blobs`, `runtime-artifacts/locks`, and `runtime-artifacts/tmp`. Runtime projections and default evidence are stored in the external project-state namespace keyed by the selected constructor project's canonical path; no implicit `.docker-generated` directory is created in the constructor project or a workspace.

A local `cache.dir` must be an absolute, dedicated constructor-owned root. Do not select `/`, the home directory, `XDG_CACHE_HOME` itself, or its ancestor. Constructor-owned directories are secured to `0700`, HTTP entries to `0600`, and verified blobs to `0444`; existing parent directories, including `XDG_CACHE_HOME`, are not chmodded. If a selected cache path has foreign ownership or cannot be secured, restore its ownership or remove that stale constructor subtree and retry. Host access does not need to be enabled.

### Corporate trust and application proxy

Corporate network settings are machine-local: add `[corporate-trust]` and `[network.proxy]` only to the resolved `docker-constructor.local.toml` companion, and place trust material at the fixed repository path `.docker-local/corporate-ca-bundle.crt`. For example:

```toml
[corporate-trust]
enabled = true

[network.proxy]
url = "http://proxy.corp.example:3128"
no_proxy = "localhost,.corp.example"
```

The certificate file is a **complete replacement** for the system `/etc/ssl/certs/ca-certificates.crt`, not an extra certificate. It must therefore contain every public and corporate root required by container clients; the constructor checks PEM framing and Base64 only, while certificate validity and trust coverage remain the operator's responsibility.


When corporate trust is enabled, every networked build command and every in-scope constructor-launched runtime container receives this fixed client CA mapping:

- `NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt`
- `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt`
- `REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`
- `PIP_CERT=/etc/ssl/certs/ca-certificates.crt`
- `CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt`

This closed enabled-only policy supports enterprise TLS interception. The variables are not configurable and are not copied from the invoking host. When corporate trust is absent or disabled, the constructor emits none of them, so inherited image values are preserved. The assignments are operation- or launch-scoped and never become persistent image `ENV` metadata. Consequently, a direct image launch that bypasses the constructor is outside this runtime policy.

The system bundle is replaced by the configured complete bundle, whereas `NODE_EXTRA_CA_CERTS` augments Node's built-in trust roots and does not replace them. All constructor-launched runtime containers are in scope, including every standalone npm assembler container. Runtime CA-policy verification remains required and is separate from the two diagnostic launch exceptions. The only launch exceptions are internal build-verification containers from `docker/versioning/verification.py` and gateway-probe containers from `docker/networking.py`; those paths receive neither the corporate bundle mount nor these assignments, and preserve inherited image trust settings regardless of whether corporate trust is enabled.

The proxy URL requires an explicit host and port and supports `http`, `socks5`, or `socks5h`. `no_proxy` is optional and creates `NO_PROXY`/`no_proxy` only when explicitly configured. Proxy credentials or URI userinfo are not allowed; use a credential-free proxy endpoint. SOCKS support during a build is best-effort and a build client that does not support `socks5` or `socks5h` may fail normally.

After enabling or changing the certificate bundle, rebuild the image for build-stage trust. A restart or new launch mounts the current bundle read-only and receives certificate updates without a rebuild; this is not live reload for an already-running container or process.

This feature does not configure or control the Docker client or daemon proxy/trust, registry authentication or registry trust, image pulls, or `FROM` resolution. Configure those operator-managed host and Docker facilities separately when they are required.

## 2. Launch the environment

Open the interactive workspace selector:

```bash
./docker/docker-constructor.py run --tui
```

The selected primary workspace becomes the container working directory and is bind-mounted at the same absolute path. Extra workspaces are mounted 1:1 with consecutive `WORKSPACE_PATH_2`, `WORKSPACE_PATH_3`, … numbering — no fixed limit. Host `~/.pi` is mounted at `/home/dev/.pi`. Set optional `WORKSPACE_ROOT` in the selected constructor project's `.env` or pass `--workspace-root` to choose the TUI tree root.

Direct launch with explicit workspaces:

```bash
./docker/docker-constructor.py run --workspace /path/to/primary --extra-workspace /path/to/extra
```

### Runtime extension artifacts

Before `run` starts Docker, the host selects the reviewed runtime extensions and materializes each selected tarball in its private content-addressed cache. A first launch for an uncached selection may use the network to fetch only its reviewed artifacts. Later launches reuse verified cache hits and need no extension-artifact network access, so they can start offline.

The container receives neither artifact URLs nor the cache directory. It receives only the narrow runtime projection and one read-only file mount per selected verified artifact beneath `/run/pi-cli/runtime-artifacts`; unselected cache content is never mounted. If a cache miss cannot be downloaded, verified, or published, `run` fails before Docker starts. There is no public prefetch command; run preparation owns cache materialization.

## 3. Update environment components

### Update Pi after a release

1. Inspect only Pi and request a reviewable suggestion:

   ```bash
   ./docker/docker-constructor.py check-updates --only build.stages.pi-tools.pi --suggest
   ```

2. `--suggest` is **non-mutating**: its output is a complete manual replacement block, never an automatic edit or TOML to append. In the canonical `docker-constructor.toml`, locate the matching `# --- pi-tools.pi ---` header and replace that entire block through the next `# --- ... ---` header. The fragment retains unchanged source, update-policy, override, validation, and configured platform-artifact fields; review them together with the candidate values. Headers are visual-only; use the full TOML table path when locating a reviewed entry.
3. Validate and review the exact repository change:

   ```bash
   ./docker/docker-constructor.py validate
   git diff -- docker-constructor.toml
   ```

4. Rebuild and verify the runtime image:

   ```bash
   ./docker/docker-constructor.py build
   ./docker/docker-constructor.py verify
   ```

### Managed component lifecycle

| Category | Representative components | Installation location / owner | Update source |
|---|---|---|---|
| Base image | Node base | Image-owned OCI layers | Docker registry metadata in `docker-constructor.toml` |
| Toolchain | Rust, uv, Python, ty | Builder/image-owned paths | Rust channel, GitHub, uv, PyPI providers |
| Node CLIs | Pi, OpenSpec | Image-owned global tools | npm provider |
| Prebuilt binaries | rtk, fd | Image-owned runtime binaries | GitHub release artifacts and checksums |
| Shell runtime | Oh My Zsh | Image-owned `/home/dev` content | Git revision provider |
| Pi extensions | pi-read, usage, proxy, rtk registration | Host-mounted `/home/dev/.pi` | `runtime.pi-extensions` npm metadata |
| Debian packages | OS utilities and libraries | Image-owned system paths | APT; outside `docker-constructor.toml` update discovery |

For another managed component, locate its inventory path, run `check-updates --only <path> --suggest`, and replace the complete suggested block rather than appending leaf values. Review retained fields and every configured platform artifact, then validate, inspect the diff, rebuild, and verify. If the path is under `runtime.pi-extensions`, rebuilding updates the effective image inventory but not mounted state; refresh it under Maintenance.

### Update-check controls

The default text report is for review: `TARGET`, `PROVIDER`, `CURR -> NEXT`, `STATUS`, and `PUBLISHED`. It removes only a leading `build.stages.` from targets, shows publication dates as `YYYY-MM-DD`, and places provider explanations in a `Details:` section. During interactive text discovery, stderr temporarily shows `Checking updates [N/T] TARGET (PROVIDER)…`; redirected output has no progress bytes.

Use `--details` for the full diagnostic table with complete paths, separate current/candidate values, applicability, reasons, and publication times as `YYYY-MM-DD HH:MM:SS GMT`. Use `--json` for automation: it is the machine-readable interface and is unchanged by `--details`.

- **Interactive review:** `--only <provider-or-path>` narrows discovery; `--suggest` adds non-mutating, complete manual-replacement TOML blocks.
- **Automation and policy:** `--json` emits machine-readable output; `--strict` fails on provider errors; `--fail-on-outdated` fails when an update exists.
- **Advanced discovery/cache:** `--include-prerelease` includes prerelease results; reviewed `[cache].ttl` controls update-discovery HTTP caching, and `--no-cache` bypasses it for one invocation.

Ordinary builds, validation, launch, and extension setup never perform update discovery.

## Maintenance

### Validate Dockerfile changes

Run the dependency-free static contracts and the containerized linter independently:

```bash
python -m unittest discover -s tests -p 'test_*.py'
scripts/check-types
scripts/check-dockerfile
```

The unittest suite uses only the Python standard library and does not invoke Docker. `scripts/check-types` checks maintained production modules under `docker/` with Python 3.14 semantics from `pyproject.toml`; run it in the project image, where `ty` and the sole intended Python installation are available. The Hadolint gate is a separate command that requires Docker and runs the immutably pinned official image; Hadolint is not installed through Python.

### Verify the image

```bash
./docker/docker-constructor.py verify
```

### Refresh mounted Pi extensions

After changing `runtime.pi-extensions`, launch with the intended Pi home mounted — the entrypoint will automatically run the idempotent installer via `docker.runtime_installer`:

```bash
./docker/docker-constructor.py run --workspace /path/to/primary
```

### Repair host ownership and permissions

With rootless Docker, `EACCES` can occur in two situations:

1. The agent inside the container cannot modify a file created by the host user.
2. The host user cannot modify or read a file created inside the container.

To grant both sides the required access, assign the target directory to the `docker-dev` user and group, then allow the owner and group to read and write:

```bash
sudo chown -R <docker-dev>:<docker-dev> /path/you-intend-to-own
chmod -R ug+rwX /path/you-intend-to-own
```

In a rootless Docker configuration, the `docker-dev` user and group typically correspond to UID/GID `100999`. The host user must also be added to the `docker-dev` group:

```bash
sudo usermod -aG <docker-dev> "$USER"
```

Confirm the actual UID/GID before running these commands, and limit recursive permission changes to the directory you intend to own.

### Clean Docker storage and caches

Inspect usage, then remove only disposable build cache:

```bash
docker system df -v
docker builder prune
```

Use `docker builder prune -af` or `docker image prune -a` only when a full cache/image reset is intended. Avoid `--volumes` unless all stored data is known to be disposable.

## Troubleshooting

- **EACCES on a mounted project or Pi home:** compare host ownership with the configured runtime UID/GID. Either let `CHOWN_WORK_ON_START=1` repair actual mount points or disable it and use the narrowly scoped Maintenance procedure above.
- **Update provider unavailable:** retry later or inspect cached results; use `--strict` only when provider availability must be enforced.
