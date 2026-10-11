# P4 — policy rendering versus validated metadata

Task 4.8 documentation for `linearize-diagnostic-url-detection`.  Every example
below is executable against the P4 focused suite
`tests/test_diagnostic_policy_phase4.py`; the corresponding test name is named
inline so documentation and assertion cannot drift.

## Separate products, separate frontiers

Rendering and metadata are independent.  The selected local policy controls
only the rendered text; metadata is derived separately and only from an
independently validated, secret-free supported URL.  Rendering markers are
display strings, never validated identities.

| Product | Selected by | Secret-bearing host | Secret-bearing path | Credential secret only |
| --- | --- | --- | --- | --- |
| `redacted` text | `NetworkUrlDisplay.REDACTED` | `<redacted>` | `<redacted>` | `<redacted>` |
| `host-path` text | `NetworkUrlDisplay.HOST_PATH` | `<redacted-host>/public` | `host/<redacted-path>` | `host/public` |
| normalized-host fact | internal `hostnames` | absent | present (`host`) | present (`host`) |
| host/path display fact | internal `host_paths` | absent | `host/<redacted-path>` | `host/public` |
| URL fingerprint | internal `url_fingerprints` (`SessionUrlIdentity`) | absent | absent | present |

Examples (task 4.3, `DecodedSecretScopeTests`):

```python
# credential secret only: safe host/path facts and fingerprint survive
project("https://user:TOKEN@host.example/public end", ("TOKEN",))
# -> "host.example/public end", hostnames ("host.example",), 1 fingerprint

# host secret: no normalized-host fact and no fingerprint
project("https://TOKEN.example/public end", ("TOKEN",))
# -> "<redacted-host>/public end", hostnames (), 0 fingerprints

# path secret: safe hostname survives, fingerprint suppressed
project("https://host.example/TOKEN/private end", ("TOKEN",))
# -> "host.example/<redacted-path> end", hostnames ("host.example",), 0 fingerprints
```

A marker never becomes a fact (`PolicyMarkerTests
.test_markers_are_not_normalized_host_facts`): the masked-host and masked-path
products do not appear in `hostnames` or in the fingerprint tuple.

## Secrets that would manufacture a URL

A preceding adjacent secret is never removed and the remainder reparsed.
`TOKENhttps://host.example/public` with secret `TOKEN` renders
`<secret><redacted>` and yields no hostname, host/path, or fingerprint
(`UnknownTextPolicyTests.test_to_kenhttps_secret_prefix_does_not_manufacture_a_url`).
Deleting `TOKEN` would join into a valid `https://host.example/public`; the
source-coordinate coverage prevents that.

## Decoded-secret scope

Configured literal secrets match in the original source everywhere.  Inside a
recognized supported URL only, safe projection also matches each of the first
three percent-decoded views.  Ordinary text keeps literal-only matching, so
`%54%4f%4b%45%4e` stays literal outside a URL, and a fourth-percent-decoded
secret is never matched even when the separator itself uses the effective
depth-four spelling (`DecodedSecretScopeTests
.test_encoded_secret_falls_closed_within_three_passes`,
`.test_decoded_secret_scope_matches_literal_scope`).

## Unknown text

Unknown schemes with no supported embedded start remain ordinary text subject
to literal `<secret>` replacement, terminal safety, and bounds, and contribute
no hostname, host/path, or fingerprint on any route - delimiter, clean EOF, or
structured (`UnknownTextPolicyTests`).  URL-specific decoded secret matching
does not apply to them.

## Canonical identity

Fingerprint input preserves the lowercased scheme, the normalized hostname,
the path, and an explicitly supplied port (including an explicit default port),
and excludes user information, query, fragment, and proxy detail.  Order and
multiplicity are preserved, and each presentation session keys the digests
freshly (`CanonicalIdentityTests`).

## The unchanged exact exception

Local `exact` mode is the documented exception: it bypasses content
substitution and preserves terminal-safe source, including secrets, within the
existing bounds.  The external SDK product is always the redacted projection
independent of the local mode, so an attached SDK sink still receives
`<redacted>` / `<secret>` and never the source URL, credentials, or a
fingerprint (`FactGateRouteTests`, `PolicyMarkerTests`).  This is why the safe
projector rejects `NetworkUrlDisplay.EXACT` at construction: exact rendering is
owned by the collector, not by the substitution engine.
