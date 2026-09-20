"""Fixed npm policy and the consumer-neutral assembler script.

The assembler runs one canonical shell script inside the reviewed Node image.
The script first asserts the container's actual Node and npm versions equal
the caller-reviewed values, then synthesizes a project manifest in sync with
the mounted lockfile, and finally runs exactly
``npm ci --ignore-scripts --no-bin-links --no-audit --no-fund
--loglevel=http``.  Lifecycle scripts never run, ``engine-strict`` stays
disabled, no reviewed-root ``bin`` metadata creates an executable link, and
``--loglevel=http`` is the accepted Phase 6 decision that makes timely
cache-hit, cache-miss, retry, timeout, and HTTP-status observations available
on the existing stdout/stderr pipes.

The script bytes and the npm policy flags are canonical constants so callers
derive ``script_digest``/``policy_digest`` for :class:`AssemblerIdentity`
from these exact bytes rather than from any ambient or consumer input.
"""

from __future__ import annotations

import hashlib
import json

#: The one fixed npm invocation policy.  ``--ignore-scripts`` disables all
#: lifecycle scripts, ``--no-bin-links`` prevents executable-link creation,
#: ``--no-audit``/``--no-fund`` disable network advisory/funding probes, and
#: ``--loglevel=http`` is the accepted Phase 6 logging decision that exposes
#: cache-hit, cache-miss, retry, timeout, and HTTP-status observations on the
#: existing stdout/stderr pipes.  Adding it changes the policy and script
#: digests, so outputs assembled under the prior policy identity are never
#: reused.
NPM_CI_FLAGS = (
    "--ignore-scripts",
    "--no-bin-links",
    "--no-audit",
    "--no-fund",
    "--loglevel=http",
)

NPM_CI_COMMAND = ("npm", "ci") + NPM_CI_FLAGS

# ── Reviewed finite npm network and execution limits ───────────────────
#
# These are fixed reviewed policy constants, never user-configurable and
# never read from the ambient environment.  They bound npm's own request
# and retry behaviour (rendered into the assembler container environment)
# and the reviewed total Docker-backed assembly duration reserved for
# enforcement by the execution boundary in Phase 3.  All npm timeouts are
# milliseconds; the total duration is whole seconds.

#: Maximum duration of one npm registry request (5 minutes).
NPM_REQUEST_TIMEOUT_MS = 300_000

#: Number of additional fetches after the first attempt (3 retries).
NPM_RETRY_COUNT = 3

#: Minimum backoff delay before a fetch retry (10 seconds).
NPM_RETRY_MIN_TIMEOUT_MS = 10_000

#: Maximum backoff delay before a fetch retry (60 seconds).
NPM_RETRY_MAX_TIMEOUT_MS = 60_000

#: Reviewed total assembly duration identity-bound in Phase 1 and enforced
#: by the constructor-owned execution deadline in Phase 3.
ASSEMBLY_TOTAL_TIMEOUT_SECONDS = 1_800

#: Structured assembler-script exit codes.
EXIT_NODE_VERSION_MISMATCH = 65
EXIT_NPM_VERSION_MISMATCH = 66

#: Root dependency-declaration maps the assembler copies into the
#: synthesized project manifest.  These are exactly the dependency classes
#: lockfile closure validation treats as *installed* root edges.
#: ``peerDependencies`` is deliberately absent: validation leaves root peers
#: to the consumer environment rather than installing them, so copying them
#: into the manifest would make ``npm ci`` install an unvalidated package.
MANIFEST_DEPENDENCY_KEYS = (
    "dependencies",
    "devDependencies",
    "optionalDependencies",
)

#: Canonical consumer-neutral assembler script (fixed bytes).
ASSEMBLER_SCRIPT = """\
#!/bin/sh
set -eu

mkdir -p /cache/home

node_actual="$(node --version)"
if [ "${node_actual}" != "v${REVIEWED_NODE_VERSION}" ]; then
    printf 'node version mismatch: got %s, want v%s\\n' "${node_actual}" "${REVIEWED_NODE_VERSION}" >&2
    exit 65
fi

npm_actual="$(npm --version)"
if [ "${npm_actual}" != "${REVIEWED_NPM_VERSION}" ]; then
    printf 'npm version mismatch: got %s, want %s\\n' "${npm_actual}" "${REVIEWED_NPM_VERSION}" >&2
    exit 66
fi

cd /work

node -e 'const fs=require("fs");const lock=JSON.parse(fs.readFileSync("/work/package-lock.json","utf8"));const r=lock.packages[""];const pkg={name:r.name||"npm-assembler",version:r.version||"0.0.0",private:true};for(const k of ["dependencies","devDependencies","optionalDependencies"]){if(r[k]&&typeof r[k]==="object")pkg[k]=r[k];}fs.writeFileSync("/work/package.json",JSON.stringify(pkg,null,2)+"\\n");'

exec npm ci --ignore-scripts --no-bin-links --no-audit --no-fund --loglevel=http
"""


def assembler_script_bytes() -> bytes:
    """Return the exact UTF-8 bytes of the canonical assembler script."""
    return ASSEMBLER_SCRIPT.encode("utf-8")


def assembler_script_digest() -> str:
    """Return the SHA-256 hex digest of the canonical assembler script."""
    return hashlib.sha256(assembler_script_bytes()).hexdigest()


def npm_policy_flags() -> tuple[str, ...]:
    """Return the one fixed npm invocation policy flag tuple."""
    return NPM_CI_FLAGS


def npm_policy() -> dict:
    """Return the canonical reviewed npm policy payload.

    The payload binds the four fixed script-free invocation flags with the
    reviewed finite limits.  The four npm request/retry limits are rendered
    into the assembler environment and enforced by npm today; the reviewed
    total assembly duration is currently included only in the canonical
    policy identity, and its runtime enforcement belongs to Phase 3.
    """
    return {
        "flags": list(NPM_CI_FLAGS),
        "request_timeout_ms": NPM_REQUEST_TIMEOUT_MS,
        "retry_count": NPM_RETRY_COUNT,
        "retry_min_timeout_ms": NPM_RETRY_MIN_TIMEOUT_MS,
        "retry_max_timeout_ms": NPM_RETRY_MAX_TIMEOUT_MS,
        "total_timeout_seconds": ASSEMBLY_TOTAL_TIMEOUT_SECONDS,
    }


def npm_policy_digest() -> str:
    """Return the SHA-256 hex digest of the canonical npm policy."""
    payload = json.dumps(
        npm_policy(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def npm_policy_env() -> tuple[tuple[str, str], ...]:
    """Return the npm request/retry environment for the assembler only.

    These are the four npm ``fetch-*`` settings rendered into the standalone
    assembler container environment and enforced by npm.  The reviewed total
    assembly duration is deliberately absent here: it is not an npm network
    setting, and its runtime enforcement belongs to Phase 3.
    """
    return (
        ("npm_config_fetch_timeout", str(NPM_REQUEST_TIMEOUT_MS)),
        ("npm_config_fetch_retries", str(NPM_RETRY_COUNT)),
        ("npm_config_fetch_retry_mintimeout", str(NPM_RETRY_MIN_TIMEOUT_MS)),
        ("npm_config_fetch_retry_maxtimeout", str(NPM_RETRY_MAX_TIMEOUT_MS)),
    )
