# Capability: corporate-network-configuration

## Purpose

Define optional machine-local corporate trust and credential-free proxy configuration for Docker builds and launched runtime containers.

## Requirements

### Requirement: Configure optional local corporate trust
The system SHALL support an optional `[corporate-trust]` section only in `docker-constructor.local.toml` beneath the selected constructor project directory. `enabled` SHALL be a boolean; absent or `false` SHALL disable corporate trust. When `enabled = true`, the system SHALL require the sole trust source to be `<project-directory>/.docker-local/corporate-ca-bundle.crt`, validate that it is readable and contains one or more nonempty PEM `CERTIFICATE` blocks, and reject missing, unreadable, malformed, or unknown local configuration before Docker execution. The trust path SHALL never be derived from the constructor installation directory. PEM validation SHALL be dependency-free and SHALL require ASCII input, matching complete `BEGIN CERTIFICATE`/`END CERTIFICATE` delimiters, no non-whitespace content outside those blocks, and strictly decodable nonempty Base64 payloads. It SHALL not parse, verify, or assess payloads as X.509 certificates; certificate validity, trust-chain validity, and organizational trust coverage remain the operator's responsibility. The supplied file SHALL be treated as a complete replacement trust bundle.

#### Scenario: Corporate trust is disabled by default
- **WHEN** the selected project's local companion is absent or omits `[corporate-trust]`
- **THEN** build and run SHALL retain the standard image trust store
- **AND** SHALL not require `.docker-local/corporate-ca-bundle.crt`

#### Scenario: Enabled corporate trust has a valid fixed bundle
- **WHEN** the selected project's local companion declares `[corporate-trust] enabled = true` and its fixed bundle is valid
- **THEN** build and run planning SHALL use that exact project-owned file as the only corporate trust source
- **AND** SHALL not read an arbitrary certificate path from configuration or the tool installation

#### Scenario: Enabled corporate trust has no usable bundle
- **WHEN** corporate trust is enabled and the selected project's fixed bundle is missing, unreadable, empty, has incomplete or unmatched certificate delimiters, contains non-whitespace text outside certificate blocks, or has an empty or non-Base64-decodable block payload
- **THEN** the invoking command SHALL fail with a path-specific CONFIG error before Docker execution

#### Scenario: Certificate semantics remain an operator responsibility
- **WHEN** an enabled fixed bundle has complete PEM `CERTIFICATE` blocks with nonempty, strictly decodable Base64 payloads
- **THEN** the invoking command SHALL accept the bundle without an X.509 parser or external Python dependency
- **AND** any invalid certificate object, expired certificate, invalid signature, missing trust root, or incomplete corporate coverage SHALL remain an operator responsibility

### Requirement: Preserve default trust configuration when corporate trust is disabled
When `[corporate-trust]` is absent or `enabled = false`, the constructor SHALL preserve the base image's default certificate and trust configuration. It SHALL NOT add, replace, remove, or override trust bundles, certificate paths, certificate directories, or client-specific CA settings at build or runtime. This prohibition includes, but is not limited to, injecting or persisting `SSL_CERT_FILE`, `SSL_CERT_DIR`, `NODE_EXTRA_CA_CERTS`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `GIT_SSL_CAINFO`, `NPM_CONFIG_CAFILE`, or analogous settings for other TLS clients. Values already defined by the selected base image SHALL remain unchanged.

#### Scenario: Disabled trust preserves base-image client behavior
- **WHEN** the local companion omits `[corporate-trust]` or declares `enabled = false`
- **THEN** the Dockerfile, build vector, and run vector SHALL introduce no certificate or trust override
- **AND** SHALL not persist or inject any constructor-defined CA path or client-specific trust setting
- **AND** certificate and trust settings inherited from the selected base image SHALL remain unchanged

### Requirement: Apply enabled trust at build and restarted runtime
When corporate trust is enabled, the Dockerfile SHALL validate the fixed complete bundle, ensure the parent directory for `/etc/ssl/certs/ca-certificates.crt` exists, and replace that system bundle before image-stage network operations. Pre-network parent-directory bootstrap SHALL be scoped to enabled corporate trust. When corporate trust is disabled, the constructor SHALL NOT perform that bootstrap or install a constructor-provided replacement bundle before the first network operation. For this requirement, "launched runtime container" excludes the internal build-verification containers launched by `docker/versioning/verification.py` and the gateway-probe containers launched by `docker/networking.py`; the constructor SHALL bind-mount the same current host file read-only at that path for each other launched runtime container, including standalone npm assembler containers. The two excluded internal diagnostic launch paths SHALL NOT receive a constructor-injected corporate bundle mount regardless of the corporate-trust setting. A restarted or newly launched in-scope runtime container SHALL receive a changed bundle without rebuilding the image; already-running containers and processes SHALL not be required to reload it. Any certificate-path or client-specific CA setting introduced by the constructor SHALL be scoped to enabled corporate trust and SHALL point to the replacement system bundle.

#### Scenario: Build bootstraps enabled corporate trust before package downloads
- **WHEN** an enabled trust bundle is used to build an image whose base does not contain `/etc/ssl/certs`
- **THEN** the Dockerfile SHALL validate the bundle before creating the missing parent directory
- **AND** SHALL create the parent directory before replacing the system CA bundle
- **AND** base-stage networked package and installer operations SHALL run after the system CA bundle replacement
- **AND** the final image SHALL contain the replacement bundle

#### Scenario: Disabled trust skips pre-network directory bootstrap
- **WHEN** corporate trust is absent or disabled and the base image does not contain `/etc/ssl/certs`
- **THEN** the constructor SHALL NOT create that directory as part of the corporate-trust bootstrap before the first network operation
- **AND** SHALL NOT install a constructor-provided replacement bundle
- **AND** this restriction SHALL NOT prohibit normal package installation from creating or populating the standard trust directory later in the build

#### Scenario: Runtime receives an updated bundle after restart
- **WHEN** the fixed host bundle changes after an image was built and an in-scope runtime container is subsequently launched or restarted
- **THEN** the container SHALL receive the current file as a read-only mount at the system CA bundle path
- **AND** the image SHALL not need rebuilding for that launch

#### Scenario: Internal diagnostic launches do not receive the bundle mount
- **WHEN** the constructor launches an internal build-verification container through `docker/versioning/verification.py` or a gateway-probe container through `docker/networking.py`, regardless of whether corporate trust is enabled
- **THEN** its launch vector SHALL NOT introduce a corporate bundle mount
- **AND** this exception SHALL NOT apply to any other constructor-launched container, including standalone npm assembler containers

### Requirement: Apply one enabled client CA environment policy
When corporate trust is enabled, the constructor SHALL set `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT`, and `CURL_CA_BUNDLE` to exactly `/etc/ssl/certs/ca-certificates.crt` for every networked Dockerfile build-stage command and for every runtime container launched by the constructor, including standalone npm assembler containers, except the internal build-verification containers launched by `docker/versioning/verification.py` and the gateway-probe containers launched by `docker/networking.py`. These values SHALL be derived solely from enabled corporate trust, SHALL NOT be configurable as arbitrary paths or inherited from the invoking host, and SHALL NOT be persisted in image `ENV` metadata. The variables are compatibility guidance for enterprise TLS interception; `NODE_EXTRA_CA_CERTS` SHALL NOT be represented as replacing Node's built-in trust roots. Direct image launches that bypass the constructor SHALL remain outside the runtime propagation contract.

#### Scenario: Enabled trust reaches build clients
- **WHEN** corporate trust is enabled and a Dockerfile build stage performs a network operation
- **THEN** that operation SHALL receive all five client CA variables
- **AND** every variable SHALL equal `/etc/ssl/certs/ca-certificates.crt`
- **AND** conflicting values inherited from the base image SHALL be overridden for that operation

#### Scenario: Enabled trust reaches a constructor-launched container
- **WHEN** the constructor launches an in-scope runtime container (including a standalone npm assembler) with corporate trust enabled
- **THEN** the run vector SHALL pass all five client CA variables with the fixed system-bundle path
- **AND** the current corporate bundle SHALL be mounted read-only at that path

#### Scenario: Internal build verification and gateway probes do not receive trust injection
- **WHEN** the constructor launches an internal build-verification container through `docker/versioning/verification.py` or a gateway-probe container through `docker/networking.py`, regardless of whether corporate trust is enabled
- **THEN** its launch vector SHALL introduce neither a corporate bundle mount nor any of the five client CA assignments
- **AND** inherited image trust settings SHALL remain unchanged
- **AND** this exception SHALL NOT exclude verification of the CA environment and bundle mount of an in-scope runtime container
- **AND** no other constructor-launched container SHALL be excluded merely because its current launch vector lacks a bundle mount

#### Scenario: Disabled trust preserves inherited client settings
- **WHEN** corporate trust is absent or disabled
- **THEN** build and run vectors SHALL introduce none of the five client CA variables
- **AND** inherited image settings for those names SHALL remain unchanged

#### Scenario: Client CA settings do not persist in image metadata
- **WHEN** an image is built with corporate trust enabled
- **THEN** none of the five client CA variables SHALL be recorded as persistent image `ENV` metadata
- **AND** a direct launch of that image outside the constructor SHALL not be claimed to receive the conditional runtime environment

#### Scenario: Node trust remains augmentation-oriented
- **WHEN** a Node process receives `NODE_EXTRA_CA_CERTS` through enabled corporate trust
- **THEN** the system SHALL describe the variable as enabling enterprise TLS interception through additional certificates
- **AND** SHALL NOT claim that it removes or replaces Node's built-in trust roots

### Requirement: Configure optional credential-free local proxy
The system SHALL support an optional `[network.proxy]` section only in the resolved local companion. Its required `url` SHALL be a credential-free URI with an explicit host and port and a scheme of exactly `http`, `socks5`, or `socks5h`; userinfo, fragments, unsupported schemes, missing hosts, missing ports, malformed values, and unknown keys SHALL be rejected before Docker execution. Its optional `no_proxy` value SHALL be emitted only when explicitly configured.

#### Scenario: Proxy is absent by default
- **WHEN** the local companion omits `[network.proxy]`
- **THEN** build and run SHALL not emit proxy or bypass environment variables

#### Scenario: Credential-free external proxy is accepted
- **WHEN** the local companion configures `url = "http://proxy.corp.example:3128"`
- **THEN** build and run planning SHALL accept the endpoint without requiring host-access configuration

#### Scenario: Credential-bearing proxy is rejected
- **WHEN** the configured proxy URL includes URI userinfo such as `user:password@`
- **THEN** the invoking command SHALL fail with a path-specific CONFIG error before Docker execution

### Requirement: Propagate local proxy without persisting it in the image
When a local proxy is configured, the constructor SHALL transport its exact URL into the Docker build through constructor-specific build arguments that cannot be shadowed by same-named proxy `ENV` values inherited from the base image. Before every networked build-stage command, the Dockerfile SHALL conditionally export that URL under `HTTP_PROXY`, `http_proxy`, `HTTPS_PROXY`, `https_proxy`, `ALL_PROXY`, and `all_proxy`, overriding inherited values only on the configured path. An explicitly configured bypass list SHALL similarly be transported through a constructor-specific build argument and conditionally exported under both `NO_PROXY` and `no_proxy`; when omitted, the constructor SHALL introduce neither bypass variable. At runtime, the constructor SHALL propagate the standard proxy variables through direct Docker environment arguments. None of these settings SHALL become persistent image `ENV` values. `socks5` and `socks5h` build operation SHALL be best-effort: a build client that does not support the configured SOCKS URL can fail normally.

#### Scenario: HTTP proxy is propagated in all supported forms
- **WHEN** a credential-free HTTP proxy and explicit bypass list are configured
- **THEN** the build vector SHALL carry the exact values through constructor-specific proxy arguments
- **AND** every networked build-stage command SHALL receive each required uppercase and lowercase standard proxy variable with those values, overriding conflicting inherited proxy settings
- **AND** the run vector SHALL contain each required uppercase and lowercase standard proxy variable with those values
- **AND** the image configuration SHALL not persist proxy `ENV` values

#### Scenario: SOCKS build client rejects the configured proxy
- **WHEN** a build-stage client does not support a configured `socks5` or `socks5h` URL
- **THEN** the build MAY fail with that client's normal operational error
- **AND** the constructor SHALL not reinterpret the endpoint or claim universal SOCKS build support

### Requirement: Preserve Docker daemon/client boundary
The project-managed corporate trust and proxy configuration SHALL apply only to Dockerfile build-stage processes and launched runtime containers. It SHALL NOT configure the Docker client or daemon, registry authentication, image registry trust, or base-image pulls performed for `FROM`.

#### Scenario: Docker pulls require separate host configuration
- **WHEN** a Docker daemon needs proxy or trust configuration to pull a base image
- **THEN** the constructor SHALL not claim that local corporate network configuration controls that operation
- **AND** documentation SHALL identify daemon/client configuration as external to this feature

### Requirement: Apply resolved credential-free network policy to npm assembler containers
Standalone npm assembler containers SHALL receive the same resolved credential-free proxy and enabled corporate trust inputs as host-orchestrated dependency acquisition, scoped only to the assembly process. Enabled trust SHALL be mounted read-only at the fixed system trust path before npm network access; disabled trust SHALL introduce no override. The assembler SHALL NOT persist configured proxy endpoints or trust paths in command displays, logs, evidence, output trees, or cache manifests.

#### Scenario: Assembling behind an enabled corporate network
- **WHEN** corporate trust and a credential-free proxy are enabled and valid
- **THEN** the assembler SHALL use those resolved inputs for locked HTTPS registry requests
- **AND** the assembled output and evidence SHALL contain neither the configured proxy endpoint nor trust path

#### Scenario: Preserving default assembler trust
- **WHEN** corporate trust and proxy configuration are disabled
- **THEN** assembler execution SHALL preserve the pinned image's default trust and introduce no constructor-defined certificate or proxy setting

### Requirement: Apply corporate network policy to host artifact materialization
When corporate trust or a credential-free proxy is enabled, host-side build artifact requests SHALL use the validated local proxy and replacement trust bundle under the same confidentiality and redaction rules as build networking. When disabled, host requests SHALL retain standard host trust and proxy behavior without constructor-defined certificate overrides. Invalid enabled configuration SHALL prevent downloads and Docker execution.

#### Scenario: Downloading through enabled corporate configuration
- **WHEN** a selected build artifact is absent and valid corporate trust or proxy settings are enabled
- **THEN** host materialization SHALL apply those settings to the artifact request
- **AND** diagnostics SHALL not reveal proxy credentials or sensitive endpoint components

#### Scenario: Materializing with corporate configuration disabled
- **WHEN** corporate trust and constructor proxy settings are disabled
- **THEN** host materialization SHALL introduce no constructor-defined CA path or proxy override

#### Scenario: Rejecting invalid corporate configuration before download
- **WHEN** enabled corporate settings fail their existing validation
- **THEN** the build SHALL fail before host artifact network access, snapshot publication, or Docker execution
