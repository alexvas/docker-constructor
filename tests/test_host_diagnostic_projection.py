"""Phase 2 safe diagnostic projection contracts.

These tests bind the RED deliverables for Phase 2 of
``improve-host-build-observability``: one pure, output-policy-independent
projector that validates logical host-resource context, applies existing
secret redaction before boundary-safe URL sanitization with bounded pending
state and fixed fail-closed markers, separates normalized hostname facts, and
projects deterministic bounded exception-type chains.
"""
from __future__ import annotations

import inspect
import unittest
import urllib.error
import urllib.parse

from docker.npm_environment.streaming import REDACTED
from docker.versioning.diagnostic_projection import (
    EXCEPTION_TYPE_LIMIT,
    INCOMPLETE_TOKEN_MARKER,
    OVERSIZED_TOKEN_MARKER,
    PENDING_LIMIT_BYTES,
    PI_RELEASE_ASSET_NAMES,
    URL_DECODE_LAYER_LIMIT,
    DiagnosticLogicalResource,
    DiagnosticProjector,
    DiagnosticResourceKind,
    project_exception_type_chain,
    project_structured_diagnostic,
    sanitize_diagnostic_text,
)
from docker.versioning.host_progress import (
    HostDiagnosticClassification,
    HostDiagnosticStream,
    HostPhase,
    HostStep,
)
from docker.versioning.pi_release import (
    INSTALL_PACKAGE_FILENAME,
    INSTALL_PACKAGE_LOCK_FILENAME,
    SHA256SUMS_FILENAME,
)


class _MessageBombError(Exception):
    """Exception whose message evaluation fails loudly if ever attempted."""

    def __str__(self) -> str:  # noqa: D105 - deliberately unavailable
        raise AssertionError("exception message must never be evaluated")


class _HostileRelationshipError(Exception):
    """Exception that raises when one named relationship attribute is read."""

    _RELATIONSHIPS = frozenset({"reason", "__cause__", "__context__"})

    def __init__(self, hostile: str) -> None:
        super().__init__("hostile message must never be evaluated")
        object.__setattr__(self, "_hostile", hostile)

    def __getattribute__(self, name: str):
        if name in _HostileRelationshipError._RELATIONSHIPS:
            if name == object.__getattribute__(self, "_hostile"):
                raise RuntimeError("hostile relationship access")
        return object.__getattribute__(self, name)

    def __str__(self) -> str:  # noqa: D105 - deliberately unavailable
        raise AssertionError("exception message must never be evaluated")


def _project(text: str, **overrides: object):
    values: dict[str, object] = {
        "phase": HostPhase.RELEASE_ACQUISITION,
        "step": HostStep.ARTIFACT_ACQUISITION,
        "stream": HostDiagnosticStream.STDERR,
        "classification": HostDiagnosticClassification.RETRY,
        "text": text,
    }
    values.update(overrides)
    return project_structured_diagnostic(**values)  # type: ignore[arg-type]


class _DrainEntryProbe(DiagnosticProjector):
    """Projector that records retained bytes at every drain entry.

    Drain entry is the maximal point of the append-then-drain cycle, so this
    probe exposes any transient over-limit retention that a whole-feed call
    would otherwise hide.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.drain_entry_bytes: list[int] = []

    def _drain(self) -> list[str]:
        self.drain_entry_bytes.append(len(self._pending.encode("utf-8")))
        return super()._drain()


class TestLogicalResourceContext(unittest.TestCase):
    def test_reviewed_artifact_names_are_the_closed_supported_set(self):
        for name in ("rustup", "uv", "rtk", "fd"):
            with self.subTest(name=name):
                resource = DiagnosticLogicalResource(
                    DiagnosticResourceKind.REVIEWED_ARTIFACT, name
                )
                self.assertEqual(name, resource.name)
                self.assertIs(DiagnosticResourceKind.REVIEWED_ARTIFACT, resource.kind)

    def test_pi_release_asset_names_are_the_closed_supported_set(self):
        for name in (
            SHA256SUMS_FILENAME,
            INSTALL_PACKAGE_FILENAME,
            INSTALL_PACKAGE_LOCK_FILENAME,
        ):
            with self.subTest(name=name):
                resource = DiagnosticLogicalResource(
                    DiagnosticResourceKind.PI_RELEASE_ASSET, name
                )
                self.assertEqual(name, resource.name)

    def test_pi_release_name_set_tracks_authoritative_module(self):
        self.assertEqual(
            frozenset(
                (
                    SHA256SUMS_FILENAME,
                    INSTALL_PACKAGE_FILENAME,
                    INSTALL_PACKAGE_LOCK_FILENAME,
                )
            ),
            PI_RELEASE_ASSET_NAMES,
        )

    def test_assembler_container_requires_a_hex_digest_prefix(self):
        resource = DiagnosticLogicalResource(
            DiagnosticResourceKind.ASSEMBLER_CONTAINER,
            "npm-assembler-0123456789abcdef",
        )
        self.assertEqual("npm-assembler-0123456789abcdef", resource.name)

    def test_rejects_arbitrary_resource_labels(self):
        rejected = (
            (DiagnosticResourceKind.REVIEWED_ARTIFACT, "definitely-not-an-artifact"),
            (DiagnosticResourceKind.REVIEWED_ARTIFACT, "../../etc/passwd"),
            (DiagnosticResourceKind.REVIEWED_ARTIFACT, "rustup\n"),
            (DiagnosticResourceKind.PI_RELEASE_ASSET, "evil.json"),
            (DiagnosticResourceKind.PI_RELEASE_ASSET, "SHA256SUMS\n"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "npm-assembler-"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "npm-assembler-../../x"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "npm-assembler-XYZ"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "npm-assembler-a"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "npm-assembler-0123456789abcde"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "npm-assembler-0123456789abcdef0"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "npm-assembler-0123456789ABCDEF"),
            (DiagnosticResourceKind.ASSEMBLER_CONTAINER, "some-container"),
        )
        for kind, name in rejected:
            with self.subTest(kind=kind, name=name):
                with self.assertRaises((TypeError, ValueError)):
                    DiagnosticLogicalResource(kind, name)

    def test_rejects_non_member_kind_and_non_string_name(self):
        with self.assertRaises(TypeError):
            DiagnosticLogicalResource("reviewed_artifact", "rustup")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            DiagnosticLogicalResource(DiagnosticResourceKind.REVIEWED_ARTIFACT, 5)  # type: ignore[arg-type]

    def test_projection_rejects_unvalidated_resource_labels(self):
        for label in ("rustup", "../../etc/passwd", "npm-assembler-xyz"):
            with self.subTest(label=label):
                with self.assertRaises(TypeError):
                    _project("failed", logical_resource=label)

    def test_projection_carries_only_the_validated_resource_name(self):
        resource = DiagnosticLogicalResource(
            DiagnosticResourceKind.REVIEWED_ARTIFACT, "rustup"
        )
        diagnostic = _project("failed", secrets=(), logical_resource=resource)
        self.assertEqual("rustup", diagnostic.logical_resource)
        self.assertEqual("failed", diagnostic.text)


class TestStructuredHostProjection(unittest.TestCase):
    def test_removes_every_url_component_and_separates_normalized_host(self):
        text = "download https://alice:s3cr3t@ExAmPlE.com:8443/a/b?q=1&sig=dead#frag retry"
        diagnostic = _project(text, secrets=("s3cr3t",))
        self.assertEqual("download <redacted> retry", diagnostic.text)
        self.assertEqual(("example.com",), diagnostic.hostnames)
        for leaked in (
            "alice",
            "s3cr3t",
            "ExAmPlE.com",
            "8443",
            "/a/b",
            "q=1",
            "dead",
            "frag",
        ):
            self.assertNotIn(leaked, diagnostic.text)

    def test_proxy_endpoint_is_removed_without_a_host_fact(self):
        proxy = "http://proxy.corp.example:3128"
        diagnostic = _project(f"npm retry via {proxy} failed", secrets=(proxy,))
        self.assertEqual("npm retry via <redacted> failed", diagnostic.text)
        self.assertEqual((), diagnostic.hostnames)
        self.assertNotIn("proxy.corp.example", diagnostic.text)
        self.assertNotIn("3128", diagnostic.text)

    def test_malformed_candidates_are_fully_replaced_without_host_facts(self):
        for candidate in ("https://:8080/x", "https://@/x", "https://[:::1]/x"):
            with self.subTest(candidate=candidate):
                diagnostic = _project(f"bad {candidate} end")
                self.assertEqual("bad <redacted> end", diagnostic.text)
                self.assertEqual((), diagnostic.hostnames)

    def test_normalizes_idn_ipv4_and_ipv6_hosts(self):
        cases = {
            "https://bücher.example/x": "xn--bcher-kva.example",
            "https://192.0.2.10:443/x": "192.0.2.10",
            "https://[2001:DB8::1]:443/x": "2001:db8::1",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                diagnostic = _project(f"see {url} now")
                self.assertEqual("see <redacted> now", diagnostic.text)
                self.assertEqual((expected,), diagnostic.hostnames)

    def test_plain_text_is_unchanged_and_bare_secret_is_redacted(self):
        safe, hostnames = sanitize_diagnostic_text("nothing sensitive here")
        self.assertEqual("nothing sensitive here", safe)
        self.assertEqual((), hostnames)

        safe, hostnames = sanitize_diagnostic_text(
            "token=topsecret", secrets=("topsecret",)
        )
        self.assertEqual("token=<redacted>", safe)
        self.assertEqual((), hostnames)

    def test_projection_is_deterministic_and_has_no_output_policy_input(self):
        text = "see https://example.com/x now"
        self.assertEqual(_project(text), _project(text))
        parameters = set(inspect.signature(project_structured_diagnostic).parameters)
        self.assertFalse(
            parameters
            & {"show_network_hosts", "output_policy", "host_heartbeat", "output"}
        )


class TestBoundarySafeUrlSanitization(unittest.TestCase):
    def test_url_split_across_many_small_chunks(self):
        url = "https://alice:s3cr3t@example.com:8443/path?q=1#frag"
        text = f"GET {url} done"
        data = text.encode("utf-8")
        for size in (1, 2, 3, 5, 8, 13):
            with self.subTest(size=size):
                projector = DiagnosticProjector(secrets=("s3cr3t",))
                out: list[str] = []
                for i in range(0, len(data), size):
                    out.extend(projector.feed_bytes(data[i : i + size]))
                    self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
                out.extend(projector.finish())
                self.assertEqual("GET <redacted> done", "".join(out))
                self.assertEqual(("example.com",), projector.hostnames)

    def test_split_credentials_and_encoded_url_forms(self):
        cases = (
            "up https://user%3Apass@Example.com/a%20b?q=x%2Fy end",
            "up https://user:pass@example.com/x end",
        )
        for text in cases:
            with self.subTest(text=text):
                projector = DiagnosticProjector()
                out: list[str] = []
                for byte in text.encode("utf-8"):
                    out.extend(projector.feed_bytes(bytes((byte,))))
                out.extend(projector.finish())
                self.assertEqual("up <redacted> end", "".join(out))
                self.assertEqual(("example.com",), projector.hostnames)

    def test_idn_host_split_across_bytes(self):
        data = "see https://bücher.example/x done".encode("utf-8")
        projector = DiagnosticProjector()
        out: list[str] = []
        for i in range(len(data)):
            out.extend(projector.feed_bytes(data[i : i + 1]))
            self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        self.assertEqual("see <redacted> done", "".join(out))
        self.assertEqual(("xn--bcher-kva.example",), projector.hostnames)

    def test_incomplete_url_candidate_finalizes_fail_closed(self):
        for text in (
            "see https:",
            "see https:/",
            "see https://",
            "see https://exam",
            "see https://exam.",
        ):
            with self.subTest(text=text):
                safe, hostnames = sanitize_diagnostic_text(text)
                self.assertEqual(f"see {INCOMPLETE_TOKEN_MARKER}", safe)
                self.assertEqual((), hostnames)

    def test_incomplete_secret_prefix_finalizes_fail_closed(self):
        safe, hostnames = sanitize_diagnostic_text(
            "token=superse", secrets=("supersecret",)
        )
        self.assertEqual(f"token={INCOMPLETE_TOKEN_MARKER}", safe)
        self.assertEqual((), hostnames)

    def test_reader_failure_finalizes_pending_candidate(self):
        projector = DiagnosticProjector(secrets=("supersecret",))
        out = list(projector.feed_text("token=superse"))
        out.extend(projector.finish(abort=True))
        joined = "".join(out)
        self.assertNotIn("superse", joined)
        self.assertIn(INCOMPLETE_TOKEN_MARKER, joined)
        self.assertEqual((), projector.hostnames)

    def test_non_listed_schemes_are_withheld_at_every_boundary(self):
        cases = (
            ("git://user:password@secret.example/path", "secret.example"),
            ("custom+transport://secret.example/path", "secret.example"),
            ("custom+transport://user@h.example:8443/p?q=1#frag", "h.example"),
        )
        forbidden = (
            "git",
            "custom",
            "transport",
            "user",
            "password",
            "secret.example",
            "h.example",
            "/path",
            "?q=1",
            "#frag",
        )
        for url, host in cases:
            text = f"GET {url} done"
            for index in range(1, len(text)):
                with self.subTest(url=url, index=index):
                    projector = DiagnosticProjector()
                    out: list[str] = []
                    out.extend(projector.feed_text(text[:index]))
                    out.extend(projector.feed_text(text[index:]))
                    out.extend(projector.finish())
                    joined = "".join(out)
                    self.assertEqual("GET <redacted> done", joined)
                    self.assertEqual((host,), projector.hostnames)
                    for token in forbidden:
                        self.assertNotIn(token, joined)

    def test_non_listed_scheme_split_across_single_bytes(self):
        text = "GET custom+transport://user:password@secret.example/path done"
        data = text.encode("utf-8")
        projector = DiagnosticProjector()
        out: list[str] = []
        for index in range(len(data)):
            out.extend(projector.feed_bytes(data[index : index + 1]))
            self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        self.assertEqual("GET <redacted> done", "".join(out))
        self.assertEqual(("secret.example",), projector.hostnames)

    def test_scheme_fragment_does_not_leak_across_a_chunk_boundary(self):
        projector = DiagnosticProjector()
        out: list[str] = []
        for chunk in ("gi", "t://user:password@secret.example/path "):
            out.extend(projector.feed_text(chunk))
        out.extend(projector.finish())
        joined = "".join(out)
        self.assertEqual("<redacted> ", joined)
        self.assertEqual(("secret.example",), projector.hostnames)
        for token in ("gi", "user", "password", "secret.example", "/path"):
            self.assertNotIn(token, joined)

    def test_non_listed_scheme_urls_are_replaced_and_processing_resumes(self):
        safe, hostnames = sanitize_diagnostic_text(
            "a git://x.example/p b custom+transport://y.example/q c"
        )
        self.assertEqual("a <redacted> b <redacted> c", safe)
        self.assertEqual(("x.example", "y.example"), hostnames)

    def test_scheme_like_words_are_emitted_unchanged(self):
        for text in ("git", "custom", "status", "custom+transport"):
            with self.subTest(text=text):
                safe, hostnames = sanitize_diagnostic_text(text)
                self.assertEqual(text, safe)
                self.assertNotIn(INCOMPLETE_TOKEN_MARKER, safe)
                self.assertEqual((), hostnames)
        for chunks, expected in (
            (("git", " status"), "git status"),
            (("git", "!"), "git!"),
            (("run ", "custom", " now"), "run custom now"),
        ):
            with self.subTest(chunks=chunks):
                projector = DiagnosticProjector()
                out: list[str] = []
                for chunk in chunks:
                    out.extend(projector.feed_text(chunk))
                out.extend(projector.finish())
                joined = "".join(out)
                self.assertEqual(expected, joined)
                self.assertNotIn(INCOMPLETE_TOKEN_MARKER, joined)

    def test_eof_separates_ordinary_words_from_unresolved_candidates(self):
        for text in (
            "git",
            "custom",
            "status",
            "custom+transport",
            "run git now",
        ):
            with self.subTest(ordinary=text):
                safe, hostnames = sanitize_diagnostic_text(text)
                self.assertEqual(text, safe)
                self.assertNotIn(INCOMPLETE_TOKEN_MARKER, safe)
                self.assertEqual((), hostnames)
        for text in (
            "see git:",
            "see git/",
            "see git%3A",
            "see git://",
            "see git://host",
        ):
            with self.subTest(unresolved=text):
                safe, hostnames = sanitize_diagnostic_text(text)
                self.assertEqual(f"see {INCOMPLETE_TOKEN_MARKER}", safe)
                self.assertEqual((), hostnames)

    def test_clean_eof_preserves_bare_scheme_like_words(self):
        for text in ("git", "custom+transport"):
            with self.subTest(text=text):
                safe, hostnames = sanitize_diagnostic_text(text)
                self.assertEqual(text, safe)
                self.assertNotIn(INCOMPLETE_TOKEN_MARKER, safe)
                self.assertEqual((), hostnames)

    def test_abort_fails_closed_for_ambiguous_literal_prefixes(self):
        for text in ("git", "custom+transport", "git:", "git:/"):
            with self.subTest(text=text):
                projector = DiagnosticProjector()
                out: list[str] = []
                for character in text:
                    out.extend(projector.feed_text(character))
                out.extend(projector.finish(abort=True))
                joined = "".join(out)
                self.assertEqual(INCOMPLETE_TOKEN_MARKER, joined)
                self.assertNotIn(text, joined)
                self.assertEqual((), projector.hostnames)

    def test_abort_fails_closed_for_ambiguous_encoded_prefixes(self):
        for text in ("%", "%6", "%67", "%67it", "git%3"):
            with self.subTest(text=text):
                projector = DiagnosticProjector()
                out: list[str] = []
                for character in text:
                    out.extend(projector.feed_text(character))
                out.extend(projector.finish(abort=True))
                joined = "".join(out)
                self.assertEqual(INCOMPLETE_TOKEN_MARKER, joined)
                self.assertNotIn(text, joined)
                self.assertEqual((), projector.hostnames)

    def test_abort_still_replaces_complete_urls(self):
        projector = DiagnosticProjector()
        out = list(projector.feed_text("see git://secret.example/p done "))
        out.extend(projector.finish(abort=True))
        self.assertEqual("see <redacted> done ", "".join(out))
        self.assertEqual(("secret.example",), projector.hostnames)

    def test_host_fact_is_withheld_when_the_host_is_a_registered_secret(self):
        safe, hostnames = sanitize_diagnostic_text(
            "see https://internal.corp/x now", secrets=("internal.corp",)
        )
        self.assertEqual("see <redacted> now", safe)
        self.assertEqual((), hostnames)

    def test_encoded_delimiters_inside_a_url_are_removed(self):
        safe, hostnames = sanitize_diagnostic_text("GET https://h.example/a%0Ab%0D%0Ac ok")
        self.assertEqual("GET <redacted> ok", safe)
        self.assertEqual(("h.example",), hostnames)

    def test_zone_scoped_and_percent_hosts_fail_closed(self):
        for candidate in ("https://[fe80::1%25eth0]/x", "https://bad%20host.example/x"):
            with self.subTest(candidate=candidate):
                safe, hostnames = sanitize_diagnostic_text(f"GET {candidate} ok")
                self.assertEqual("GET <redacted> ok", safe)
                self.assertEqual((), hostnames)

    def test_non_http_proxy_schemes_are_sanitized(self):
        for scheme in ("socks5", "socks5h", "ftp"):
            with self.subTest(scheme=scheme):
                safe, hostnames = sanitize_diagnostic_text(
                    f"retry {scheme}://proxy.example:1080 next"
                )
                self.assertEqual("retry <redacted> next", safe)
                self.assertEqual(("proxy.example",), hostnames)

    def test_encoded_scheme_separators_are_fully_redacted(self):
        cases = (
            "GET https%3A%2F%2Falice%3Asecret%40example.com:8443/path%3Fq%3D1%26sig%3Ddead done",
            "GET https%3a%2f%2falice%3asecret%40example.com:8443/path done",
            "GET https%3A//alice%3Asecret%40example.com:8443/path done",
            "GET https:/%2Falice%3Asecret%40example.com:8443/path done",
            "GET https:%2F%2Falice%3Asecret%40example.com:8443/path done",
        )
        leaked = ("alice", "secret", "example.com", "8443", "path", "q=1", "sig", "dead")
        for text in cases:
            with self.subTest(text=text):
                safe, hostnames = sanitize_diagnostic_text(text, secrets=("secret",))
                self.assertEqual("GET <redacted> done", safe)
                self.assertEqual(("example.com",), hostnames)
                for token in leaked:
                    self.assertNotIn(token, safe)

    def test_encoded_scheme_separators_split_across_byte_chunks(self):
        text = "GET https%3A%2F%2Falice%3Asecret%40example.com:8443/p%3Fq%3D1 done"
        data = text.encode("utf-8")
        for size in range(1, 10):
            with self.subTest(size=size):
                projector = DiagnosticProjector(secrets=("secret",))
                out: list[str] = []
                for index in range(0, len(data), size):
                    out.extend(projector.feed_bytes(data[index : index + size]))
                    self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
                out.extend(projector.finish())
                joined = "".join(out)
                self.assertEqual("GET <redacted> done", joined)
                self.assertEqual(("example.com",), projector.hostnames)
                for token in ("alice", "secret", "example.com", "8443", "/p", "q=1"):
                    self.assertNotIn(token, joined)

    def test_encoded_scheme_separator_split_by_characters(self):
        projector = DiagnosticProjector()
        out: list[str] = []
        for character in "GET https%3A%2F%2Fh.example/x done":
            out.extend(projector.feed_text(character))
            self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        self.assertEqual("GET <redacted> done", "".join(out))
        self.assertEqual(("h.example",), projector.hostnames)

    def test_percent_encoded_scheme_characters_are_fully_redacted(self):
        cases = (
            "GET %68%74%74%70%73%3A%2F%2Falice%3Asecret%40example.com:8443/path%3Fq%3D1%23frag done",
            "GET h%74tps%3A%2F%2Falice%3Asecret%40example.com:8443/path%3Fq%3D1%23frag done",
            "GET ht%74p%73://alice%3Asecret%40example.com:8443/path done",
            "GET %68t%74ps%3A%2F%2Falice%3Asecret%40example.com:8443/path done",
        )
        leaked = ("alice", "secret", "example.com", "8443", "path", "q=1", "frag")
        for text in cases:
            with self.subTest(text=text):
                safe, hostnames = sanitize_diagnostic_text(text, secrets=("secret",))
                self.assertEqual("GET <redacted> done", safe)
                self.assertEqual(("example.com",), hostnames)
                for token in leaked:
                    self.assertNotIn(token, safe)

    def test_encoded_scheme_split_across_byte_chunks(self):
        text = (
            "GET %68%74%74%70%73%3A%2F%2Falice%3Asecret%40example.com:8443"
            "/p%3Fq%3D1%23frag done"
        )
        data = text.encode("utf-8")
        for size in range(1, 10):
            with self.subTest(size=size):
                projector = DiagnosticProjector(secrets=("secret",))
                out: list[str] = []
                for index in range(0, len(data), size):
                    out.extend(projector.feed_bytes(data[index : index + size]))
                    self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
                out.extend(projector.finish())
                joined = "".join(out)
                self.assertEqual("GET <redacted> done", joined)
                self.assertEqual(("example.com",), projector.hostnames)
                for token in ("alice", "secret", "example.com", "8443", "/p", "q=1", "frag"):
                    self.assertNotIn(token, joined)

    def test_encoded_scheme_split_by_characters(self):
        projector = DiagnosticProjector()
        out: list[str] = []
        for character in "GET h%74tps%3A%2F%2Fh.example%2Fp%3Fq%3D1 done":
            out.extend(projector.feed_text(character))
            self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        joined = "".join(out)
        self.assertEqual("GET <redacted> done", joined)
        self.assertEqual(("h.example",), projector.hostnames)
        for token in ("h.example", "/p", "q=1"):
            self.assertNotIn(token, joined)

    def test_fully_encoded_authority_is_decoded_for_the_host_fact(self):
        text = "GET https%3A%2F%2Fh%2Eexample%2Fp%2Fq%3Fr%3D1 done"
        safe, hostnames = sanitize_diagnostic_text(text)
        self.assertEqual("GET <redacted> done", safe)
        self.assertEqual(("h.example",), hostnames)
        for token in ("h.example", "h%2Eexample", "/p", "r=1"):
            self.assertNotIn(token, safe)

    def test_double_encoded_separator_is_fully_redacted(self):
        text = "GET https%253A%252F%252Fexample.com/private done"
        safe, hostnames = sanitize_diagnostic_text(text, secrets=("secret",))
        self.assertEqual("GET <redacted> done", safe)
        self.assertEqual(("example.com",), hostnames)
        for token in ("https%253A", "%252F", "example.com", "private"):
            self.assertNotIn(token, safe)

    def test_fully_double_encoded_scheme_and_authority_are_redacted(self):
        text = (
            "GET %2568%2574%2574%2570%2573%253A%252F%252F"
            "alice%253Asecret%2540example%252Ecom%253A8443"
            "%252Fprivate%253Fq%253D1%2523frag done"
        )
        safe, hostnames = sanitize_diagnostic_text(text, secrets=("secret",))
        self.assertEqual("GET <redacted> done", safe)
        self.assertEqual(("example.com",), hostnames)
        for token in (
            "alice",
            "secret",
            "example",
            "8443",
            "private",
            "q=1",
            "frag",
            "%253A",
            "%252F",
        ):
            self.assertNotIn(token, safe)

    def test_nested_encoded_delimiters_split_across_byte_chunks(self):
        text = (
            "GET https%253A%252F%252Falice%253Asecret%2540example.com%253A8443"
            "%252Fprivate%253Fq%253D1 done"
        )
        data = text.encode("utf-8")
        for size in range(1, 10):
            with self.subTest(size=size):
                projector = DiagnosticProjector(secrets=("secret",))
                out: list[str] = []
                for index in range(0, len(data), size):
                    out.extend(projector.feed_bytes(data[index : index + size]))
                    self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
                out.extend(projector.finish())
                joined = "".join(out)
                self.assertEqual("GET <redacted> done", joined)
                self.assertEqual(("example.com",), projector.hostnames)
                for token in (
                    "alice",
                    "secret",
                    "example.com",
                    "8443",
                    "private",
                    "q=1",
                    "%253A",
                    "%252F",
                ):
                    self.assertNotIn(token, joined)

    def test_nested_encoded_delimiters_split_by_characters(self):
        projector = DiagnosticProjector()
        out: list[str] = []
        for character in "GET %2568%2574tps%253A%252F%252Fh.example%252Fp done":
            out.extend(projector.feed_text(character))
            self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        joined = "".join(out)
        self.assertEqual("GET <redacted> done", joined)
        self.assertEqual(("h.example",), projector.hostnames)
        for token in ("h.example", "/p", "%253A", "%252F"):
            self.assertNotIn(token, joined)

    def test_nested_decoding_is_bounded_to_the_layer_limit(self):
        self.assertGreaterEqual(URL_DECODE_LAYER_LIMIT, 2)
        separator = "://"
        for _ in range(URL_DECODE_LAYER_LIMIT):
            separator = urllib.parse.quote(separator, safe="")
        text = f"GET https{separator}example.com/private done"
        safe, hostnames = sanitize_diagnostic_text(text)
        self.assertEqual("GET <redacted> done", safe)
        self.assertEqual(("example.com",), hostnames)

    def test_encoded_url_without_decodable_host_omits_the_host_fact(self):
        candidates = (
            "https%3A%2F%2Fbad%20host.example/x",
            "https%3A%2F%2F%FF%FF.example/x",
            "https%3A%2F%2F/x",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                safe, hostnames = sanitize_diagnostic_text(f"see {candidate} now")
                self.assertEqual("see <redacted> now", safe)
                self.assertEqual((), hostnames)

    def test_candidate_cannot_be_retained_past_the_byte_limit(self):
        candidate = "https://example.com/" + "a" * (PENDING_LIMIT_BYTES + 512)
        safe, hostnames = sanitize_diagnostic_text(f"pre {candidate} post")
        self.assertIn(OVERSIZED_TOKEN_MARKER, safe)
        self.assertNotIn(REDACTED, safe)
        self.assertNotIn("example.com", safe)
        self.assertEqual((), hostnames)
        self.assertTrue(safe.endswith("post"), safe)

    def test_multibyte_unterminated_candidate_stays_within_the_limit(self):
        projector = DiagnosticProjector()
        text = "https://example.com/" + "🎉" * (2 * PENDING_LIMIT_BYTES)
        out: list[str] = []
        for index in range(0, len(text), 512):
            out.extend(projector.feed_text(text[index : index + 512]))
            self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        joined = "".join(out)
        self.assertIn(OVERSIZED_TOKEN_MARKER, joined)
        self.assertNotIn("🎉" * 16, joined)

    def test_multibyte_append_never_exceeds_the_limit_at_drain_entry(self):
        text = "https://example.com/" + "🎉" * (2 * PENDING_LIMIT_BYTES)
        projector = _DrainEntryProbe()
        out = list(projector.feed_text(text))
        self.assertTrue(projector.drain_entry_bytes)
        self.assertLessEqual(max(projector.drain_entry_bytes), PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        joined = "".join(out)
        self.assertIn(OVERSIZED_TOKEN_MARKER, joined)
        self.assertNotIn("🎉" * 16, joined)

    def test_secret_ordering_does_not_change_projection(self):
        text = "x https://a:b@h.example/p y token=zzz"
        self.assertEqual(
            sanitize_diagnostic_text(text, ("b", "zzz")),
            sanitize_diagnostic_text(text, ("zzz", "b")),
        )

    def test_finish_is_idempotent_and_feed_after_finish_is_rejected(self):
        projector = DiagnosticProjector()
        self.assertEqual((), projector.finish())
        self.assertEqual((), projector.finish())
        with self.assertRaises(RuntimeError):
            projector.feed_text("late")
        with self.assertRaises(RuntimeError):
            projector.feed_bytes(b"late")

    def test_every_chunk_boundary_withholds_sensitive_tokens(self):
        secrets = ("s3cr3t", "internal.corp")
        text = (
            "a https://alice:s3cr3t@internal.corp:8443/p?q=1#f "
            "b http://proxy.invalid:3128/x c token=s3cr3t d"
        )
        data = text.encode("utf-8")
        forbidden = (
            "s3cr3t",
            "internal.corp",
            "alice",
            "8443",
            "proxy.invalid",
            "3128",
            "/p",
            "q=1",
        )
        for size in range(1, 8):
            with self.subTest(size=size):
                projector = DiagnosticProjector(secrets=secrets)
                out: list[str] = []
                for i in range(0, len(data), size):
                    out.extend(projector.feed_bytes(data[i : i + size]))
                    self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
                out.extend(projector.finish())
                joined = "".join(out)
                for token in forbidden:
                    self.assertNotIn(token, joined)
                self.assertEqual(("proxy.invalid",), projector.hostnames)

    def test_pending_state_remains_bounded_for_an_unterminated_token(self):
        projector = DiagnosticProjector()
        data = ("https://example.com/" + "a" * (4 * PENDING_LIMIT_BYTES)).encode()
        out: list[str] = []
        for i in range(0, len(data), 7):
            out.extend(projector.feed_bytes(data[i : i + 7]))
            self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
        out.extend(projector.finish())
        joined = "".join(out)
        self.assertIn(OVERSIZED_TOKEN_MARKER, joined)
        self.assertNotIn("a" * 16, joined)
        self.assertEqual((), projector.hostnames)

    def test_oversized_token_discards_until_boundary_then_recovers(self):
        huge = "https://example.com/" + "a" * (2 * PENDING_LIMIT_BYTES)
        safe, hostnames = sanitize_diagnostic_text(
            f"pre {huge} post https://good.example/x end"
        )
        self.assertIn("pre ", safe)
        self.assertIn(OVERSIZED_TOKEN_MARKER, safe)
        self.assertNotIn("a" * 64, safe)
        self.assertTrue(safe.endswith("post <redacted> end"), safe)
        self.assertEqual(("good.example",), hostnames)
        self.assertNotIn("example.com", safe)

    def test_oversized_token_without_boundary_finalizes_without_incomplete_marker(self):
        projector = DiagnosticProjector()
        out = list(
            projector.feed_text(
                "https://example.com/" + "a" * (2 * PENDING_LIMIT_BYTES)
            )
        )
        out.extend(projector.finish())
        joined = "".join(out)
        self.assertIn(OVERSIZED_TOKEN_MARKER, joined)
        self.assertNotIn(INCOMPLETE_TOKEN_MARKER, joined)
        self.assertEqual((), projector.hostnames)


class TestCompleteUrlAtCleanEof(unittest.TestCase):
    """A syntactically complete URL ending at a clean EOF keeps its host fact."""

    def test_complete_url_at_clean_eof_is_redacted_with_its_hostname(self):
        for text, host in (
            ("https://example.com", "example.com"),
            ("https://example.com/", "example.com"),
            ("https%3A%2F%2Fexample.com", "example.com"),
            ("https://Example.COM.", "example.com"),
            ("https://bücher.example/x", "xn--bcher-kva.example"),
            ("https://192.0.2.10:443/x", "192.0.2.10"),
            ("https://[2001:DB8::1]:443/x", "2001:db8::1"),
            ("git://x.example/p", "x.example"),
        ):
            with self.subTest(text=text):
                safe, hostnames = sanitize_diagnostic_text(text)
                self.assertEqual("<redacted>", safe)
                self.assertEqual((host,), hostnames)
                self.assertNotIn(INCOMPLETE_TOKEN_MARKER, safe)

    def test_complete_url_is_complete_at_every_decoder_boundary(self):
        text = "https://alice:s3cr3t@example.com:8443/path?q=1#frag"
        data = text.encode("utf-8")
        for size in (1, 2, 3, 5, 8, 13):
            with self.subTest(size=size):
                projector = DiagnosticProjector(secrets=("s3cr3t",))
                out: list[str] = []
                for index in range(0, len(data), size):
                    out.extend(projector.feed_bytes(data[index : index + size]))
                    self.assertLessEqual(projector.pending_size, PENDING_LIMIT_BYTES)
                out.extend(projector.finish())
                self.assertEqual("<redacted>", "".join(out))
                self.assertEqual(("example.com",), projector.hostnames)

    def test_complete_url_with_credentials_path_query_keeps_only_the_host(self):
        text = "https://alice:s3cr3t@example.com:8443/path?q=1#frag"
        safe, hostnames = sanitize_diagnostic_text(text, secrets=("s3cr3t",))
        self.assertEqual("<redacted>", safe)
        self.assertEqual(("example.com",), hostnames)
        for token in ("alice", "s3cr3t", "example.com", "8443", "path", "q=1", "frag"):
            self.assertNotIn(token, safe)

    def test_incomplete_eof_forms_still_fail_closed(self):
        for text in (
            "https:",
            "https:/",
            "https://",
            "https:///path",
            "https://exam",
            "https://exam.",
            "https://example.com/%",
            "https://example.com/%6",
            "https://example.com/%zz",
            "https://example.com:",
            "https://example.com:notaport",
            "https://example.com:99999",
            "https://:8080/x",
            "https://@/x",
            "https://[:::1]/x",
            "git://host",
            "git://",
            "see git://host",
        ):
            with self.subTest(text=text):
                safe, hostnames = sanitize_diagnostic_text(text)
                prefix = "see " if text.startswith("see ") else ""
                self.assertEqual(f"{prefix}{INCOMPLETE_TOKEN_MARKER}", safe)
                self.assertEqual((), hostnames)

    def test_aborted_pending_url_is_fail_closed_without_a_hostname(self):
        for text in (
            "https://example.com",
            "https://alice:s3cr3t@example.com:8443/path?q=1#frag",
            "git://secret.example/p",
        ):
            with self.subTest(text=text):
                projector = DiagnosticProjector(secrets=("s3cr3t",))
                out: list[str] = []
                for character in text:
                    out.extend(projector.feed_text(character))
                out.extend(projector.finish(abort=True))
                joined = "".join(out)
                self.assertEqual(INCOMPLETE_TOKEN_MARKER, joined)
                self.assertEqual((), projector.hostnames)
                for token in ("example.com", "s3cr3t", "8443", "path"):
                    self.assertNotIn(token, joined)

    def test_eof_completeness_matches_the_boundary_terminated_result(self):
        for text in ("https://example.com", "https://example.com/p?q=1"):
            with self.subTest(text=text):
                at_eof = sanitize_diagnostic_text(text)
                at_boundary = sanitize_diagnostic_text(f"{text} ")
                self.assertEqual("<redacted>", at_eof[0])
                self.assertEqual("<redacted> ", at_boundary[0])
                self.assertEqual(at_eof[1], at_boundary[1])

    def test_complete_eof_url_host_is_still_suppressed_for_a_registered_secret(self):
        safe, hostnames = sanitize_diagnostic_text(
            "https://PROXY.EXAMPLE:8080/svc", secrets=("https://proxy.example:8080",)
        )
        self.assertEqual("<redacted>", safe)
        self.assertEqual((), hostnames)
        self.assertNotIn("proxy.example", safe)
        # Positive control: an unrelated source URL still records its host.
        safe, hostnames = sanitize_diagnostic_text(
            "https://source.example/svc", secrets=("https://proxy.example:8080",)
        )
        self.assertEqual("<redacted>", safe)
        self.assertEqual(("source.example",), hostnames)


class TestCanonicalHostSuppression(unittest.TestCase):
    """A registered proxy/secret host fact must never be emitted.

    Text redaction stays deterministic and case-sensitive, while hostname-fact
    suppression compares canonical hostnames so an uppercase, trailing-dot, or
    IDN secret cannot leak its host through the structured host fact.
    """

    def _assert_suppressed(
        self, secret: str, text: str, leaked: tuple[str, ...]
    ) -> None:
        safe, hostnames = sanitize_diagnostic_text(text, secrets=(secret,))
        self.assertEqual("retry <redacted> next", safe)
        self.assertEqual((), hostnames)
        for token in leaked:
            with self.subTest(secret=secret, token=token):
                self.assertNotIn(token, safe)

    def test_uppercase_proxy_url_secret_suppresses_the_host_fact(self):
        self._assert_suppressed(
            "https://PROXY.EXAMPLE:8080",
            "retry https://PROXY.EXAMPLE:8080/path next",
            ("PROXY.EXAMPLE", "proxy.example", "8080", "path"),
        )

    def test_uppercase_bare_hostname_secret_suppresses_the_host_fact(self):
        self._assert_suppressed(
            "PROXY.EXAMPLE",
            "retry https://PROXY.EXAMPLE/path next",
            ("PROXY.EXAMPLE", "proxy.example", "path"),
        )

    def test_trailing_dot_hostname_secret_suppresses_the_host_fact(self):
        self._assert_suppressed(
            "PROXY.EXAMPLE.",
            "retry https://proxy.example./path next",
            ("proxy.example", "path"),
        )

    def test_idn_secret_matches_its_normalized_punycode_hostname(self):
        self._assert_suppressed(
            "https://xn--bcher-kva.example:8080",
            "retry https://B\u00dcCHER.EXAMPLE:8080/path next",
            ("xn--bcher-kva.example", "B\u00dcCHER.EXAMPLE", "b\u00fccher.example", "8080"),
        )
        self._assert_suppressed(
            "https://B\u00dcCHER.EXAMPLE:8080",
            "retry https://xn--bcher-kva.example:8080/path next",
            ("xn--bcher-kva.example", "b\u00fccher.example", "8080"),
        )

    def test_proxy_url_with_user_information_and_port_suppresses_the_host_fact(self):
        self._assert_suppressed(
            "https://user:pass@PROXY.EXAMPLE:8080",
            "retry https://PROXY.EXAMPLE:8080/path next",
            ("PROXY.EXAMPLE", "proxy.example", "user", "pass", "8080", "path"),
        )

    def test_percent_encoded_ascii_hostname_secret_suppresses_the_host_fact(self):
        self._assert_suppressed(
            "https://%70roxy.example:8080",
            "retry https://proxy.example:8080/path next",
            ("%70roxy.example", "proxy.example", "8080", "path"),
        )
        self._assert_suppressed(
            "https://proxy.example:8080",
            "retry https://%70roxy.example:8080/path next",
            ("%70roxy.example", "proxy.example", "8080", "path"),
        )

    def test_percent_encoded_idn_hostname_secret_suppresses_the_host_fact(self):
        self._assert_suppressed(
            "https://b%C3%BCcher.example:8080",
            "retry https://B\u00dcCHER.EXAMPLE:8080/path next",
            (
                "b%C3%BCcher.example",
                "b\u00fccher.example",
                "B\u00dcCHER.EXAMPLE",
                "xn--bcher-kva.example",
                "8080",
            ),
        )
        self._assert_suppressed(
            "https://B\u00dcCHER.EXAMPLE:8080",
            "retry https://b%C3%BCcher.example:8080/path next",
            ("b%C3%BCcher.example", "b\u00fccher.example", "xn--bcher-kva.example"),
        )

    def test_fully_and_nested_encoded_url_secrets_suppress_the_host_fact(self):
        encoded = "retry https://proxy.example:8080/path next"
        leaked = ("%3A%2F%2F", "%253A%252F%252F", "proxy.example", "8080", "path")
        self._assert_suppressed(
            "https%3A%2F%2Fproxy.example%3A8080", encoded, leaked
        )
        self._assert_suppressed(
            "https%253A%252F%252Fproxy.example%253A8080", encoded, leaked
        )
        self._assert_suppressed(
            "https%253A%252F%252Fb%C3%BCcher.example%253A8080",
            "retry https://xn--bcher-kva.example:8080/path next",
            (
                "b%C3%BCcher.example",
                "b\u00fccher.example",
                "xn--bcher-kva.example",
                "8080",
            ),
        )

    def test_encoded_bare_host_secret_suppresses_the_host_fact(self):
        self._assert_suppressed(
            "%70roxy.example",
            "retry https://proxy.example/path next",
            ("%70roxy.example", "proxy.example", "path"),
        )
        self._assert_suppressed(
            "%70roxy.example.",
            "retry https://proxy.example./path next",
            ("%70roxy.example", "proxy.example", "path"),
        )

    def test_unrelated_source_url_still_records_its_hostname(self):
        safe, hostnames = sanitize_diagnostic_text(
            "see https://source.example/path next",
            secrets=("https://PROXY.EXAMPLE:8080",),
        )
        self.assertEqual("see <redacted> next", safe)
        self.assertEqual(("source.example",), hostnames)

    def test_unrelated_source_url_still_records_its_hostname_with_encoded_secrets(self):
        safe, hostnames = sanitize_diagnostic_text(
            "see https://source.example/path next",
            secrets=("https://%70roxy.example:8080", "https://b%C3%BCcher.example:8080"),
        )
        self.assertEqual("see <redacted> next", safe)
        self.assertEqual(("source.example",), hostnames)


class TestExceptionTypeChain(unittest.TestCase):
    def test_cause_is_preferred_over_context(self):
        outer = RuntimeError("outer")
        outer.__cause__ = OSError("cause")
        outer.__context__ = KeyError("context")
        self.assertEqual(
            ("RuntimeError", "OSError"), project_exception_type_chain(outer)
        )

    def test_context_is_traversed_when_no_cause_exists(self):
        outer = RuntimeError("outer")
        outer.__context__ = OSError("context")
        self.assertEqual(
            ("RuntimeError", "OSError"), project_exception_type_chain(outer)
        )

    def test_exception_reason_is_traversed(self):
        outer = urllib.error.URLError(TimeoutError("timed out"))
        self.assertEqual(
            ("URLError", "TimeoutError"), project_exception_type_chain(outer)
        )

    def test_exception_reason_takes_precedence_over_cause_and_context(self):
        outer = urllib.error.URLError(TimeoutError("reason"))
        outer.__cause__ = OSError("cause")
        outer.__context__ = KeyError("context")
        self.assertEqual(
            ("URLError", "TimeoutError"), project_exception_type_chain(outer)
        )

    def test_non_exception_reason_falls_back_to_cause_then_context(self):
        with_cause = urllib.error.URLError("connection refused")
        with_cause.__cause__ = OSError("cause")
        with_cause.__context__ = KeyError("context")
        self.assertEqual(
            ("URLError", "OSError"), project_exception_type_chain(with_cause)
        )
        context_only = urllib.error.URLError(b"bytes are not exceptions")
        context_only.__context__ = KeyError("context")
        self.assertEqual(
            ("URLError", "KeyError"), project_exception_type_chain(context_only)
        )

    def test_reason_cycle_terminates(self):
        outer = urllib.error.URLError(TimeoutError("first"))
        nested = outer.reason
        nested.reason = outer
        self.assertEqual(
            ("URLError", "TimeoutError"), project_exception_type_chain(outer)
        )

    def test_reason_messages_are_never_evaluated(self):
        outer = urllib.error.URLError(_MessageBombError("nested"))
        self.assertEqual(
            ("URLError", "_MessageBombError"),
            project_exception_type_chain(outer),
        )

    def test_hostile_reason_access_terminates_without_raising(self):
        hostile = _HostileRelationshipError("reason")
        self.assertEqual(
            ("_HostileRelationshipError",),
            project_exception_type_chain(hostile),
        )

    def test_hostile_reason_falls_back_to_safe_cause(self):
        hostile = _HostileRelationshipError("reason")
        hostile.__cause__ = KeyError("cause")
        self.assertEqual(
            ("_HostileRelationshipError", "KeyError"),
            project_exception_type_chain(hostile),
        )

    def test_hostile_reason_falls_back_to_safe_context(self):
        hostile = _HostileRelationshipError("reason")
        hostile.__context__ = OSError("context")
        self.assertEqual(
            ("_HostileRelationshipError", "OSError"),
            project_exception_type_chain(hostile),
        )

    def test_hostile_cause_falls_back_to_safe_context(self):
        hostile = _HostileRelationshipError("__cause__")
        hostile.__context__ = KeyError("context")
        self.assertEqual(
            ("_HostileRelationshipError", "KeyError"),
            project_exception_type_chain(hostile),
        )

    def test_hostile_cause_terminates_without_raising(self):
        hostile = _HostileRelationshipError("__cause__")
        self.assertEqual(
            ("_HostileRelationshipError",),
            project_exception_type_chain(hostile),
        )

    def test_hostile_context_terminates_without_raising(self):
        hostile = _HostileRelationshipError("__context__")
        self.assertEqual(
            ("_HostileRelationshipError",),
            project_exception_type_chain(hostile),
        )

    def test_relationship_cycle_terminates(self):
        first = RuntimeError("first")
        second = OSError("second")
        first.__cause__ = second
        second.__context__ = first
        self.assertEqual(
            ("RuntimeError", "OSError"), project_exception_type_chain(first)
        )

    def test_duplicate_types_are_suppressed_and_four_unique_limit_applies(self):
        first = _MessageBombError("1")
        first_dup = _MessageBombError("1b")
        second = KeyError("2")
        second_dup = KeyError("2b")
        third = OSError("3")
        fourth = TypeError("4")
        fifth = RuntimeError("5")
        first.__cause__ = first_dup
        first_dup.__cause__ = second
        second.__cause__ = second_dup
        second_dup.__cause__ = third
        third.__cause__ = fourth
        fourth.__cause__ = fifth
        self.assertEqual(
            ("_MessageBombError", "KeyError", "OSError", "TypeError"),
            project_exception_type_chain(first),
        )
        self.assertNotIn("RuntimeError", project_exception_type_chain(first))

    def test_requested_limit_cannot_exceed_the_four_type_cap(self):
        first = _MessageBombError("1")
        second = KeyError("2")
        third = OSError("3")
        fourth = TypeError("4")
        fifth = RuntimeError("5")
        first.__cause__ = second
        second.__cause__ = third
        third.__cause__ = fourth
        fourth.__cause__ = fifth
        with self.assertRaises(ValueError):
            project_exception_type_chain(first, limit=EXCEPTION_TYPE_LIMIT + 1)
        with self.assertRaises(ValueError):
            project_exception_type_chain(first, limit=5)
        self.assertEqual(
            ("_MessageBombError", "KeyError", "OSError", "TypeError"),
            project_exception_type_chain(first, limit=EXCEPTION_TYPE_LIMIT),
        )
        self.assertEqual(
            ("_MessageBombError", "KeyError"),
            project_exception_type_chain(first, limit=2),
        )

    def test_rejects_invalid_limit_values(self):
        reason = _MessageBombError("boom")
        for bad_limit in (0, -1, 2.5, True):
            with self.subTest(limit=bad_limit):
                with self.assertRaises(ValueError):
                    project_exception_type_chain(reason, limit=bad_limit)  # type: ignore[arg-type]

    def test_does_not_evaluate_exception_messages(self):
        self.assertEqual(
            ("_MessageBombError",), project_exception_type_chain(_MessageBombError())
        )

    def test_none_reason_is_empty(self):
        self.assertEqual((), project_exception_type_chain(None))

    def test_rejects_non_exception_reason(self):
        with self.assertRaises(TypeError):
            project_exception_type_chain("boom")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
