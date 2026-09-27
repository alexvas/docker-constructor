"""Phase 3 safe host/path projection contracts.

Binds tasks 3.1-3.8 of ``add-configurable-network-url-display``.  The safe
projector derives a bounded, internal host/path presentation fact -- a
normalized hostname plus a canonical safe encoded path, or the fixed
``<redacted-path>`` fallback -- while it:

* preserves the existing secret/URL confidentiality exclusions;
* keeps the fact out of the external SDK DTO, verification/assembler evidence,
  persistence, and semantic identities; and
* preserves the existing path-sensitive but opaque session URL fingerprint
  identity.

The fact is produced by the projector only.  Binding it into a selected live
or retained representation is Phase 6 work; here it must stay internal and
bounded.
"""
from __future__ import annotations

import dataclasses
import inspect
import unicodedata
import unittest

from docker.npm_environment.streaming import StreamChunk
from docker.versioning.diagnostic_identity import SessionUrlIdentity
from docker.versioning.diagnostic_projection import (
    INCOMPLETE_TOKEN_MARKER,
    OVERSIZED_TOKEN_MARKER,
    PENDING_LIMIT_BYTES,
    REDACTED_PATH_MARKER,
    DiagnosticProjector,
    SafeHostPath,
    normalized_url_host,
    project_structured_diagnostic,
    sanitize_diagnostic_text,
    sanitize_host_paths,
)
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticPrefix,
    HostDiagnosticStream,
    HostFailureContext,
    HostPhase,
    HostStep,
    HostStructuredDiagnostic,
)
from docker.versioning.npm_diagnostic_stream import NpmDiagnosticStream


def _facts(text: str, secrets=()) -> tuple[SafeHostPath, ...]:
    return sanitize_host_paths(text, secrets)


def _structured(text: str, *, session: SessionUrlIdentity | None = None):
    return project_structured_diagnostic(
        phase=HostPhase.LOCKED_ASSEMBLY,
        step=HostStep.NPM_EXECUTION,
        stream=HostDiagnosticStream.STDOUT,
        classification=HostDiagnosticClassification.STATUS,
        text=text,
        url_identity=session,
    )


class TestSafeHostPathProjection(unittest.TestCase):
    """Task 3.1: normalized hostnames and canonical encoded paths."""

    def test_normalized_hostname_and_canonical_encoded_path(self):
        facts = _facts(
            "npm http fetch GET 200 "
            "https://ExAmPlE.com:8443/A/b%2Fc?q=1#frag 15ms"
        )
        self.assertEqual(1, len(facts))
        self.assertEqual("example.com", facts[0].hostname)
        # Reserved ``%2F`` stays encoded; the host is lowercased and the
        # explicit port, query, and fragment are excluded.
        self.assertEqual("/A/b%2Fc", facts[0].path)
        self.assertEqual("example.com/A/b%2Fc", facts[0].text)

    def test_scoped_package_path_is_preserved(self):
        url = "https://registry.npmjs.org/@scope/pkg/-/pkg-1.0.0.tgz"
        facts = _facts(f"npm http fetch GET 200 {url} 15ms (cache miss)")
        self.assertEqual(
            [SafeHostPath("registry.npmjs.org", "/@scope/pkg/-/pkg-1.0.0.tgz")],
            list(facts),
        )
        self.assertEqual(
            "registry.npmjs.org/@scope/pkg/-/pkg-1.0.0.tgz", facts[0].text
        )

    def test_facts_are_ordered_and_deduplicated_within_the_window(self):
        text = (
            "https://one.example/a then https://two.example/b "
            "then https://one.example/a"
        )
        self.assertEqual(
            [
                SafeHostPath("one.example", "/a"),
                SafeHostPath("two.example", "/b"),
            ],
            list(_facts(text)),
        )

    def test_idn_ipv4_and_ipv6_hosts_normalize_the_same_way(self):
        cases = {
            "https://b\u00fccher.example/x": ("xn--bcher-kva.example", "/x"),
            "https://192.0.2.10:443/x": ("192.0.2.10", "/x"),
            "https://[2001:DB8::1]:443/x": ("2001:db8::1", "/x"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                facts = _facts(f"see {url} now")
                self.assertEqual((expected[0], expected[1]), (
                    facts[0].hostname, facts[0].path,
                ))

    def test_authority_only_url_has_a_root_path(self):
        facts = _facts("see https://example.com now")
        self.assertEqual([SafeHostPath("example.com", "/")], list(facts))
        self.assertEqual("example.com/", facts[0].text)

    def test_non_url_text_yields_no_facts(self):
        self.assertEqual((), _facts("added 3 packages in 2s"))
        self.assertEqual((), _facts("token=topsecret", secrets=("topsecret",)))


class TestNormalizedUrlHostHelper(unittest.TestCase):
    """The shared authority/hostname helper also used by the fetch parser.

    It must apply the projector's authority validation, not just hostname
    normalization, so a structurally malformed authority cannot yield a host
    that host/path projection refuses to emit.
    """

    def test_malformed_authorities_return_no_host(self):
        for url in (
            "https://example.com:/x",
            "https://example.com:",
            "https://[2001:db8::1]:/x",
            "https://user@example.com:/x",
            "https://%",
            "https://[",
            "http://:80/x",
        ):
            with self.subTest(url=url):
                self.assertIsNone(normalized_url_host(url))
                self.assertEqual((), _facts(url))

    def test_nonnumeric_and_out_of_range_ports_return_no_host(self):
        for url in (
            "http://127.0.0.1:0x50/x",
            "http://127.0.0.1:-1/x",
            "http://127.0.0.1:70000/x",
            "http://127.0.0.1:99999/x",
            "http://registry.npmjs.org:65536/a",
        ):
            with self.subTest(url=url):
                self.assertIsNone(normalized_url_host(url))
                self.assertEqual((), _facts(url))

    def test_encoded_malformed_authorities_return_no_host(self):
        for url in (
            "https://example.com%3A/x",
            "https://example.com%3Aabc/x",
            "https://example.com%3A99999/x",
            "https://example.com%3A0x50/x",
            "https://user@example.com%3A/x",
        ):
            with self.subTest(url=url):
                self.assertIsNone(normalized_url_host(url))
                self.assertEqual((), _facts(url))

    def test_encoded_host_uses_the_validated_decoded_layer(self):
        url = "https://%65xample.com/x"
        self.assertEqual("example.com", normalized_url_host(url))
        self.assertEqual(
            [SafeHostPath("example.com", "/x")], list(_facts(url))
        )

    def test_supported_host_forms_still_normalize(self):
        cases = {
            "https://example.com/x": "example.com",
            "HTTPS://EXAMPLE.COM/x": "example.com",
            "https://example.com./x": "example.com",
            "https://example.com:443/x": "example.com",
            "https://192.0.2.10:443/x": "192.0.2.10",
            "https://[2001:DB8::1]:443/x": "2001:db8::1",
            "https://xn--bcher-kva.example/x": "xn--bcher-kva.example",
            "https://b\u00fccher.example/x": "xn--bcher-kva.example",
            "https://%65xample.com/x": "example.com",
            "https://a_b.example/x": "a_b.example",
            "https://user:pw@example.com:443/x?q=1#f": "example.com",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(expected, normalized_url_host(url))


class TestConfidentialityPreserved(unittest.TestCase):
    """Task 3.2: existing exclusions stay intact and the safe path appears."""

    def test_every_url_component_stays_excluded_from_text_and_path(self):
        text = (
            "download https://alice:s3cr3t@ExAmPlE.com:8443/a/b"
            "?q=1&sig=dead#frag retry"
        )
        safe, hostnames = sanitize_diagnostic_text(text, secrets=("s3cr3t",))
        self.assertEqual("download <redacted> retry", safe)
        self.assertEqual(("example.com",), hostnames)

        facts = _facts(text, secrets=("s3cr3t",))
        self.assertEqual("example.com", facts[0].hostname)
        self.assertEqual("/a/b", facts[0].path)
        for leaked in (
            "alice",
            "s3cr3t",
            "ExAmPlE.com",
            "8443",
            "q=1",
            "dead",
            "frag",
        ):
            self.assertNotIn(leaked, facts[0].text)

    def test_proxy_endpoint_secret_suppresses_the_whole_host_path_fact(self):
        proxy = "http://proxy.corp.example:3128"
        self.assertEqual(
            (),
            _facts(f"npm retry via {proxy} failed", secrets=(proxy,)),
        )

    def test_registered_host_secret_suppresses_the_host_path_fact(self):
        facts = _facts(
            "see https://secret.example/private/artifact.tgz now",
            secrets=("secret.example",),
        )
        self.assertEqual((), facts)

    def test_bare_secret_stays_redacted_without_host_path_facts(self):
        safe, hostnames = sanitize_diagnostic_text(
            "token=topsecret", secrets=("topsecret",)
        )
        self.assertEqual("token=<redacted>", safe)
        self.assertEqual((), hostnames)
        self.assertEqual((), _facts("token=topsecret", secrets=("topsecret",)))


class TestHostPathBoundaries(unittest.TestCase):
    """Task 3.3: percent-encoded controls, unsafe paths, and token bounds."""

    def test_percent_encoded_controls_stay_encoded_and_inert(self):
        facts = _facts("see https://example.com/a%0a%1b%7fb now")
        self.assertEqual("example.com", facts[0].hostname)
        # Never decoded into active terminal text; hex is uppercased.
        self.assertEqual("/a%0A%1B%7Fb", facts[0].path)
        self.assertNotIn("\n", facts[0].text)
        self.assertNotIn("\x1b", facts[0].text)

    def test_unreserved_percent_encoding_is_canonicalized(self):
        facts = _facts("see https://example.com/%41%5f~%2db now")
        # ``%41`` -> ``A``, ``%5f`` -> ``_``, ``%2d`` -> ``-``; ``~`` stays.
        self.assertEqual("/A_~-b", facts[0].path)

    def test_raw_bidi_control_is_percent_encoded_not_active(self):
        facts = _facts("see https://example.com/\u202eabc now")
        self.assertEqual("example.com", facts[0].hostname)
        self.assertEqual("/%E2%80%AEabc", facts[0].path)
        self.assertNotIn("\u202e", facts[0].path)
        self.assertNotIn("\u202e", facts[0].text)

    def test_raw_non_ascii_path_is_utf8_percent_encoded(self):
        facts = _facts("see https://example.com/caf\u00e9 now")
        self.assertEqual("example.com", facts[0].hostname)
        # ``é`` must become ``%C3%A9`` (uppercase hex), never a literal.
        self.assertEqual("/caf%C3%A9", facts[0].path)
        self.assertEqual("example.com/caf%C3%A9", facts[0].text)
        self.assertTrue(facts[0].text.isascii())

    def test_other_unicode_format_controls_are_encoded(self):
        samples = (
            "\u200b",  # zero-width space
            "\u200e",  # left-to-right mark
            "\u2066",  # left-to-right isolate
            "\u00ad",  # soft hyphen
            "\ufeff",  # zero-width no-break space / BOM
            "\u2028",  # line separator
        )
        for sample in samples:
            with self.subTest(code=hex(ord(sample))):
                facts = _facts(f"see https://example.com/a{sample}b now")
                self.assertEqual("example.com", facts[0].hostname)
                self.assertTrue(facts[0].path.isascii())
                self.assertNotIn(sample, facts[0].path)
                self.assertNotIn(sample, facts[0].text)
                expected = "".join(
                    f"%{byte:02X}" for byte in sample.encode("utf-8")
                )
                self.assertIn(expected, facts[0].path)

    def test_rendered_host_path_has_no_active_unicode_controls(self):
        text = "GET https://example.com/a\u202eb/caf\u00e9/\u200b%2E%2E/z 15ms"
        facts = _facts(text)
        self.assertEqual(1, len(facts))
        for rendered in (facts[0].path, facts[0].text):
            self.assertTrue(rendered.isascii())
            self.assertFalse(
                any(
                    unicodedata.category(character)[0] == "C"
                    for character in rendered
                )
            )

    def test_safe_host_path_rejects_active_non_ascii_paths(self):
        for unsafe in ("/\u202eabc", "/caf\u00e9", "/a\x00b", "/a\x7fb"):
            with self.subTest(path=unsafe):
                with self.assertRaises(ValueError):
                    SafeHostPath("example.com", unsafe)

    def test_dot_segments_are_removed_from_the_canonical_path(self):
        facts = _facts("see https://example.com/a/../b/./c now")
        self.assertEqual("/b/c", facts[0].path)

    def test_malformed_percent_escape_falls_back_to_the_marker(self):
        facts = _facts("see https://example.com/a%zz now")
        self.assertEqual("example.com", facts[0].hostname)
        self.assertEqual(REDACTED_PATH_MARKER, facts[0].path)
        self.assertEqual("example.com/<redacted-path>", facts[0].text)
        self.assertNotIn("%zz", facts[0].text)

    def test_secret_bearing_path_falls_back_but_keeps_the_safe_host(self):
        facts = _facts(
            "see https://example.com/s3cr3t/v1 now", secrets=("s3cr3t",)
        )
        self.assertEqual("example.com", facts[0].hostname)
        self.assertEqual(REDACTED_PATH_MARKER, facts[0].path)
        self.assertNotIn("s3cr3t", facts[0].text)

    def test_encoded_secret_bearing_path_falls_back(self):
        facts = _facts(
            "see https://example.com/%73ecret/v1 now", secrets=("secret",)
        )
        self.assertEqual(REDACTED_PATH_MARKER, facts[0].path)

    def test_canonicalization_cannot_create_a_secret(self):
        # Dot-segment removal turns ``/sec/x/../ret`` into ``/sec/ret``, a
        # registered secret that appears in neither the original path nor any
        # percent-decoded layer.  Confidentiality is rechecked after canonical
        # form, so the path fails closed while the safe host stays visible.
        facts = _facts(
            "see https://example.com/sec/x/../ret now", secrets=("sec/ret",)
        )
        self.assertEqual("example.com", facts[0].hostname)
        self.assertEqual(REDACTED_PATH_MARKER, facts[0].path)
        self.assertEqual("example.com/<redacted-path>", facts[0].text)
        self.assertNotIn("sec/ret", facts[0].text)

    def test_canonicalization_cannot_erase_a_secret(self):
        # The original-path check is retained: ``/sec/ret/x/..`` canonicalizes
        # to ``/sec/ret`` only by removing a segment, but the registered secret
        # is already visible in the original path and must still redact.
        facts = _facts(
            "see https://example.com/sec/ret/x/.. now", secrets=("sec/ret",)
        )
        self.assertEqual("example.com", facts[0].hostname)
        self.assertEqual(REDACTED_PATH_MARKER, facts[0].path)
        self.assertNotIn("sec/ret", facts[0].text)

    def test_unrelated_path_is_not_redacted(self):
        facts = _facts("see https://example.com/public/v1 now")
        self.assertEqual("/public/v1", facts[0].path)

    def test_incomplete_url_token_yields_no_host_path_fact(self):
        projector = DiagnosticProjector()
        list(projector.feed_text("see https://example.com/a%2"))
        self.assertIn(INCOMPLETE_TOKEN_MARKER, projector.finish())
        self.assertEqual((), projector.host_paths)

    def test_oversized_url_token_yields_no_host_path_fact(self):
        projector = DiagnosticProjector()
        produced = list(
            projector.feed_text(
                "https://example.com/" + "a" * (PENDING_LIMIT_BYTES + 32)
            )
        )
        self.assertIn(OVERSIZED_TOKEN_MARKER, produced)
        projector.finish()
        self.assertEqual((), projector.host_paths)

    def test_path_at_the_token_bound_is_retained(self):
        path = "/" + "a" * 4096
        facts = _facts(f"see https://example.com{path} now")
        self.assertEqual(path, facts[0].path)


class TestHostPathApiShape(unittest.TestCase):
    """Task 3.4: the internal fact never widens a public/evidence boundary."""

    def test_structured_diagnostic_has_no_host_path_field(self):
        names = {field.name for field in dataclasses.fields(HostStructuredDiagnostic)}
        self.assertNotIn("host_paths", names)
        self.assertNotIn("host_path", names)
        self.assertNotIn("path", names)

    def test_stream_chunk_has_no_host_path_field(self):
        names = {field.name for field in dataclasses.fields(StreamChunk)}
        self.assertNotIn("host_paths", names)
        self.assertNotIn("host_path", names)

    def test_projection_apis_take_no_host_path_or_policy_input(self):
        for callable_ in (
            project_structured_diagnostic,
            sanitize_diagnostic_text,
            sanitize_host_paths,
        ):
            with self.subTest(callable=callable_.__name__):
                parameters = set(inspect.signature(callable_).parameters)
                self.assertFalse(
                    parameters
                    & {"host_paths", "host_path", "network_url_display", "output"}
                )

    def test_url_fingerprints_remain_path_sensitive_within_one_session(self):
        session = SessionUrlIdentity()
        left = _structured("GET https://cache.example/pkg/a.tgz", session=session)
        right = _structured("GET https://cache.example/pkg/b.tgz", session=session)
        self.assertEqual(1, len(left.url_fingerprints))
        self.assertEqual(1, len(right.url_fingerprints))
        self.assertNotEqual(left.url_fingerprints, right.url_fingerprints)

    def test_url_fingerprint_never_exposes_the_plaintext_path(self):
        session = SessionUrlIdentity()
        diagnostic = _structured(
            "GET https://cache.example/private/artifact.tgz", session=session
        )
        fingerprint = diagnostic.url_fingerprints[0]
        self.assertNotIn("private", fingerprint)
        self.assertNotIn("artifact.tgz", fingerprint)
        self.assertNotIn(fingerprint, diagnostic.text)


class TestInternalOnlyBoundary(unittest.TestCase):
    """Task 3.7: collected host/path facts stay bounded and internal."""

    def test_collector_drains_host_path_facts_after_each_line(self):
        stream = NpmDiagnosticStream("stdout")
        chunks = stream.feed_bytes(
            b"GET https://registry.example.com/@scope/pkg/-/p.tgz 15ms\n"
        )
        self.assertTrue(any(chunk.finalized for chunk in chunks))
        self.assertEqual((), stream._projector.host_paths)

    def test_collected_chunks_carry_no_host_path_payload(self):
        stream = NpmDiagnosticStream("stdout")
        chunks = list(
            stream.feed_bytes(
                b"GET https://registry.example.com/a/b 15ms\n"
            )
        )
        chunks.extend(stream.finish())
        for chunk in chunks:
            self.assertFalse(hasattr(chunk, "host_paths"))
            self.assertFalse(hasattr(chunk, "path"))

    def test_structured_diagnostic_never_carries_the_fact(self):
        diagnostic = _structured("GET https://registry.example.com/a/b 15ms")
        self.assertFalse(hasattr(diagnostic, "host_paths"))
        self.assertFalse(hasattr(diagnostic, "path"))

    def test_every_host_event_dto_omits_a_host_path_field(self):
        for dto in (
            HostStructuredDiagnostic,
            HostDiagnosticPrefix,
            HostFailureContext,
        ):
            with self.subTest(dto=dto.__name__):
                names = {field.name for field in dataclasses.fields(dto)}
                self.assertFalse(names & {"host_paths", "host_path", "path"})

    def test_host_event_module_does_not_export_the_internal_fact(self):
        import docker.versioning.host_progress as host_progress

        self.assertFalse(hasattr(host_progress, "SafeHostPath"))

    def test_exception_projection_emits_only_type_names(self):
        from docker.versioning.diagnostic_projection import (
            project_exception_type_chain,
        )

        class _PrivateTransportError(Exception):
            pass

        chain = project_exception_type_chain(
            _PrivateTransportError("https://user:pass@cache.example/private")
        )
        self.assertEqual(("_PrivateTransportError",), chain)
        self.assertNotIn("private", chain[0])


if __name__ == "__main__":
    unittest.main()
