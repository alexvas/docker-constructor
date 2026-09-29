## MODIFIED Requirements

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

## ADDED Requirements

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
