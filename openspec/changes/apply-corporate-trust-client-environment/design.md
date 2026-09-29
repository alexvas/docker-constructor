## Context

See `proposal.md` for motivation. The existing corporate trust path already validates and installs the project-owned complete bundle at `/etc/ssl/certs/ca-certificates.crt`, sources `docker/corp-network-env.sh` before networked Dockerfile commands, and bind-mounts the current host bundle read-only at the same path for constructor-launched containers. The helper currently exports only `SSL_CERT_FILE` and `NODE_EXTRA_CA_CERTS`; the runtime run vector mounts the bundle but does not provide client-specific CA variables.

The disabled path deliberately preserves same-named values inherited from the base image. Client CA settings must remain absent from persistent image metadata, and the local configuration must not become an arbitrary environment or path-injection channel.

## Goals / Non-Goals

**Goals:**

- Use one fixed five-variable CA environment contract for build-stage network operations and constructor-launched runtime containers.
- Override inherited client CA values only when corporate trust is enabled.
- Keep runtime variables aligned with the existing read-only system-bundle mount.
- Make the policy directly testable at Dockerfile, render-plan, orchestration, and runtime-verification boundaries.

**Non-Goals:**

- Persist CA variables in image metadata or modify direct image launches outside the constructor.
- Add configuration fields for variable names, values, or certificate paths.
- Change proxy propagation, Docker daemon/client trust, registry pulls, or `FROM` resolution.
- Guarantee strict replacement of Node's built-in trust roots or universal behavior for every TLS library.

## Decisions

### Define one closed client CA environment mapping

The constructor-owned mapping is:

```text
NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt
SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
PIP_CERT=/etc/ssl/certs/ca-certificates.crt
CURL_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
```

The names and path are constants rather than local configuration. This prevents arbitrary environment injection and guarantees that every client points to the same validated/replaced bundle.

Alternative: expose configurable variables or paths. Rejected because corporate trust intentionally has one fixed source and destination.

### Extend the existing build helper

The existing conditional helper remains the build-stage owner. Its enabled branch exports all five variables from the constructor-specific CA path argument; its disabled branch exports none. Every existing networked `RUN` continues to source the helper, preserving one audit point and avoiding persistent Dockerfile `ENV` instructions.

Alternative: duplicate exports in each `RUN`. Rejected because duplicated policy would drift and weaken complete network-operation coverage.

### Emit direct Docker runtime environment arguments

Run planning adds the five fixed `--env NAME=value` entries only when the resolved corporate trust policy is enabled. They travel alongside, but remain logically separate from, the existing read-only bundle mount. Disabled planning emits no entries, so inherited image values remain untouched.

Alternative: persistent image `ENV`. Rejected because Dockerfile metadata cannot conditionally omit keys without separate final stages, would affect direct image launches, and could overwrite inherited settings when trust is disabled.

Alternative: an entrypoint wrapper. Rejected because it would couple trust behavior to process startup, complicate arbitrary commands, and make image behavior depend on runtime probing rather than the resolved launch policy.

### Verify policy rather than TLS connectivity

Runtime verification should inspect the launched container's environment for exact enabled values and exact disabled absence/non-injection while retaining the existing mount check. It should not make an external TLS request: connectivity depends on operator-provided certificates, network reachability, and interception infrastructure outside the constructor contract.

### Document Node augmentation explicitly

`NODE_EXTRA_CA_CERTS` adds certificates to Node's built-in trust behavior; it is suitable for the stated enterprise interception goal but is not a strict replacement mechanism. Documentation distinguishes this from replacement of the filesystem system bundle.

## Risks / Trade-offs

- **[Client-specific variables have different precedence rules]** → Set all supported variables to the same fixed bundle and test exact values at both build and runtime boundaries.
- **[A newly added networked Dockerfile command may omit the helper]** → Retain and extend the existing structural test that enumerates networked `RUN` blocks.
- **[Disabled mode could accidentally clear inherited values]** → Require complete omission of constructor-generated entries rather than empty assignments and test conflicting inherited values.
- **[`NODE_EXTRA_CA_CERTS` does not remove Node roots]** → State augmentation semantics explicitly and limit the objective to enterprise TLS interception compatibility.
- **[Environment inspection may disclose only a non-secret fixed path]** → Keep certificate contents and host source paths out of variables; expose only the fixed in-container system path.

## Migration Plan

No configuration migration is required. Existing projects with corporate trust enabled receive the additional build and runtime client hints automatically. Disabled projects remain byte-for-byte equivalent at the trust environment boundary. Rollback consists of removing the added helper exports and runtime environment entries while retaining the existing bundle replacement and mount behavior.
