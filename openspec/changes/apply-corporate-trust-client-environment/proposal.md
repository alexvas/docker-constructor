## Why

The configured replacement CA bundle is available during image builds and mounted into launched containers, but several common Node, Python, pip, Requests, and curl clients do not reliably discover that bundle without client-specific environment variables. Applying one conditional CA environment policy at build and runtime will make enterprise TLS interception work consistently without changing images or containers when corporate trust is disabled.

## What Changes

- When corporate trust is enabled, export `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT`, and `CURL_CA_BUNDLE` to `/etc/ssl/certs/ca-certificates.crt` for every networked Dockerfile build command.
- When corporate trust is enabled, pass the same five variables through direct Docker environment arguments to each launched runtime container, including standalone npm assemblers, except internal build-verification containers (`docker/versioning/verification.py`) and gateway-probe containers (`docker/networking.py`).
- Leave the two excluded internal launch paths without constructor-injected corporate bundle mounts or client CA assignments, preserving inherited image trust settings; runtime CA-policy verification remains in scope.
- Keep these variables out of persistent image `ENV` metadata and out of user-controlled configuration.
- Preserve inherited client trust settings exactly when corporate trust is absent or disabled.
- Document that the goal is compatibility with enterprise TLS interception; `NODE_EXTRA_CA_CERTS` augments Node's built-in roots and does not promise strict replacement of Node's trust store.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `corporate-network-configuration`: Extend enabled corporate trust to apply a fixed, conditional client CA environment consistently to build-stage network operations and all constructor-launched runtime containers except the internal build-verification and gateway-probe diagnostic paths; those two paths receive neither corporate bundle mounts nor client CA assignments.

## Impact

- Affects the Dockerfile corporate-network helper and all networked build-stage commands that source it.
- Affects Docker run-vector rendering and corporate-network launch verification.
- Requires focused contract, orchestration, disabled-mode, Dockerfile, runtime, and documentation tests.
- Does not change local configuration syntax, proxy behavior, Docker daemon/client trust, registry pulls, image metadata, or direct image launches that bypass the constructor.
