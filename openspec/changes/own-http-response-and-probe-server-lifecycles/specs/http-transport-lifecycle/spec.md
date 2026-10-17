# Spec Delta

## Purpose

Define deterministic ownership and release of HTTP response resources used by metadata requests and streamed artifact downloads, without changing request or content policy.

## ADDED Requirements

### Requirement: Release successful and error responses deterministically
Every acquired HTTP response, including a response delivered as an HTTP-status exception, SHALL have one explicit release authority before fallible use. Response resources SHALL be released without relying on garbage collection, and a failed or interrupted release SHALL NOT be retried or duplicated through an independently owned descriptor number.

#### Scenario: Buffered request returns a successful response
- **WHEN** a buffered request reads a successful response
- **THEN** its response resource SHALL receive one release attempt before the request returns
- **AND** returned status, headers and body bytes SHALL retain their established values

#### Scenario: Buffered request receives an HTTP-status exception
- **WHEN** the HTTP client delivers a response through its HTTP-status error path
- **THEN** its body SHALL be owned and released once after reading
- **AND** successful body processing SHALL retain the response's HTTP status, headers and bytes
- **AND** an error response without an acquired body SHALL NOT cause a fabricated resource or descriptor close

#### Scenario: Streaming acquisition raises an HTTP-status exception
- **WHEN** streaming acquisition raises an HTTP-status exception containing a response resource
- **THEN** the resource SHALL receive one release attempt before propagation
- **AND** an ordinary release failure SHALL remain secondary to that exact acquisition error

#### Scenario: Ownership preparation fails
- **WHEN** resource-owner preparation fails before acquisition
- **THEN** no HTTP request SHALL start
- **AND** no caller-owned response SHALL be closed as a rejected adoption

### Requirement: Preserve transport authority across cleanup failures
Response cleanup SHALL retain first control-flow interruption, unexpected cleanup defect, original ordinary operation failure and ordinary cleanup failure in that precedence order. Every independently required cleanup action SHALL be attempted once when safe. Domain error translation SHALL remain at the existing transport or artifact boundary, and public diagnostics SHALL preserve existing sanitization.

#### Scenario: Reading and releasing both fail
- **WHEN** a response read or body-processing operation fails and response release also fails ordinarily
- **THEN** the original operation failure SHALL remain authoritative with release failure retained secondarily
- **AND** its existing domain translation or raw propagation SHALL remain unchanged

#### Scenario: Only response release fails
- **WHEN** body processing succeeds but response release raises an ordinary I/O error
- **THEN** a buffered metadata request SHALL use its established operational-failure response mapping rather than report the successfully read response
- **AND** a streamed artifact request SHALL fail through its existing transport-error boundary before artifact publication

#### Scenario: Cleanup is interrupted or defective
- **WHEN** response release raises a control-flow interruption or unexpected defect
- **THEN** shared precedence SHALL select authority without changing the selected exception object's identity
- **AND** ordinary transport-error translation SHALL NOT swallow control flow
- **AND** repeated finalization SHALL NOT attempt release again

### Requirement: Finalize streamed responses on all consumption boundaries
A streamed HTTP response SHALL remain owned while its iterator is active and SHALL be finalized on exhaustion, read failure, interruption or explicit iterator termination. An artifact consumer that stops because its own processing failed SHALL deterministically finalize its acquired closeable iterator without requiring garbage collection or a new interface from compatible plain iterators.

#### Scenario: Streaming reaches EOF
- **WHEN** a streamed response reaches clean EOF
- **THEN** its release attempt SHALL finish before consumption is reported successful
- **AND** chunk sizes, ordering and bytes SHALL retain their established behavior without whole-body buffering

#### Scenario: Artifact processing stops consumption early
- **WHEN** hashing, chunk validation, temporary-file writing or cancellation stops artifact consumption before EOF
- **THEN** the consumer SHALL finalize the acquired closeable iterator and its response once
- **AND** the processing failure SHALL remain authoritative over ordinary finalization failures
- **AND** existing temporary-artifact cleanup SHALL still run and failed acquisition SHALL NOT publish an artifact

#### Scenario: Caller explicitly terminates an iterator
- **WHEN** a caller explicitly closes or throws an interruption into a partially consumed response iterator
- **THEN** the response SHALL receive one release attempt before termination completes
- **AND** Python generator termination semantics SHALL remain intact, including normal handling of GeneratorExit by generator close
- **AND** merely dropping an externally retained iterator SHALL NOT be promised deterministic release

#### Scenario: Injected transport provides a plain iterator
- **WHEN** a compatible injected transport returns an iterator without a close method
- **THEN** artifact consumption SHALL retain its existing interface and behavior
- **AND** SHALL NOT manufacture raw resource-release authority from the iterator's name or type

### Requirement: Preserve HTTP and artifact policy during lifecycle correction
Lifecycle changes SHALL preserve request methods, URLs, headers, authentication, timeouts, cache behavior, status interpretation, streamed content, artifact integrity and publication policy. Network-resource ownership SHALL NOT introduce retries, extra requests, whole-body buffering, new public result fields or unsanitized diagnostics.

#### Scenario: Metadata and artifact regression comparison
- **WHEN** buffered metadata requests and streamed artifact downloads run through the corrected lifecycle
- **THEN** their characterized inputs and outputs SHALL remain unchanged except for explicitly required release and cleanup-failure behavior
- **AND** real loopback tests SHALL demonstrate completed resource release independently of descriptor-less doubles
