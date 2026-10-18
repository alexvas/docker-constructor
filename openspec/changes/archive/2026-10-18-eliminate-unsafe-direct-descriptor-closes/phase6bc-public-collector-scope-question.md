# Phase 6BC public collector scope question

## Discovery

While inventorying 6BC.10 consumers and the removal boundary for 6BC.18:

- `docker/npm_environment/__init__.py` imports `collect_streams` at line 154 and includes it in `__all__` at line 244.
- `docker/npm_environment/streaming.py:1275` defines its keyword-only public contract over arbitrary `stdout_read`/`stderr_read` callables, reader-failure/interruption callbacks, abort event and domain diagnostic policy. It creates and joins reader workers independently of OwnedProcess.
- Current DockerRunExecutor migration uses `_DiagnosticCollector`; the old public collector remains exported and directly exercised by multiple diagnostic/streaming suites.
- `proposal.md` compatibility and `design.md` Non-Goals preserve consumer public APIs except specifically authorized changes. The explicit API revisions concern docker.process and run_captured's owner return, not removal or signature replacement of exported collect_streams.
- 6BC.18 removes obsolete independent reader/coordinator machinery and asks for fixture migration rather than compatibility machinery. It does not explicitly distinguish this exported arbitrary-byte-reader API from obsolete private process helpers.
- OwnedProcess acquires children at launch and expressly excludes existing-process/external-reader adoption. A transparent delegation of the existing arbitrary-read-callable contract is therefore not available.

## Historical question before GREEN (resolved below)

Clarify whether the exported collect_streams API:

1. Is explicitly removed in this change (including package export and migration of its direct tests/callers to diagnostic components/OwnedProcess); or
2. Remains a supported standalone arbitrary-byte diagnostic API, explicitly outside process ownership, with no production subprocess adapter invoking it. This preserves its signature but retains its independent byte-reader coordination, which must be distinguished from the obsolete machinery removed in 6BC.18.

A third option is an explicitly approved public API redesign, but existing-process adoption, another generic owner, or an unapproved compatibility shim must not be introduced to evade the boundary.

At the pause no option had been selected; that historical state is not a current blocker. RED 6BC.6–9 evidence is recorded separately in phase6bc-orthogonal-red-verification.md.

## Resolution

The user clarified that removing the public collect_streams API was not planned. Preserve it as a standalone arbitrary-byte diagnostic collector and preserve its package export. Subprocess adapters use OwnedProcess with _DiagnosticCollector; remove obsolete private process helpers, not the standalone API. No implementation question remains on this boundary. Subsequent generic GREEN and migration evidence is in phase6bc-value-green-verification.md.
