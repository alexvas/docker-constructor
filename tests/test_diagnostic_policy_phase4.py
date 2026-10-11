"""Phase 4: policy rendering markers and secret-aware validated facts.

Binds tasks 4.1-4.3, 4.5-4.7 of ``linearize-diagnostic-url-detection``.  The
safe projector must keep unknown-scheme text as ordinary text with no
URL-derived metadata; render supported URLs under the selected policy with the
distinct masked products ``<redacted-host>`` and ``/<redacted-path>``; replace
standalone literal secrets with ``<secret>``; and grant normalized-host and
URL-fingerprint facts only to independently validated, secret-free hosts and
paths.  Rendering markers are display products, never validated identities.

The tests deliberately distinguish the agreed compatibility changes (unknown
schemes now stay visible; standalone secrets now render ``<secret>``) from
accidental recognition failures.
"""
from __future__ import annotations

import unittest

from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.diagnostic_projection import (
    DiagnosticProjector,
    REDACTED_PATH_MARKER,
    project_host_acquisition_failure,
    project_structured_diagnostic,
    sanitize_host_paths,
)
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
)
from docker.versioning.logical_resource import (
    DiagnosticLogicalResource,
    DiagnosticResourceKind,
)
from docker.versioning.model import NetworkUrlDisplay
from docker.versioning.npm_diagnostic_stream import NpmDiagnosticStream
from docker.versioning.npm_fetch import (
    NpmFetchRecognizer,
    canonical_fetch_text,
    parse_npm_fetch_line,
)

#: The distinct early URL product used by redacted projection.
REDACTED_URL = "<redacted>"
#: The distinct masked host product.
REDACTED_HOST = "<redacted-host>"
#: The distinct standalone secret marker.
SECRET_MARKER = "<secret>"


class _Projection:
    """One projector run: rendered text plus the drained metadata products."""

    def __init__(self, text, secrets=(), mode=NetworkUrlDisplay.HOST_PATH):
        self.projector = DiagnosticProjector(
            secrets, url_display=mode, url_identity=SessionUrlIdentity()
        )
        chunks = list(self.projector.feed_text(text))
        chunks.extend(self.projector.finish())
        self.text = "".join(chunks)
        self.hostnames = self.projector.hostnames
        self.host_paths = self.projector.host_paths
        self.fingerprints = self.projector.url_fingerprints


def _project(text, secrets=(), mode=NetworkUrlDisplay.HOST_PATH):
    return _Projection(text, secrets, mode)


def _finalized(payload, secrets=(), mode=NetworkUrlDisplay.HOST_PATH):
    """Return finalized ``(local_text, sdk_text, tail)`` for a byte payload."""
    stream = NpmDiagnosticStream("stdout", secrets, network_url_display=mode)
    chunks = list(stream.feed_bytes(payload))
    chunks.extend(stream.finish())
    done = [chunk for chunk in chunks if chunk.finalized]
    return (
        "".join(chunk.local_text or "" for chunk in done),
        "".join(chunk.text for chunk in done),
        stream.tail(),
    )


class UnknownTextPolicyTests(unittest.TestCase):
    """4.1 - unknown text stays text and creates no metadata."""

    def test_unknown_scheme_at_a_delimiter_stays_text(self):
        text = (
            "custom+transport://user:password@internal.example/"
            "private?token=value#fragment "
        )
        # ``exact`` never enters the safe projector; the collector owns that
        # route and it is exercised in test_unknown_text_is_not_hidden below.
        for mode in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(mode=mode):
                result = _project(text, mode=mode)
                self.assertEqual(text, result.text)
                self.assertEqual((), result.hostnames)
                self.assertEqual((), result.host_paths)
                self.assertEqual((), result.fingerprints)

    def test_unknown_scheme_at_clean_eof_stays_text(self):
        text = "custom+transport://user:password@internal.example/private"
        result = _project(text, mode=NetworkUrlDisplay.REDACTED)
        self.assertEqual(text, result.text)
        self.assertEqual((), result.hostnames)

    def test_unknown_scheme_on_the_structured_route_stays_text(self):
        diagnostic = project_structured_diagnostic(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream.STDOUT,
            classification=HostDiagnosticClassification.STATUS,
            text="custom+transport://user:password@internal.example/private",
        )
        self.assertEqual(
            "custom+transport://user:password@internal.example/private",
            diagnostic.text,
        )
        self.assertEqual((), diagnostic.hostnames)
        self.assertEqual((), diagnostic.url_fingerprints)

    def test_unknown_text_is_not_hidden_only_because_url_shaped(self):
        # The agreed compatibility change: unknown-scheme URL-shaped text is
        # ordinary text under every local mode and in SDK text, not hidden.
        text = "git+unknown://internal.example/private"
        for mode in NetworkUrlDisplay:
            with self.subTest(mode=mode):
                local, sdk, _ = _finalized((text + "\n").encode(), mode=mode)
                self.assertEqual(text, local)
                self.assertEqual(text, sdk)

    def test_to_kenhttps_secret_prefix_does_not_manufacture_a_url(self):
        # Replacing ``TOKEN`` with an empty string would join into a valid
        # ``https://host.example/public``; the projector must not reparse it.
        text = "TOKENhttps://host.example/public"
        for mode in (NetworkUrlDisplay.REDACTED, NetworkUrlDisplay.HOST_PATH):
            with self.subTest(mode=mode):
                result = _project(text, secrets=("TOKEN",), mode=mode)
                self.assertEqual(f"{SECRET_MARKER}{REDACTED_URL}", result.text)
                self.assertEqual((), result.hostnames)
                self.assertEqual((), result.host_paths)
                self.assertEqual((), result.fingerprints)


class PolicyMarkerTests(unittest.TestCase):
    """4.2/4.5 - policy-specific markers and standalone secret replacement."""

    def test_credential_secret_keeps_a_settled_safe_host_path(self):
        for mode, expected in (
            (NetworkUrlDisplay.REDACTED, REDACTED_URL),
            (NetworkUrlDisplay.HOST_PATH, "host.example/public"),
        ):
            with self.subTest(mode=mode):
                result = _project(
                    "https://user:TOKEN@host.example/public end",
                    secrets=("TOKEN",),
                    mode=mode,
                )
                self.assertEqual(f"{expected} end", result.text)
                self.assertEqual(("host.example",), result.hostnames)
                self.assertEqual(("host.example/public",), tuple(
                    fact.text for fact in result.host_paths
                ))
                self.assertEqual(1, len(result.fingerprints))

    def test_secret_bearing_host_renders_a_masked_host(self):
        result = _project(
            "https://TOKEN.example/public end", secrets=("TOKEN",)
        )
        self.assertEqual(f"{REDACTED_HOST}/public end", result.text)
        # Markers are display products, not normalized-host facts.
        self.assertEqual((), result.hostnames)
        self.assertEqual((), result.host_paths)
        self.assertEqual((), result.fingerprints)
        self.assertNotIn(REDACTED_HOST, result.hostnames)

    def test_secret_bearing_and_unsafe_path_render_masked_path(self):
        result = _project(
            "https://host.example/TOKEN/private end", secrets=("TOKEN",)
        )
        self.assertEqual("host.example/<redacted-path> end", result.text)
        self.assertEqual(("host.example",), result.hostnames)
        self.assertEqual(
            ("host.example/<redacted-path>",),
            tuple(fact.text for fact in result.host_paths),
        )
        self.assertEqual((), result.fingerprints)

    def test_masked_host_and_masked_path_together(self):
        result = _project(
            "https://TOKEN.example/TOKEN/private end", secrets=("TOKEN",)
        )
        self.assertEqual(
            f"{REDACTED_HOST}/<redacted-path> end", result.text
        )
        self.assertEqual((), result.hostnames)
        self.assertEqual((), result.fingerprints)

    def test_malformed_supported_url_hides_without_facts(self):
        for candidate in ("https://[broken", "https://%", "https://host.example:/x"):
            with self.subTest(candidate=candidate):
                result = _project(f"bad {candidate} end")
                self.assertIn(REDACTED_URL, result.text)
                self.assertEqual((), result.hostnames)
                self.assertEqual((), result.fingerprints)

    def test_encoded_secret_falls_closed_within_three_passes(self):
        for spelling in ("%54%4f%4b%45%4e", "%2554%254f%254b%2545%254e"):
            with self.subTest(spelling=spelling):
                result = _project(
                    f"https://host.example/{spelling}/private end",
                    secrets=("TOKEN",),
                )
                self.assertEqual("host.example/<redacted-path> end", result.text)
                self.assertEqual((), result.fingerprints)

    def test_standalone_literal_secret_renders_the_secret_marker(self):
        for mode, expected in (
            (NetworkUrlDisplay.REDACTED, "before <secret> after"),
            (NetworkUrlDisplay.HOST_PATH, "before <secret> after"),
        ):
            with self.subTest(mode=mode):
                result = _project(
                    "before TOKEN after", secrets=("TOKEN",), mode=mode
                )
                self.assertEqual(expected, result.text)

    def test_early_redacted_marker_is_irreversible(self):
        projector = DiagnosticProjector(
            (), url_display=NetworkUrlDisplay.REDACTED,
            url_identity=SessionUrlIdentity(),
        )
        self.assertEqual(("<redacted>",), projector.feed_text("https://ho"))
        # Later EOF/abort must not replace or duplicate the marker, and early
        # rendering must not authorize metadata.
        self.assertEqual((), projector.finish())
        self.assertEqual((), projector.hostnames)
        self.assertEqual((), projector.url_fingerprints)

    def test_markers_are_not_normalized_host_facts(self):
        # A masked host and a masked path must not appear in any fact list.
        result = _project(
            "https://TOKEN.example/TOKEN/private end", secrets=("TOKEN",)
        )
        self.assertEqual((), result.hostnames)
        self.assertEqual((), result.host_paths)
        self.assertNotIn(REDACTED_HOST, result.hostnames)
        self.assertNotIn(REDACTED_PATH_MARKER, result.hostnames)


class DecodedSecretScopeTests(unittest.TestCase):
    """4.3 - decoded-secret scope decides which facts survive."""

    def test_credential_only_secret_permits_safe_facts(self):
        result = _project(
            "https://user:TOKEN@host.example/public end", secrets=("TOKEN",)
        )
        self.assertEqual(("host.example",), result.hostnames)
        self.assertEqual(
            ("host.example/public",),
            tuple(fact.text for fact in result.host_paths),
        )
        self.assertEqual(1, len(result.fingerprints))

    def test_host_secret_suppresses_hostname_and_fingerprint(self):
        result = _project(
            "https://TOKEN.example/public end", secrets=("TOKEN",)
        )
        self.assertEqual((), result.hostnames)
        self.assertEqual((), result.fingerprints)

    def test_path_secret_suppresses_fingerprint_but_keeps_safe_hostname(self):
        result = _project(
            "https://host.example/TOKEN/private end", secrets=("TOKEN",)
        )
        self.assertEqual(("host.example",), result.hostnames)
        self.assertEqual(
            ("host.example/<redacted-path>",),
            tuple(fact.text for fact in result.host_paths),
        )
        self.assertEqual((), result.fingerprints)

    def test_decoded_secret_scope_matches_literal_scope(self):
        # A per-cent-decoded secret in the path has the same scope as its
        # literal spelling: hostname fact survives, fingerprint is suppressed.
        for spelling in ("TOKEN", "%54%4f%4b%45%4e"):
            with self.subTest(spelling=spelling):
                result = _project(
                    f"https://host.example/{spelling}/private end",
                    secrets=("TOKEN",),
                )
                self.assertEqual(("host.example",), result.hostnames)
                self.assertEqual((), result.fingerprints)


class FactGateRouteTests(unittest.TestCase):
    """4.6/4.7 - the same gates hold on every metadata route."""

    def test_clean_eof_applies_the_same_gates(self):
        cases = (
            ("https://TOKEN.example/public", ("TOKEN",), (), ()),
            ("https://host.example/TOKEN/private", ("TOKEN",),
             ("host.example",), ()),
            ("https://user:TOKEN@host.example/public", ("TOKEN",),
             ("host.example",), None),
            ("TOKENhttps://host.example/public", ("TOKEN",), (), ()),
        )
        for text, secrets, hosts, fingerprints in cases:
            with self.subTest(text=text):
                result = _project(text, secrets=secrets)
                self.assertEqual(hosts, result.hostnames)
                if fingerprints is not None:
                    self.assertEqual(len(fingerprints), len(result.fingerprints))

    def test_structured_route_applies_the_same_gates(self):
        host_secret = project_structured_diagnostic(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream.STDOUT,
            classification=HostDiagnosticClassification.STATUS,
            text="https://TOKEN.example/public",
            secrets=("TOKEN",),
        )
        self.assertEqual(REDACTED_URL, host_secret.text)
        self.assertEqual((), host_secret.hostnames)
        self.assertEqual((), host_secret.url_fingerprints)

        path_secret = project_structured_diagnostic(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream.STDOUT,
            classification=HostDiagnosticClassification.STATUS,
            text="https://host.example/TOKEN/private",
            secrets=("TOKEN",),
        )
        self.assertEqual(("host.example",), path_secret.hostnames)
        self.assertEqual((), path_secret.url_fingerprints)

    def test_host_acquisition_failure_route_applies_the_same_gates(self):
        resource = DiagnosticLogicalResource(
            DiagnosticResourceKind.REVIEWED_ARTIFACT, "rustup"
        )
        diagnostic = project_host_acquisition_failure(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            logical_resource=resource,
            reason=None,
            url="https://TOKEN.example/private",
            secrets=("TOKEN",),
        )
        self.assertIn(REDACTED_URL, diagnostic.text)
        self.assertNotIn("TOKEN.example", diagnostic.text)
        self.assertEqual((), diagnostic.hostnames)
        self.assertEqual((), diagnostic.url_fingerprints)

    def test_host_and_path_facts_are_delayed_until_settled(self):
        projector = DiagnosticProjector(
            (), url_display=NetworkUrlDisplay.HOST_PATH,
            url_identity=SessionUrlIdentity(),
        )
        self.assertEqual(("host.example",), projector.feed_text("https://host.example/pa"))
        # The host is a committed component, but the URL is not complete, so no
        # hostname or fingerprint fact is granted yet.
        self.assertEqual((), projector.hostnames)
        self.assertEqual((), projector.url_fingerprints)
        self.assertEqual(("/path",), projector.feed_text("th?query"))
        self.assertEqual((), projector.hostnames)
        self.assertEqual((), projector.url_fingerprints)
        self.assertEqual((" end",), projector.feed_text(" end"))
        self.assertEqual(("host.example",), projector.hostnames)
        self.assertEqual(1, len(projector.url_fingerprints))

    def test_no_fingerprint_from_secret_bearing_host_path_or_prefix(self):
        for text, secrets in (
            ("https://TOKEN.example/public end", ("TOKEN",)),
            ("https://host.example/TOKEN/private end", ("TOKEN",)),
            ("TOKENhttps://host.example/public end", ("TOKEN",)),
        ):
            with self.subTest(text=text):
                result = _project(text, secrets=secrets)
                self.assertEqual((), result.fingerprints)

    def test_facts_from_unrelated_urls_remain_available(self):
        result = _project(
            "https://TOKEN.example/private and https://safe.example/public end",
            secrets=("TOKEN",),
        )
        self.assertEqual(("safe.example",), result.hostnames)
        self.assertEqual(("safe.example/public",), tuple(
            fact.text for fact in result.host_paths
        ))
        self.assertEqual(1, len(result.fingerprints))


class CanonicalIdentityTests(unittest.TestCase):
    """4.7 - canonicalization, explicit ports, order and session isolation."""

    @staticmethod
    def _fingerprint(url, secrets=(), identity=None):
        identity = identity or SessionUrlIdentity()
        projector = DiagnosticProjector(
            secrets, url_identity=identity,
            url_display=NetworkUrlDisplay.REDACTED,
        )
        list(projector.feed_text(url + " "))
        projector.finish()
        return projector.url_fingerprints

    def test_normalized_host_is_canonical_in_the_fingerprint(self):
        identity = SessionUrlIdentity()
        for url in (
            "https://HOST.example/a",
            "https://host.example./a",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    self._fingerprint("https://host.example/a", identity=identity),
                    self._fingerprint(url, identity=identity),
                )

    def test_explicit_default_port_is_preserved(self):
        identity = SessionUrlIdentity()
        implicit = self._fingerprint("https://host.example/a", identity=identity)
        explicit = self._fingerprint(
            "https://host.example:443/a", identity=identity
        )
        self.assertNotEqual(implicit, explicit)

    def test_userinfo_query_and_fragment_do_not_change_the_fingerprint(self):
        identity = SessionUrlIdentity()
        plain = self._fingerprint("https://host.example/a", identity=identity)
        decorated = self._fingerprint(
            "https://user:pw@host.example/a?q=1#frag", identity=identity
        )
        self.assertEqual(plain, decorated)

    def test_fingerprint_order_and_multiplicity_are_preserved(self):
        text = (
            "https://host.example/a https://host.example/a "
            "https://host.example/b "
        )
        fingerprints = self._fingerprint(text)
        self.assertEqual(3, len(fingerprints))
        self.assertEqual(fingerprints[0], fingerprints[1])
        self.assertNotEqual(fingerprints[0], fingerprints[2])

    def test_fingerprints_do_not_correlate_across_sessions(self):
        left = self._fingerprint(
            "https://host.example/a", identity=SessionUrlIdentity()
        )
        right = self._fingerprint(
            "https://host.example/a", identity=SessionUrlIdentity()
        )
        self.assertNotEqual(left, right)

    def test_same_session_is_deterministic(self):
        identity = SessionUrlIdentity()
        self.assertEqual(
            self._fingerprint("https://host.example/a", identity=identity),
            self._fingerprint("https://host.example/a", identity=identity),
        )


class FetchRouteReconciliationTests(unittest.TestCase):
    """4.9 - the fetch grammar and shared validation are unchanged."""

    def test_unknown_scheme_fetch_line_is_recognized_without_facts(self):
        line = "npm http fetch GET 200 custom+transport://internal.example/x 15ms"
        record = parse_npm_fetch_line(line)
        self.assertIsNotNone(record)
        self.assertEqual((), sanitize_host_paths(record.url + " ", ()))

        recognizer = NpmFetchRecognizer(("TOKEN",))
        for character in line:
            recognizer.feed(character)
        safe = recognizer.finish()
        self.assertIsNotNone(safe)

    def test_secret_bearing_fetch_url_facts_match_the_projector(self):
        # A secret-bearing host has no normalized-host fact at all; a
        # secret-bearing path keeps the safe host but masks the path.  Both
        # scopes are identical to the normal projector route.
        cases = (
            ("https://TOKEN.example/public", ("TOKEN",), ()),
            ("https://host.example/TOKEN/private", ("TOKEN",),
             ("host.example/<redacted-path>",)),
            ("https://TOKEN.example/TOKEN/private", ("TOKEN",), ()),
        )
        for url, secrets, expected in cases:
            with self.subTest(url=url):
                record = parse_npm_fetch_line(
                    f"npm http fetch GET 200 {url} 15ms"
                )
                self.assertIsNotNone(record)
                facts = sanitize_host_paths(record.url + " ", secrets)
                self.assertEqual(expected, tuple(fact.text for fact in facts))

    def test_canonical_fetch_text_hides_secret_bearing_supported_urls(self):
        for url, secrets in (
            ("https://TOKEN.example/public", ("TOKEN",)),
            ("https://host.example/TOKEN/private", ("TOKEN",)),
        ):
            with self.subTest(url=url):
                record = parse_npm_fetch_line(
                    f"npm http fetch GET 200 {url} 15ms"
                )
                canonical = canonical_fetch_text(
                    record, NetworkUrlDisplay.REDACTED, secrets=secrets
                )
                self.assertIn(REDACTED_URL, canonical)
                self.assertNotIn("TOKEN", canonical)
                self.assertNotIn("host.example", canonical)


if __name__ == "__main__":
    unittest.main()
