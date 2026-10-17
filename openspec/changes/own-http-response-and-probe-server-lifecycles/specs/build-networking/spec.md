# Spec Delta

## ADDED Requirements

### Requirement: Own the host probe listener across startup failure
Gateway diagnosis SHALL retain one explicit release authority for each acquired host probe listener before fallible post-acquisition setup. Startup failure or interruption SHALL release every safely releasable acquired resource and account for every started worker. Failure before acquisition SHALL NOT fabricate a listener or call a shutdown operation that waits for a server loop that never started.

#### Scenario: Listener acquisition fails
- **WHEN** acquiring the probe listener fails before a server is returned
- **THEN** no serving worker SHALL be started for that listener
- **AND** no borrowed or nonexistent listener SHALL be released

#### Scenario: Worker preparation or startup fails
- **WHEN** listener acquisition succeeds but address extraction, worker construction, readiness or startup fails
- **THEN** the acquired listener SHALL receive one release attempt once safe
- **AND** cleanup SHALL preserve the selected startup failure under shared precedence
- **AND** teardown SHALL NOT deadlock waiting for a never-entered serving loop or join a never-started worker

#### Scenario: Start is requested while ownership remains active
- **WHEN** another start is requested while a prior listener or serving worker remains owned
- **THEN** startup SHALL reject the request before acquiring another listener
- **AND** SHALL NOT overwrite or discard the existing cleanup responsibility

### Requirement: Stop the host probe without skipped or repeated cleanup
Probe shutdown SHALL coordinate one stop request, safe terminal listener release and serving-worker completion. Every independent safe cleanup action SHALL be attempted despite other failures. Repeated, concurrent or reentrant stop calls SHALL share the same request and winning release outcome, never retry an ambiguous release, and never mistake a claimed release for completed cleanup.

#### Scenario: Ordinary shutdown succeeds
- **WHEN** gateway diagnosis completes and stops its probe
- **THEN** the serving worker SHALL stop and the listener SHALL receive exactly one terminal release attempt
- **AND** successful stop SHALL leave no owned listener or serving worker
- **AND** subsequent stop calls SHALL perform no additional release

#### Scenario: A cleanup action fails or is interrupted
- **WHEN** stop signalling, listener release or worker joining fails or is interrupted
- **THEN** every independently safe cleanup action SHALL still be attempted once
- **AND** first interruption, unexpected defect, original ordinary operation failure and ordinary cleanup failure SHALL retain shared precedence in that order
- **AND** the selected exception identity SHALL remain unchanged before any existing domain projection

#### Scenario: A release is still running or a worker has not stopped
- **WHEN** the bounded worker wait expires or another caller has claimed but not completed listener release
- **THEN** shutdown SHALL NOT report completed cleanup
- **AND** the same lifecycle SHALL retain worker, resource and winning-outcome responsibility until safe settlement
- **AND** repeated callers SHALL NOT acquire fresh waiting budgets, close a listener beneath unsafe worker use or discard retained failures
- **AND** arbitrary blocked injected operations SHALL NOT be promised forcible termination

#### Scenario: Diagnosis and cleanup both fail
- **WHEN** gateway diagnosis fails and probe teardown also fails ordinarily
- **THEN** the diagnosis failure SHALL remain authoritative with cleanup failures retained secondarily
- **AND** cleanup SHALL NOT replace it through an unprotected finalization path

### Requirement: Preserve gateway diagnosis policy during lifecycle correction
Probe lifecycle changes SHALL preserve the networking service API, token response, bind policy, gateway ordering, Docker-mode detection, probe commands, persistence boundaries and normal diagnostics. HTTP/server policy SHALL remain in networking rather than the filesystem foundation, and image builds or non-gateway host-access modes SHALL NOT gain diagnosis side effects.

#### Scenario: Existing gateway diagnosis behavior is retained
- **WHEN** successful or failed gateway diagnosis uses the corrected probe lifecycle
- **THEN** characterized candidate order, token exchange, selected address and persistence behavior SHALL remain unchanged
- **AND** a real loopback test SHALL verify listener release and worker completion without requiring Docker or an external network
