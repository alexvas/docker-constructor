## ADDED Requirements

### Requirement: Apply one enabled client CA environment policy
When corporate trust is enabled, the constructor SHALL set `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `PIP_CERT`, and `CURL_CA_BUNDLE` to exactly `/etc/ssl/certs/ca-certificates.crt` for every networked Dockerfile build-stage command and for every runtime container launched by the constructor. These values SHALL be derived solely from enabled corporate trust, SHALL NOT be configurable as arbitrary paths or inherited from the invoking host, and SHALL NOT be persisted in image `ENV` metadata. The variables are compatibility guidance for enterprise TLS interception; `NODE_EXTRA_CA_CERTS` SHALL NOT be represented as replacing Node's built-in trust roots. Direct image launches that bypass the constructor SHALL remain outside the runtime propagation contract.

#### Scenario: Enabled trust reaches build clients
- **WHEN** corporate trust is enabled and a Dockerfile build stage performs a network operation
- **THEN** that operation SHALL receive all five client CA variables
- **AND** every variable SHALL equal `/etc/ssl/certs/ca-certificates.crt`
- **AND** conflicting values inherited from the base image SHALL be overridden for that operation

#### Scenario: Enabled trust reaches a constructor-launched container
- **WHEN** the constructor launches a runtime container with corporate trust enabled
- **THEN** the run vector SHALL pass all five client CA variables with the fixed system-bundle path
- **AND** the current corporate bundle SHALL be mounted read-only at that path

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
