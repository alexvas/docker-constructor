"""Phase 7 ephemeral URL identity and structured-diagnostic identity contracts.

These tests bind the RED deliverables for tasks 7.1-7.3 of
``improve-host-build-observability``: ordered, fixed-width, session-keyed URL
fingerprints that distinguish hidden resources without ever becoming a
rendered, retained, persisted, failure, evidence, or policy artifact, plus a
pure presentation identity that ignores normalized hostnames after the final
safe rendered text is formed.
"""
from __future__ import annotations

import hashlib
import inspect
import unittest

from docker.versioning.diagnostic_identity import (
    URL_FINGERPRINT_LENGTH,
    DiagnosticComparison,
    DiagnosticIdentity,
    SessionUrlIdentity,
    compare_diagnostics,
    identity_for,
)
from docker.versioning.diagnostic_projection import (
    project_structured_diagnostic,
    sanitize_diagnostic_text,
)
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
    HostStructuredDiagnostic,
)


def _project(text: str, *, session: SessionUrlIdentity, **overrides: object):
    values: dict[str, object] = {
        "phase": HostPhase.LOCKED_ASSEMBLY,
        "step": HostStep.NPM_EXECUTION,
        "stream": HostDiagnosticStream.STDOUT,
        "classification": HostDiagnosticClassification.STATUS,
        "text": text,
        "url_identity": session,
    }
    values.update(overrides)
    return project_structured_diagnostic(**values)  # type: ignore[arg-type]


def _fingerprints(session: SessionUrlIdentity, text: str) -> tuple[str, ...]:
    return _project(text, session=session).url_fingerprints


class TestUrlFingerprintIdentity(unittest.TestCase):
    def test_same_session_yields_same_identity_for_same_sanitized_url(self):
        session = SessionUrlIdentity()
        left = _fingerprints(session, "GET https://cache.example/pkg/a.tgz")
        right = _fingerprints(session, "fetched https://cache.example/pkg/a.tgz")
        self.assertEqual(1, len(left))
        self.assertEqual(left, right)

    def test_different_paths_are_distinct(self):
        session = SessionUrlIdentity()
        left = _fingerprints(session, "GET https://cache.example/pkg/a.tgz")
        right = _fingerprints(session, "GET https://cache.example/pkg/b.tgz")
        self.assertNotEqual(left, right)

    def test_explicit_port_presence_including_default_port_is_distinct(self):
        session = SessionUrlIdentity()
        implicit = _fingerprints(session, "GET https://cache.example/pkg/a.tgz")
        explicit_default = _fingerprints(
            session, "GET https://cache.example:443/pkg/a.tgz"
        )
        self.assertNotEqual(implicit, explicit_default)
        self.assertEqual(
            explicit_default,
            _fingerprints(session, "GET https://cache.example:443/pkg/a.tgz"),
        )

    def test_explicit_port_value_is_distinct(self):
        session = SessionUrlIdentity()
        left = _fingerprints(session, "GET https://cache.example:8443/pkg/a.tgz")
        right = _fingerprints(session, "GET https://cache.example:9443/pkg/a.tgz")
        self.assertNotEqual(left, right)

    def test_multiple_urls_preserve_order_and_multiplicity(self):
        session = SessionUrlIdentity()
        texts = [
            "GET https://one.example/a then https://two.example/b",
            "GET https://two.example/b then https://one.example/a",
            "GET https://one.example/a then https://one.example/a",
        ]
        first, reversed_order, duplicated = (
            _fingerprints(session, text) for text in texts
        )
        self.assertEqual(2, len(first))
        self.assertEqual(2, len(reversed_order))
        self.assertEqual(set(first), set(reversed_order))
        self.assertNotEqual(first, reversed_order)
        self.assertEqual(2, len(duplicated))
        self.assertEqual(duplicated[0], duplicated[1])

    def test_userinfo_query_and_fragment_do_not_contribute(self):
        session = SessionUrlIdentity()
        plain = _fingerprints(session, "GET https://cache.example/pkg/a.tgz")
        decorated = _fingerprints(
            session,
            "GET https://user:pass@cache.example/pkg/a.tgz?token=secret#frag",
        )
        self.assertEqual(plain, decorated)

    def test_configured_proxy_secret_yields_no_hostname_or_fingerprint(self):
        session = SessionUrlIdentity()
        proxy = "http://proxy.internal:8080"
        target = "https://registry.npmjs.org/pkg"
        diagnostic = _project(
            f"proxy {proxy} fetched {target}",
            session=session,
            secrets=(proxy,),
        )
        self.assertEqual("proxy <redacted> fetched <redacted>", diagnostic.text)
        self.assertNotIn(proxy, diagnostic.text)
        self.assertNotIn(target, diagnostic.text)
        self.assertNotIn("proxy.internal", diagnostic.hostnames)
        self.assertEqual(("registry.npmjs.org",), diagnostic.hostnames)
        self.assertEqual(1, len(diagnostic.url_fingerprints))
        independent = _project(target, session=session)
        self.assertEqual(
            independent.url_fingerprints, diagnostic.url_fingerprints
        )

    def test_malformed_candidates_do_not_emit_a_fingerprint(self):
        session = SessionUrlIdentity()
        self.assertEqual((), _fingerprints(session, "not a url at all"))
        self.assertEqual((), _fingerprints(session, "GET https:// ok"))


class TestFingerprintConfidentiality(unittest.TestCase):
    def test_fingerprint_is_a_fixed_width_opaque_value(self):
        session = SessionUrlIdentity()
        fingerprints = _fingerprints(session, "GET https://cache.example/a")
        self.assertEqual(1, len(fingerprints))
        value = fingerprints[0]
        self.assertEqual(URL_FINGERPRINT_LENGTH, len(value))
        self.assertTrue(all(character in "0123456789abcdef" for character in value))

    def test_fingerprint_is_not_an_unkeyed_fast_or_stable_hash(self):
        session = SessionUrlIdentity()
        url = "https://cache.example/private/artifact.tgz"
        fingerprint = session.fingerprint(url)
        for algorithm in ("md5", "sha1", "sha256", "blake2b", "blake2s"):
            digest = hashlib.new(algorithm, url.encode("utf-8")).hexdigest()
            self.assertNotEqual(fingerprint, digest, algorithm)
        self.assertNotIn(url, fingerprint)

    def test_fresh_random_key_per_session_prevents_cross_session_correlation(self):
        first = SessionUrlIdentity()
        second = SessionUrlIdentity()
        url = "https://cache.example/private/artifact.tgz"
        self.assertNotEqual(first.fingerprint(url), second.fingerprint(url))

    def test_explicit_test_key_is_deterministic_and_key_dependent(self):
        key = b"0123456789abcdef0123456789abcdef"
        other = b"fedcba9876543210fedcba9876543210"
        url = "https://cache.example/a"
        self.assertEqual(
            SessionUrlIdentity(key).fingerprint(url),
            SessionUrlIdentity(key).fingerprint(url),
        )
        self.assertNotEqual(
            SessionUrlIdentity(key).fingerprint(url),
            SessionUrlIdentity(other).fingerprint(url),
        )

    def test_key_never_appears_in_repr(self):
        key = b"0123456789abcdef0123456789abcdef"
        session = SessionUrlIdentity(key)
        rendered = repr(session)
        self.assertNotIn(key.hex(), rendered)
        self.assertNotIn("0123456789abcdef", rendered)

    def test_fingerprint_never_enters_rendered_text_or_retained_tail(self):
        session = SessionUrlIdentity()
        diagnostic = _project(
            "GET https://user:pass@cache.example/private/artifact.tgz?token=1",
            session=session,
        )
        self.assertEqual(1, len(diagnostic.url_fingerprints))
        fingerprint = diagnostic.url_fingerprints[0]
        self.assertNotIn(fingerprint, diagnostic.text)
        retained, hostnames = sanitize_diagnostic_text(
            "GET https://user:pass@cache.example/private/artifact.tgz?token=1"
        )
        self.assertNotIn(fingerprint, retained)
        self.assertNotIn(fingerprint, "".join(hostnames))

    def test_fingerprint_derivation_takes_no_output_policy(self):
        self.assertNotIn(
            "output", set(inspect.signature(SessionUrlIdentity.fingerprint).parameters)
        )
        self.assertNotIn(
            "hostname_display",
            set(inspect.signature(project_structured_diagnostic).parameters),
        )


class TestStructuredDiagnosticIdentity(unittest.TestCase):
    def _diagnostic(self, **overrides: object) -> HostStructuredDiagnostic:
        values: dict[str, object] = {
            "phase": HostPhase.LOCKED_ASSEMBLY,
            "step": HostStep.NPM_EXECUTION,
            "stream": HostDiagnosticStream.STDOUT,
            "classification": HostDiagnosticClassification.STATUS,
            "text": "added 1 package",
        }
        values.update(overrides)
        return HostStructuredDiagnostic(**values)  # type: ignore[arg-type]

    def test_identity_carries_every_presentation_component(self):
        identity = identity_for(self._diagnostic(url_fingerprints=("a" * 64,)))
        self.assertIs(HostPhase.LOCKED_ASSEMBLY, identity.phase)
        self.assertIs(HostStep.NPM_EXECUTION, identity.step)
        self.assertIs(HostDiagnosticStream.STDOUT, identity.stream)
        self.assertIs(HostDiagnosticClassification.STATUS, identity.classification)
        self.assertIsNone(identity.logical_resource)
        self.assertEqual("added 1 package", identity.text)
        self.assertEqual(("a" * 64,), identity.url_fingerprints)

    def test_same_identity_is_an_exact_repeat(self):
        left = identity_for(self._diagnostic())
        right = identity_for(self._diagnostic())
        self.assertEqual(DiagnosticComparison.EXACT_REPEAT, compare_diagnostics(left, right))

    def test_different_hidden_url_identity_forms_a_different_group(self):
        left = identity_for(
            self._diagnostic(text="GET <redacted>", url_fingerprints=("a" * 64,))
        )
        right = identity_for(
            self._diagnostic(text="GET <redacted>", url_fingerprints=("b" * 64,))
        )
        self.assertEqual(left.text, right.text)
        self.assertEqual(DiagnosticComparison.DIFFERENT, compare_diagnostics(left, right))

    def test_normalized_hosts_are_not_a_separate_coalescing_key(self):
        left = identity_for(
            self._diagnostic(hostnames=("one.example",), url_fingerprints=("a" * 64,))
        )
        right = identity_for(
            self._diagnostic(hostnames=("two.example",), url_fingerprints=("a" * 64,))
        )
        self.assertEqual(left, right)
        self.assertEqual(DiagnosticComparison.EXACT_REPEAT, compare_diagnostics(left, right))

    def test_phase_step_stream_classification_and_resource_partition_groups(self):
        base = self._diagnostic()
        variations = (
            self._diagnostic(phase=HostPhase.DERIVED_VALIDATION),
            self._diagnostic(step=HostStep.PUBLICATION),
            self._diagnostic(stream=HostDiagnosticStream.STDERR),
            self._diagnostic(classification=HostDiagnosticClassification.ERROR),
            self._diagnostic(
                logical_resource="npm-assembler-0123456789abcdef"
            ),
        )
        base_identity = identity_for(base)
        for variant_diagnostic in variations:
            with self.subTest(variant=variant_diagnostic):
                self.assertEqual(
                    DiagnosticComparison.DIFFERENT,
                    compare_diagnostics(base_identity, identity_for(variant_diagnostic)),
                )

    def test_identity_excludes_hostnames_from_its_public_surface(self):
        fields = {field.name for field in DiagnosticIdentity.__dataclass_fields__.values()}
        self.assertNotIn("hostnames", fields)
        self.assertEqual("added 1 package", identity_for(self._diagnostic()).text)

    def test_text_identity_is_pure_and_does_not_accept_output_policy(self):
        self.assertFalse(hasattr(DiagnosticIdentity, "render"))
        self.assertEqual(
            DiagnosticComparison.EXACT_REPEAT,
            compare_diagnostics(
                identity_for(self._diagnostic()),
                identity_for(self._diagnostic()),
            ),
        )


class TestUrlFingerprintBoundary(unittest.TestCase):
    """The structured DTO boundary enforces fixed-width lowercase hex."""

    FIXED = "a" * 64

    def _diagnostic(self, fingerprints: object) -> HostStructuredDiagnostic:
        return HostStructuredDiagnostic(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream.STDOUT,
            classification=HostDiagnosticClassification.STATUS,
            text="GET <redacted>",
            url_fingerprints=fingerprints,  # type: ignore[arg-type]
        )

    def _identity(self, fingerprints: object) -> DiagnosticIdentity:
        return DiagnosticIdentity(
            phase=HostPhase.LOCKED_ASSEMBLY,
            step=HostStep.NPM_EXECUTION,
            stream=HostDiagnosticStream.STDOUT,
            classification=HostDiagnosticClassification.STATUS,
            text="GET <redacted>",
            url_fingerprints=fingerprints,  # type: ignore[arg-type]
        )

    def test_both_dtos_accept_fixed_width_lowercase_hex(self):
        self.assertEqual((self.FIXED,), self._diagnostic((self.FIXED,)).url_fingerprints)
        self.assertEqual((self.FIXED,), self._identity((self.FIXED,)).url_fingerprints)
        self.assertEqual((), self._diagnostic(()).url_fingerprints)
        self.assertEqual((), self._identity(()).url_fingerprints)

    def test_both_dtos_reject_wrong_length_fingerprints(self):
        for malformed in ("a" * 63, "a" * 65):
            with self.subTest(fingerprint=malformed):
                with self.assertRaises(ValueError):
                    self._diagnostic((malformed,))
                with self.assertRaises(ValueError):
                    self._identity((malformed,))

    def test_both_dtos_reject_uppercase_or_non_hex_fingerprints(self):
        for malformed in ("A" * 64, "g" * 64, "0" * 63 + "z"):
            with self.subTest(fingerprint=malformed):
                with self.assertRaises(ValueError):
                    self._diagnostic((malformed,))
                with self.assertRaises(ValueError):
                    self._identity((malformed,))

    def test_both_dtos_reject_arbitrary_text(self):
        for malformed in ("SECRET URL HERE", "", "a" * 63 + "!"):
            with self.subTest(fingerprint=malformed):
                with self.assertRaises(ValueError):
                    self._diagnostic((malformed,))
                with self.assertRaises(ValueError):
                    self._identity((malformed,))

    def test_both_dtos_reject_non_string_members(self):
        for malformed in (b"a" * 64, None, 1234, 1.5):
            with self.subTest(fingerprint=malformed):
                with self.assertRaises(TypeError):
                    self._diagnostic((malformed,))
                with self.assertRaises(TypeError):
                    self._identity((malformed,))

    def test_both_dtos_reject_a_list_instead_of_a_tuple(self):
        with self.assertRaises(TypeError):
            self._diagnostic([self.FIXED])
        with self.assertRaises(TypeError):
            self._identity([self.FIXED])

    def test_invalid_fingerprints_are_never_silently_normalized(self):
        with self.assertRaises(ValueError):
            self._diagnostic(("A" * 64,))
        with self.assertRaises(ValueError):
            self._identity((self.FIXED.upper(),))
        with self.assertRaises(ValueError):
            self._diagnostic((self.FIXED[:-1],))
        with self.assertRaises(ValueError):
            self._identity((self.FIXED + "a",))


if __name__ == "__main__":
    unittest.main()
